"""flashcall.ru adapter — verification by incoming call.

Two endpoints, no SDK, no webhooks: the result is polled.

    POST /api/v1/get-phone  {"token": ..., "phone": "+7..."} -> {"id", "phone"}
    GET  /api/v1/status?token=..&id=..                       -> {"status", "callerPhone", "cost"}

``status`` is PENDING / CONFIRMED / EXPIRED. The provider gives the user
60 seconds from creation, which is why the UI shows a countdown rather than a
vague "waiting" state.

Two things learned from the live API, worth keeping written down:

* ``get-phone`` answers **201**, not 200 — accept any 2xx or the happy path dies
  on the very first request;
* ``callerPhone`` in the status response is an echo of the number we submitted,
  not the number that actually dialled. The matching is done by the provider: we
  declare the expected caller when creating the check, and CONFIRMED only comes
  for a call from that number.
"""

import aiohttp
import structlog

from app.config import settings

from .base import NoNumbersAvailableError, Verification, VerificationStatus

logger = structlog.get_logger(__name__)

BASE_URL = 'https://flashcall.ru/api/v1'

#: Provider-side window. Kept here rather than in settings: it is their limit,
#: not our preference, and lying about it in the UI only confuses users.
CALL_WINDOW_SECONDS = 60


class FlashcallRuProvider:
    name = 'flashcall_ru'
    display_name = 'flashcall.ru'

    def __init__(self, token: str | None = None, timeout: int = 15) -> None:
        self.token = token or settings.PHONE_AUTH_API_KEY
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def create(self, phone: str) -> Verification:
        payload = {'token': self.token, 'phone': phone}
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(f'{BASE_URL}/get-phone', json=payload) as response:
                body = await response.json(content_type=None)
                # 201 Created is their success code for a new check.
                if not 200 <= response.status < 300:
                    logger.error('flashcall.ru get-phone failed', status=response.status, body=str(body)[:200])
                    # Numbers are allocated from a shared pool and every pending
                    # check holds one for its window, so exhaustion is routine
                    # under load rather than an outage.
                    if response.status == 503 or 'пул' in str(body.get('message', '')).lower():
                        raise NoNumbersAvailableError('no free numbers in provider pool')
                    raise RuntimeError(f'flashcall.ru returned {response.status}')

        return Verification(
            id=str(body['id']),
            dial_number=str(body['phone']),
            expires_in=CALL_WINDOW_SECONDS,
        )

    async def status(self, verification_id: str) -> VerificationStatus:
        params = {'token': self.token, 'id': verification_id}
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(f'{BASE_URL}/status', params=params) as response:
                body = await response.json(content_type=None)
                if not 200 <= response.status < 300:
                    logger.error('flashcall.ru status failed', status=response.status, body=str(body)[:200])
                    raise RuntimeError(f'flashcall.ru returned {response.status}')

        state = str(body.get('status', '')).upper()
        return VerificationStatus(
            confirmed=state == 'CONFIRMED',
            expired=state == 'EXPIRED',
            caller_phone=body.get('callerPhone'),
            cost=body.get('cost'),
        )
