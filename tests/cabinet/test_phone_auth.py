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
from app.external.flashcall.base import VerificationStatus
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


def test_phone_oauth_wrapper_is_registered(registered_paths):
    """These literal paths must resolve, i.e. be registered before the
    parameterised /oauth/{provider}/... routes.
    """
    assert 'GET' in registered_paths['/cabinet/oauth/phone/authorize']
    assert 'GET' in registered_paths['/cabinet/oauth/phone/page']
    assert 'POST' in registered_paths['/cabinet/oauth/phone/callback']
