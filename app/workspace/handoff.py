"""One-time codes that move a signed-in PR view from a computer to a phone.

The desktop page shows a QR code. The QR holds only a random code — never the
SCM token — and the token waits here, in this process's memory, for at most
`ttl` seconds. The phone redeems the code exactly once, and only when it is
signed in as the same person who created it, so a photographed QR is useless
to anyone else. Redeeming deletes the entry whether it succeeds or not.

This is the same exposure a background job already has (`app/api/auth.py`:
the token is held in memory for the life of the job, never persisted or
logged). Like `InMemoryJobStore` it is per-process: behind several replicas a
redeem can land on the wrong one, and the phone is told to scan again.

Entries are keyed by a SHA-256 of the code, so the store never holds a code
an attacker could read back, and a lookup leaks nothing about near-misses.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.models import PullRequestRef

DEFAULT_TTL_SECONDS = 120.0
#: Far more than any real use; stops a stuck client from growing the dict.
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class Handoff:
    token: str
    ref: PullRequestRef
    email: str
    expires_at: float


class HandoffStore:
    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = MAX_ENTRIES,
    ) -> None:
        self.ttl = ttl
        self._clock = clock
        self._capacity = capacity
        self._entries: dict[str, Handoff] = {}
        self._lock = threading.Lock()

    def create(self, *, token: str, ref: PullRequestRef, email: str) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            if len(self._entries) >= self._capacity:
                # Drop the oldest; a code that old is about to expire anyway.
                oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
                del self._entries[oldest]
            self._entries[_key(code)] = Handoff(
                token=token, ref=ref, email=email, expires_at=self._clock() + self.ttl
            )
        return code

    def redeem(self, code: str, *, email: str) -> tuple[Handoff | None, str | None]:
        """`(handoff, None)` once, then `(None, reason)` forever after.

        Reasons: `expired_or_unknown`, `different_user`.
        """
        with self._lock:
            self._purge()
            entry = self._entries.pop(_key(code), None)
        if entry is None:
            return None, "expired_or_unknown"
        if not secrets.compare_digest(entry.email, email):
            return None, "different_user"
        return entry, None

    def discard(self, code: str, *, email: str) -> None:
        """Withdraw a code its creator no longer wants (the QR sheet closed)."""
        with self._lock:
            entry = self._entries.get(_key(code))
            if entry is not None and entry.email == email:
                del self._entries[_key(code)]

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._entries)

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._entries.items() if v.expires_at <= now]:
            del self._entries[key]


def _key(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()
