"""Cabinet login by phone number.

Routes are grouped by *channel*, not by provider:

    POST /auth/phone/call          start verification by incoming call
    POST /auth/phone/call/status   poll it; returns a session once confirmed

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
from ..dependencies import get_cabinet_db
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
