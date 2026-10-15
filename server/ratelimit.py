"""
LANDesk - Failed authentication throttle

Rejecting a wrong password immediately and letting the client reconnect makes
guessing free: the original server answered 968 attempts a second on
loopback, which is a wordlist in minutes. This adds the two things that cost
an attacker something and cost a person who mistyped their password almost
nothing - a fixed delay before every rejection, and a lockout per source
address after a handful of failures.

Keyed on the source address only, not the port, so reconnecting does not
reset the count. Pure bookkeeping with an injectable clock, so the behaviour
can be tested without waiting for real seconds to pass.
"""

import time

__all__ = ["AuthThrottle"]


class AuthThrottle:
    def __init__(self, max_failures=5, lockout=300.0, window=300.0,
                 max_tracked=4096, clock=time.monotonic):
        """
        max_failures  failures within `window` before the address is locked out
        lockout       how long a locked-out address stays locked out, seconds
        window        failures older than this no longer count
        max_tracked   ceiling on remembered addresses, so a flood of source
                      addresses cannot grow the table without bound
        """
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        self.max_failures = max_failures
        self.lockout = lockout
        self.window = window
        self.max_tracked = max_tracked
        self._clock = clock
        self._failures = {}   # address -> list of failure timestamps
        self._locked = {}     # address -> timestamp the lockout ends

    @staticmethod
    def key(addr):
        """Normalise a peername to the address part.

        `addr` is the (host, port, ...) tuple asyncio reports, or None when
        the transport cannot say. The port changes on every connection, so
        only the host is a useful key.
        """
        if isinstance(addr, (tuple, list)) and addr:
            return str(addr[0])
        return str(addr)

    def is_locked(self, addr) -> bool:
        return self.retry_after(addr) > 0.0

    def retry_after(self, addr) -> float:
        """Seconds until this address may try again; 0.0 if it may now."""
        k = self.key(addr)
        until = self._locked.get(k)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0.0:
            # Lockout served. Clear the failure history too, otherwise the
            # next single mistake would lock the address out again at once.
            self._locked.pop(k, None)
            self._failures.pop(k, None)
            return 0.0
        return remaining

    def record_failure(self, addr) -> bool:
        """Note a failed attempt. Returns True if that locked the address out."""
        now = self._clock()
        k = self.key(addr)
        self._prune(now)
        recent = [t for t in self._failures.get(k, []) if now - t < self.window]
        recent.append(now)
        self._failures[k] = recent
        if len(recent) >= self.max_failures:
            self._locked[k] = now + self.lockout
            return True
        return False

    def record_success(self, addr) -> None:
        """Forget an address's history once it proves it knows the password."""
        k = self.key(addr)
        self._failures.pop(k, None)
        self._locked.pop(k, None)

    def failure_count(self, addr) -> int:
        now = self._clock()
        k = self.key(addr)
        return len([t for t in self._failures.get(k, []) if now - t < self.window])

    def _prune(self, now):
        """Drop expired entries, and if still over the ceiling, the oldest."""
        for k in [k for k, until in self._locked.items() if until <= now]:
            del self._locked[k]
        for k in [k for k, ts in self._failures.items()
                  if not ts or now - ts[-1] >= self.window]:
            if k not in self._locked:
                del self._failures[k]
        if len(self._failures) > self.max_tracked:
            oldest = sorted(self._failures.items(), key=lambda kv: kv[1][-1])
            for k, _ in oldest[:len(self._failures) - self.max_tracked]:
                if k not in self._locked:
                    del self._failures[k]
