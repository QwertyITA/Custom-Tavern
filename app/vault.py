"""Character vault: a PIN gate over which cards the roster shows (§ CLAUDE.md
"What this project is" — the design notes for the character-vault feature
live there since it has no numbered DESIGN.md section of its own yet).

Not encryption. The threat model is explicitly a person glancing at (or
poking around) the phone's own screen without knowing the PIN — not the
filesystem underneath it. `data/settings.json` keeps holding the PIN as a
salted hash rather than the digits themselves for the same reason the
`api_key` fields do (§ config.Settings.to_dict): not because the backend
needs defending, but because there is no reason to keep a recoverable
secret lying around when a one-way hash does the same job. Whoever holds
the phone and the file both already has everything either way.

Attempt throttling is in-process only and resets on restart, unlike the
unlocked state itself (§ config.Settings.vault_unlocked, which is written
to disk on purpose). That asymmetry is deliberate: staying unlocked across
a restart is the whole point of the feature, but there is exactly one
vault and one person guessing at it in any one sitting, so a cheap
in-memory cooldown is all the "pretty secure if you don't know the PIN"
bar actually calls for.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

_ITERATIONS = 200_000
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 30.0

_fails = 0
_locked_until = 0.0


def valid_pin(pin: str) -> bool:
    return isinstance(pin, str) and len(pin) == 6 and pin.isdigit()


def new_salt() -> str:
    return os.urandom(16).hex()


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()


def verify(pin: str, salt: str, digest: str) -> bool:
    if not salt or not digest:
        return False
    return hmac.compare_digest(hash_pin(pin, salt), digest)


def cooldown_remaining() -> float:
    """Seconds left on a lockout from too many wrong guesses, 0 if none."""
    return max(0.0, _locked_until - time.monotonic())


def register_failure() -> None:
    global _fails, _locked_until
    _fails += 1
    if _fails >= _MAX_ATTEMPTS:
        _locked_until = time.monotonic() + _LOCKOUT_SECONDS
        _fails = 0


def register_success() -> None:
    global _fails, _locked_until
    _fails = 0
    _locked_until = 0.0


def reset() -> None:
    """Test-only: this module's throttling state is process-global (§ the
    module docstring), so a test that runs out a lockout has to be able to
    put it back rather than leaving it for whichever test runs next."""
    register_success()
