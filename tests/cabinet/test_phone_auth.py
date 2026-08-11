"""Phone login: number normalisation, provider registry, poll logic, routes.

The suite deliberately avoids a database: the repository's cabinet tests mock the
session, and the behaviour worth pinning here is the security logic, not SQL.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.auth.flashcall import (
    InvalidPhoneError,
    PhoneMismatchError,
    VerificationExpiredError,
    mask_phone,
    normalize_phone,
    poll_verification,
)
from app.config import settings
from app.external.flashcall import get_provider, list_providers
from app.external.flashcall.base import NoNumbersAvailableError, VerificationStatus
from app.external.flashcall.mock import MockProvider


# ── normalisation ────────────────────────────────────────────────
# Every accepted shape must collapse to the same string: rate limits are keyed by
# the normalised number, so two spellings of one number must not get two buckets.
@pytest.mark.parametrize(
    'raw',
    [
        '+79991234567',
        '79991234567',
        '89991234567',
        '9991234567',
        '+7 (999) 123-45-67',
        '8 999 123 45 67',
    ],
)
def test_normalize_accepts_russian_shapes(raw):
    assert normalize_phone(raw) == '+79991234567'


@pytest.mark.parametrize('raw', ['', '123', '+1 202 555 0143', '7999123456', 'абвгд', '+7999123456789'])
def test_normalize_rejects_everything_else(raw):
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


def test_normalize_respects_allowed_country_codes(monkeypatch):
    monkeypatch.setattr(settings, 'PHONE_AUTH_ALLOWED_COUNTRY_CODES', '998', raising=False)
    with pytest.raises(InvalidPhoneError):
        normalize_phone('+79991234567')


def test_mask_phone_hides_the_middle():
    masked = mask_phone('+79991234567')
    assert masked == '+7999***4567'
    assert '123' not in masked


# ── provider registry ───────────────────────────────────────────
def test_registry_reports_missing_configuration(monkeypatch):
    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', '', raising=False)
    providers = {p['name']: p for p in list_providers()}

    assert providers['flashcall_ru']['is_configured'] is False
    assert providers['flashcall_ru']['required_settings'] == ['PHONE_AUTH_API_KEY']
    # The mock needs nothing, so it is always "configured".
    assert providers['mock']['is_configured'] is True


def test_registry_sees_a_filled_key(monkeypatch):
    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', 'secret', raising=False)
    providers = {p['name']: p for p in list_providers()}
    assert providers['flashcall_ru']['is_configured'] is True


def test_unknown_provider_raises_instead_of_falling_back():
    # A typo in the settings must never silently switch production to the mock,
    # where any call would confirm.
    with pytest.raises(ValueError):
        get_provider('nonexistent')


@pytest.mark.asyncio
async def test_mock_provider_confirms_on_second_poll():
    provider = MockProvider()
    verification = await provider.create('+79991234567')

    assert verification.dial_number
    assert verification.expires_in > 0

    first = await provider.status(verification.id)
    assert first.confirmed is False  # waiting state must be reachable in tests

    second = await provider.status(verification.id)
    assert second.confirmed is True
    assert second.caller_phone == '+79991234567'


# ── poll_verification ───────────────────────────────────────────
def _attempt(**overrides):
    base = {
        'phone': '+79991234567',
        'public_id': 'public',
        'call_id': 'provider-id',
        'provider': 'mock',
        'attempts': 0,
        'consumed_at': None,
        'cost': None,
        'expires_at': datetime.now(UTC) + timedelta(seconds=60),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _db_returning(attempt):
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: attempt)
    return db


@pytest.mark.asyncio
async def test_poll_rejects_unknown_session():
    with pytest.raises(VerificationExpiredError):
        await poll_verification(_db_returning(None), 'nope')


@pytest.mark.asyncio
async def test_poll_rejects_already_used_session():
    attempt = _attempt(consumed_at=datetime.now(UTC))
    with pytest.raises(VerificationExpiredError):
        await poll_verification(_db_returning(attempt), 'public')


@pytest.mark.asyncio
async def test_poll_rejects_expired_window():
    attempt = _attempt(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(VerificationExpiredError):
        await poll_verification(_db_returning(attempt), 'public')


@pytest.mark.asyncio
async def test_poll_rejects_call_from_another_number(monkeypatch):
    """The core ownership check: a call arrived, but not from the entered number.

    Without this, anyone could confirm somebody else's number by calling from
    their own phone.
    """
    attempt = _attempt()
    stub = SimpleNamespace(
        status=AsyncMock(return_value=VerificationStatus(confirmed=True, caller_phone='+79990000000')),
    )
    monkeypatch.setattr('app.cabinet.auth.flashcall.get_provider', lambda *_: stub)

    with pytest.raises(PhoneMismatchError):
        await poll_verification(_db_returning(attempt), 'public')

    assert attempt.consumed_at is None  # must not be spent on a failed check


@pytest.mark.asyncio
async def test_poll_returns_pending_without_consuming(monkeypatch):
    attempt = _attempt()
    stub = SimpleNamespace(status=AsyncMock(return_value=VerificationStatus(confirmed=False)))
    monkeypatch.setattr('app.cabinet.auth.flashcall.get_provider', lambda *_: stub)

    phone, confirmed = await poll_verification(_db_returning(attempt), 'public')

    assert (phone, confirmed) == ('+79991234567', False)
    assert attempt.consumed_at is None
    assert attempt.attempts == 1  # polls are counted


@pytest.mark.asyncio
async def test_poll_confirms_and_consumes(monkeypatch):
    attempt = _attempt()
    stub = SimpleNamespace(
        status=AsyncMock(
            return_value=VerificationStatus(confirmed=True, caller_phone='+7 (999) 123-45-67', cost='0.20'),
        ),
    )
    monkeypatch.setattr('app.cabinet.auth.flashcall.get_provider', lambda *_: stub)

    phone, confirmed = await poll_verification(_db_returning(attempt), 'public')

    assert (phone, confirmed) == ('+79991234567', True)
    assert attempt.consumed_at is not None  # single use
    assert attempt.cost == '0.20'  # cost recorded for expense tracking


# ── routing ─────────────────────────────────────────────────────
def test_phone_routes_are_registered(registered_paths):
    assert 'POST' in registered_paths['/cabinet/auth/phone/call']
    assert 'POST' in registered_paths['/cabinet/auth/phone/call/status']
    # Переключатель для админки — рядом с ним, а не в апстримном branding.py
    assert {'GET', 'PATCH'} <= registered_paths['/cabinet/auth/phone/settings']


def test_phone_oauth_wrapper_is_gone(registered_paths):
    """Вход по номеру живёт только на /auth/phone/*.

    Раньше рядом была OAuth-обёртка со своей HTML-страницей — кабинет уходил
    на неё редиректом. Форма встроена в страницу входа, обёртка удалена: две
    реализации одного потока расходились бы при первой же правке.
    """
    assert '/cabinet/auth/oauth/phone/page' not in registered_paths
    assert '/cabinet/auth/oauth/phone/authorize' not in registered_paths


# ── provider pool exhaustion ─────────────────────────────────────
@pytest.mark.asyncio
async def test_start_maps_pool_exhaustion_to_its_own_error(monkeypatch):
    """Numbers come from a shared pool, so exhaustion is routine under load.

    It must surface as its own error (-> 503 + Retry-After) rather than a generic
    failure, otherwise the user is told "service unavailable" when the honest
    answer is "all lines busy, try in a minute".
    """
    from app.cabinet.auth import flashcall as service

    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)  # no live check
    monkeypatch.setattr(service, '_check_rate_limits', AsyncMock())
    stub = SimpleNamespace(
        name='stub',
        create=AsyncMock(side_effect=NoNumbersAvailableError('pool empty')),
    )
    monkeypatch.setattr(service, 'get_provider', lambda *_: stub)

    with pytest.raises(service.NoNumbersError):
        await service.start_verification(db, '+79991234567', ip='127.0.0.1')


@pytest.mark.asyncio
async def test_start_reuses_a_live_check(monkeypatch):
    """A second request for the same number must not grab another pool number."""
    from app.cabinet.auth import flashcall as service

    live = _attempt(dial_number='+74990000000', expires_at=datetime.now(UTC) + timedelta(seconds=40))
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: live)
    create = AsyncMock()
    monkeypatch.setattr(service, 'get_provider', lambda *_: SimpleNamespace(name='stub', create=create))
    monkeypatch.setattr(service, '_check_rate_limits', AsyncMock())

    attempt, dial, left = await service.start_verification(db, '+79991234567', ip='127.0.0.1')

    assert attempt is live
    assert dial == '+74990000000'
    assert 0 < left <= 40
    create.assert_not_awaited()  # provider untouched — the point of the reuse


# ── переключатель в админке ──────────────────────────────────────
def test_settings_report_configured_provider(monkeypatch):
    """`configured` показывает, есть ли у выбранного провайдера ключ.

    Без этого признака админка предлагала бы включить вход, который упадёт на
    первом же запросе к провайдеру.
    """
    from app.cabinet.routes.auth_phone import _phone_auth_settings

    monkeypatch.setattr(settings, 'PHONE_AUTH_PROVIDER', 'flashcall_ru')
    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', '')
    assert _phone_auth_settings().configured is False

    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', 'fc_test')
    state = _phone_auth_settings()
    assert state.configured is True
    assert state.provider == 'flashcall_ru'


def test_settings_treat_mock_as_configured(monkeypatch):
    """mock ничего не требует — иначе локальную проверку нельзя было бы включить."""
    from app.cabinet.routes.auth_phone import _phone_auth_settings

    monkeypatch.setattr(settings, 'PHONE_AUTH_PROVIDER', 'mock')
    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', '')
    assert _phone_auth_settings().configured is True


@pytest.mark.asyncio
async def test_enabling_unconfigured_provider_is_refused(monkeypatch):
    """Включение без ключа — 409, а не тихо сохранённый флаг."""
    from fastapi import HTTPException

    from app.cabinet.routes.auth_phone import PhoneAuthSettingsUpdate, update_phone_auth_settings

    monkeypatch.setattr(settings, 'PHONE_AUTH_PROVIDER', 'flashcall_ru')
    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', '')

    with pytest.raises(HTTPException) as error:
        await update_phone_auth_settings(
            PhoneAuthSettingsUpdate(enabled=True),
            admin=SimpleNamespace(telegram_id=1),
            db=AsyncMock(),
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_disabling_works_even_without_key(monkeypatch):
    """Выключить можно всегда: иначе сломанный провайдер не даёт убрать вкладку."""
    from app.cabinet.routes.auth_phone import PhoneAuthSettingsUpdate, update_phone_auth_settings

    monkeypatch.setattr(settings, 'PHONE_AUTH_PROVIDER', 'flashcall_ru')
    monkeypatch.setattr(settings, 'PHONE_AUTH_API_KEY', '')
    monkeypatch.setattr(settings, 'PHONE_AUTH_ENABLED', True)

    saved: dict[str, object] = {}

    async def fake_set_value(db, key, value):
        saved[key] = value
        monkeypatch.setattr(settings, key, value)

    monkeypatch.setattr(
        'app.cabinet.routes.auth_phone.bot_configuration_service.set_value', fake_set_value,
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth_phone.bot_configuration_service.is_env_locked', lambda key: False,
    )

    result = await update_phone_auth_settings(
        PhoneAuthSettingsUpdate(enabled=False),
        admin=SimpleNamespace(telegram_id=1),
        db=AsyncMock(),
    )

    assert saved == {'PHONE_AUTH_ENABLED': False}
    assert result.enabled is False


@pytest.mark.asyncio
async def test_env_locked_setting_is_refused(monkeypatch):
    """Значение из окружения сохранилось бы в базу, но не применилось.

    Переключатель молча не сработал бы — поэтому 409 заранее.
    """
    from fastapi import HTTPException

    from app.cabinet.routes.auth_phone import PhoneAuthSettingsUpdate, update_phone_auth_settings

    monkeypatch.setattr(settings, 'PHONE_AUTH_PROVIDER', 'mock')
    monkeypatch.setattr(
        'app.cabinet.routes.auth_phone.bot_configuration_service.is_env_locked', lambda key: True,
    )

    with pytest.raises(HTTPException) as error:
        await update_phone_auth_settings(
            PhoneAuthSettingsUpdate(enabled=True),
            admin=SimpleNamespace(telegram_id=1),
            db=AsyncMock(),
        )

    assert error.value.status_code == 409
