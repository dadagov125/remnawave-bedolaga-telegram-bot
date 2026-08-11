"""Cabinet login by phone number.

Routes are grouped by *channel*, not by provider:

    POST  /auth/phone/call          start verification by incoming call
    POST  /auth/phone/call/status   poll it; returns a session once confirmed
    GET   /auth/phone/settings      admin: is it on, which provider, is it usable
    PATCH /auth/phone/settings      admin: turn it on or off

    (reserved) POST /auth/phone/sms         send a code by SMS
    (reserved) POST /auth/phone/sms/verify  check the code

The split is not cosmetic: the channels differ in what the user does. A call
shows a number to dial and finishes by itself; an SMS shows a field to type a
code into. The client has to render different screens anyway.

What the client must NOT learn is *which service* places the calls — that is
chosen in the admin panel and can change without touching the frontend. Hence
the opaque ``session_id``: the provider's own identifiers never leave the
backend. Security rules live in app/cabinet/auth/flashcall.py.

The call flow has no code to type: the proof of ownership is that the call
arrived from the number the user entered.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import create_user_by_phone, get_user_by_phone
from app.database.models import User
from app.external.flashcall import list_providers
from app.services.system_settings_service import ReadOnlySettingError, bot_configuration_service

from ..auth.flashcall import (
    InvalidPhoneError,
    NoNumbersError,
    PhoneAuthError,
    PhoneMismatchError,
    RateLimitedError,
    VerificationExpiredError,
    mask_phone,
    poll_verification,
    start_verification,
)
from ..dependencies import get_cabinet_db, require_permission
from ..ip_utils import get_client_ip
from ..schemas.auth import AuthResponse
from .auth import _create_auth_response, _store_refresh_token


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/auth/phone', tags=['Cabinet Phone Auth'])


class PhoneCallBody(BaseModel):
    phone: str = Field(min_length=10, max_length=20)


class PhoneCallResponse(BaseModel):
    # Идентификатор наш, не провайдерский: клиент не должен знать, каким
    # сервисом мы пользуемся — это решается в админке и меняется без правок
    # фронтенда.
    session_id: str
    dial_number: str
    expires_in: int


class PhoneCallStatusBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


def _ensure_enabled() -> None:
    if not settings.PHONE_AUTH_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Phone authentication is disabled')


@router.post('/call', response_model=PhoneCallResponse)
async def start_call_verification(
    body: PhoneCallBody,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Ask the provider for a number the user must call."""
    _ensure_enabled()
    ip = get_client_ip(request)

    try:
        attempt, dial_number, expires_in = await start_verification(
            db,
            body.phone,
            ip=ip,
            user_agent=request.headers.get('user-agent'),
        )
    except InvalidPhoneError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except RateLimitedError as error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(error), headers={'Retry-After': '60'}) from error
    except NoNumbersError as error:
        # 503 with Retry-After: temporary and worth retrying, unlike a 400.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(error), headers={'Retry-After': '60'},
        ) from error
    except PhoneAuthError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except Exception as error:
        logger.error('Phone verification provider failed', error=str(error))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Сервис подтверждения недоступен') from error

    # The response deliberately says nothing about whether the number is already
    # registered — that would turn this endpoint into a way to enumerate users.
    return PhoneCallResponse(
        session_id=attempt.public_id,
        dial_number=dial_number,
        expires_in=expires_in,
    )


@router.post('/call/status', response_model=AuthResponse)
async def check_call_verification(
    body: PhoneCallStatusBody,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Poll the provider; issue a session once the call is confirmed.

    Returns 202 while waiting so the client can keep polling without treating
    "not yet" as an error.
    """
    _ensure_enabled()

    try:
        phone, confirmed = await poll_verification(db, body.session_id)
    except VerificationExpiredError as error:
        raise HTTPException(status.HTTP_410_GONE, str(error)) from error
    except PhoneMismatchError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except PhoneAuthError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    if not confirmed:
        raise HTTPException(status.HTTP_202_ACCEPTED, 'Ожидаем звонок')

    user = await get_user_by_phone(db, phone)
    if user is None:
        user = await create_user_by_phone(db, phone)
        logger.info('Cabinet user registered by phone', user_id=user.id, phone=mask_phone(phone))

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)
    return response


# ── Переключатель в админке ──────────────────────────────────────
# Отдельно от общего списка настроек бота: вход по номеру включается там же, где
# вход по email (Внешний вид → Опции интерфейса), иначе администратор ищет один
# способ входа в двух разных местах.
#
# Роуты живут здесь, а не в апстримном branding.py, чтобы ребейз не конфликтовал.


SETTING_KEY = 'PHONE_AUTH_ENABLED'


class PhoneAuthSettings(BaseModel):
    enabled: bool
    provider: str
    # Выбранный провайдер настроен (есть ключ). Без этого включённый вход
    # показывал бы вкладку, которая падает на первом же запросе.
    configured: bool


class PhoneAuthSettingsUpdate(BaseModel):
    enabled: bool


def _phone_auth_settings() -> PhoneAuthSettings:
    provider = (settings.PHONE_AUTH_PROVIDER or 'mock').strip().lower()
    # ProviderInfo — TypedDict, поэтому обращение по ключам.
    configured = any(i['name'] == provider and i['is_configured'] for i in list_providers())
    return PhoneAuthSettings(
        enabled=bool(settings.PHONE_AUTH_ENABLED),
        provider=provider,
        configured=configured,
    )


# GET только для админа: имя провайдера — не то, что должен видеть клиент.
# Странице входа он и не нужен, она узнаёт о способе из списка провайдеров.
@router.get('/settings', response_model=PhoneAuthSettings)
async def get_phone_auth_settings(_: User = Depends(require_permission('settings:edit'))):
    """Current state of phone login."""
    return _phone_auth_settings()


@router.patch('/settings', response_model=PhoneAuthSettings)
async def update_phone_auth_settings(
    body: PhoneAuthSettingsUpdate,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Turn phone login on or off."""
    current = _phone_auth_settings()

    # Включать ненастроенный провайдер бессмысленно: вкладка появится, а первый
    # же звонок упадёт. Отключить можно всегда.
    if body.enabled and not current.configured:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Провайдер {current.provider} не настроен: укажите ключ в настройках бота',
        )

    # Заданное через окружение значение сохранилось бы в базу, но не применилось —
    # переключатель молча не сработал бы. Проверяем заранее, как это делает общий
    # экран настроек.
    if bot_configuration_service.is_env_locked(SETTING_KEY):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'{SETTING_KEY} задана через переменную окружения — уберите её, чтобы менять из админки',
        )

    try:
        await bot_configuration_service.set_value(db, SETTING_KEY, body.enabled)
    except ReadOnlySettingError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    await db.commit()

    logger.info('Admin set phone auth enabled', telegram_id=admin.telegram_id, enabled=body.enabled)
    return _phone_auth_settings()
