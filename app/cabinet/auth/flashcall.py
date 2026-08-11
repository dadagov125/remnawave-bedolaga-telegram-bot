"""Phone verification by incoming call: sessions, limits, ownership check.

Security decisions live here rather than in the routes or the adapter, so
swapping a provider cannot weaken them:

* a number is normalised to E.164 before every check — otherwise ``+7 999…``,
  ``8999…`` and ``79991234567`` would each get their own rate-limit bucket and
  the limits would be trivially bypassed;
* three rate-limit levels (number, IP, global) because every completed call
  costs money: an unmetered endpoint is a direct attack on the balance;
* the caller ID reported by the provider must equal the number the user typed —
  defence in depth against a provider mixing up sessions.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PhoneAuthAttempt
from app.external.flashcall import get_provider
from app.utils.cache import RateLimitCache

logger = structlog.get_logger(__name__)

_DIGITS = re.compile(r'\D+')


class PhoneAuthError(Exception):
    """Expected failure, mapped to an HTTP code in the routes."""


class InvalidPhoneError(PhoneAuthError):
    pass


class RateLimitedError(PhoneAuthError):
    pass


class VerificationExpiredError(PhoneAuthError):
    pass


class PhoneMismatchError(PhoneAuthError):
    """Someone called, but not from the number that was entered."""


def normalize_phone(raw: str) -> str:
    """Normalise a Russian number to E.164 (``+79991234567``).

    Accepts what people actually type — ``8 999 123-45-67``,
    ``+7 (999) 123 45 67``, ``79991234567`` — and rejects everything else. A
    permissive parser here would let one number occupy several rate-limit
    buckets.
    """
    digits = _DIGITS.sub('', raw or '')

    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    # A bare 10-digit input is only unambiguous for mobile numbers, which all
    # start with 9. '7999123456' would otherwise become '+77999123456' — an area
    # code that does not exist. Regression test: test_normalize_rejects_everything_else.
    elif len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits

    if len(digits) != 11 or not digits.startswith('7'):
        raise InvalidPhoneError('Введите российский номер телефона')

    allowed = {c.strip() for c in (settings.PHONE_AUTH_ALLOWED_COUNTRY_CODES or '7').split(',') if c.strip()}
    if '7' not in allowed:
        raise InvalidPhoneError('Этот код страны не принимается')

    return '+' + digits


def mask_phone(phone: str) -> str:
    """``+79991234567`` -> ``+7999***4567``. Logs must never carry full numbers."""
    return phone[:5] + '***' + phone[-4:] if len(phone) >= 9 else '***'


async def _check_rate_limits(phone: str, ip: str) -> None:
    """Fail closed: with Redis down we refuse rather than let an
    unauthenticated, money-spending endpoint run unmetered.
    """
    per_hour = int(settings.PHONE_AUTH_RATE_LIMIT_PER_HOUR or 5)

    if await RateLimitCache.is_ip_rate_limited(phone, 'phoneauth_number', per_hour, 3600, fail_closed=True):
        raise RateLimitedError('Слишком много попыток для этого номера, попробуйте позже')
    if await RateLimitCache.is_ip_rate_limited(ip, 'phoneauth_ip', per_hour * 3, 3600, fail_closed=True):
        raise RateLimitedError('Слишком много попыток с этого адреса, попробуйте позже')
    if await RateLimitCache.is_ip_rate_limited('global', 'phoneauth_all', 500, 3600, fail_closed=True):
        raise RateLimitedError('Сервис перегружен, попробуйте позже')


async def start_verification(
    db: AsyncSession,
    raw_phone: str,
    *,
    ip: str,
    user_agent: str | None = None,
) -> tuple[PhoneAuthAttempt, str, int]:
    """Create a check and return (attempt, number to dial, seconds left)."""
    phone = normalize_phone(raw_phone)
    await _check_rate_limits(phone, ip)

    provider = get_provider()
    verification = await provider.create(phone)

    attempt = PhoneAuthAttempt(
        phone=phone,
        # Клиент видит только это; идентификатор провайдера наружу не уходит.
        public_id=secrets.token_urlsafe(24),
        call_id=verification.id,
        provider=provider.name,
        dial_number=verification.dial_number,
        expires_at=datetime.now(UTC) + timedelta(seconds=verification.expires_in),
        ip=ip,
        user_agent=(user_agent or '')[:512] or None,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)

    logger.info(
        'Phone verification started',
        phone=mask_phone(phone),
        provider=provider.name,
        call_id=verification.id,
    )
    return attempt, verification.dial_number, verification.expires_in


async def poll_verification(db: AsyncSession, public_id: str) -> tuple[str, bool]:
    """Ask the provider whether the call arrived.

    Takes our own opaque session id — the client never handles provider ids.
    Returns ``(phone, confirmed)``. Raises :class:`VerificationExpiredError`
    when the window closed and :class:`PhoneMismatchError` when the call came
    from a different number.
    """
    result = await db.execute(select(PhoneAuthAttempt).where(PhoneAuthAttempt.public_id == public_id).limit(1))
    attempt = result.scalar_one_or_none()

    if attempt is None or attempt.consumed_at is not None:
        raise VerificationExpiredError('Проверка не найдена или уже использована')
    if attempt.expires_at <= datetime.now(UTC):
        raise VerificationExpiredError('Время ожидания звонка истекло')

    status = await get_provider(attempt.provider).status(attempt.call_id)
    attempt.attempts += 1

    if status.expired:
        await db.commit()
        raise VerificationExpiredError('Время ожидания звонка истекло')

    if not status.confirmed:
        await db.commit()
        return attempt.phone, False

    # Ownership is proven by the provider: we declare the expected caller when
    # creating the check, and CONFIRMED only comes for a call from that number.
    # This comparison is defence in depth — it catches a provider that confuses
    # sessions or starts reporting the real caller instead of an echo.
    if status.caller_phone:
        try:
            caller = normalize_phone(status.caller_phone)
        except InvalidPhoneError:
            caller = status.caller_phone
        if caller != attempt.phone:
            await db.commit()
            logger.warning(
                'Phone verification caller mismatch',
                expected=mask_phone(attempt.phone),
                got=mask_phone(caller),
            )
            raise PhoneMismatchError('Звонок поступил с другого номера')

    attempt.consumed_at = datetime.now(UTC)
    attempt.cost = status.cost
    await db.commit()

    logger.info('Phone verified', phone=mask_phone(attempt.phone), cost=status.cost)
    return attempt.phone, True
