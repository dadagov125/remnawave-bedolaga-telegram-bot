"""Phone verification by *incoming* call: the user dials our number.

The classic flashcall is the other way round — the service calls the user and
the caller ID digits are the code. We deliberately use the reverse flow:

* operator anti-spam does not touch a call the user places themselves;
* nothing has to be delivered to the user, so a blocked or roaming number still
  works;
* on 8-800 numbers the call is free for the user.

Flow: create() returns a number to dial and an id; the user calls; status()
reports CONFIRMED together with the caller ID, which must equal the number the
user typed. Ownership of the number is what gets proven.
"""

from dataclasses import dataclass
from typing import Protocol


class NoNumbersAvailableError(RuntimeError):
    """Provider has no free number to hand out right now.

    Not a failure of ours and not permanent: numbers come from a shared pool and
    each pending check holds one for the duration of its window. Deserves its own
    type so the user gets "all lines are busy, try in a minute" instead of a
    generic outage message.
    """


@dataclass(frozen=True)
class Verification:
    """Pending check: what to show the user and how long they have."""

    id: str
    dial_number: str
    expires_in: int


@dataclass(frozen=True)
class VerificationStatus:
    confirmed: bool
    expired: bool = False
    #: Caller ID reported by the provider. Compared with the entered number —
    #: a provider confirming "some call arrived" is not enough on its own.
    caller_phone: str | None = None
    cost: str | None = None


class CallVerificationProvider(Protocol):
    """One provider adapter. Deliberately tiny: TTL, attempts and rate limits
    live in the service, so swapping providers cannot change security behaviour.
    """

    name: str
    display_name: str

    async def create(self, phone: str) -> Verification: ...

    async def status(self, verification_id: str) -> VerificationStatus: ...
