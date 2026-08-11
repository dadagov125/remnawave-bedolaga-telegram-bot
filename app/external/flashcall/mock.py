"""Local provider: no calls, no money, confirms on the second poll.

Lets the whole flow (create -> show number -> poll -> session) be exercised and
tested without a provider account. The factory refuses it whenever phone auth is
enabled outside development.
"""

import secrets

from .base import Verification, VerificationStatus


class MockProvider:
    name = 'mock'
    display_name = 'Mock (тест)'

    #: Number shown in the UI. Obviously fake so nobody dials it by accident.
    DIAL_NUMBER = '+70000000000'

    _polls: dict[str, int] = {}

    async def create(self, phone: str) -> Verification:
        verification_id = f'mock-{secrets.token_hex(6)}'
        self._polls[verification_id] = 0
        self._polls[f'{verification_id}:phone'] = phone  # type: ignore[assignment]
        return Verification(id=verification_id, dial_number=self.DIAL_NUMBER, expires_in=60)

    async def status(self, verification_id: str) -> VerificationStatus:
        # First poll stays PENDING so the UI's waiting state is exercised too.
        count = self._polls.get(verification_id, 0) + 1
        self._polls[verification_id] = count
        phone = self._polls.get(f'{verification_id}:phone')
        if count < 2:
            return VerificationStatus(confirmed=False)
        return VerificationStatus(confirmed=True, caller_phone=phone, cost='0.00')  # type: ignore[arg-type]
