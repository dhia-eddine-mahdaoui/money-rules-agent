"""Local demo accounts, sessions and CSRF tokens for the web page.

Demo users are generated on first start of the web server and stored salted and hashed in
`<data dir>/users.json`. The generated passphrases are written once to `<data dir>/demo_logins.txt`
(the data directory is git-ignored). Nothing here is meant for real users: it exists so the
approval flow has real identities and separation of duties (operators submit, approvers decide).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import base64
import threading
import time
from pathlib import Path


def _token(nbytes: int) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode().rstrip("=")


ROLES = {
    "operator": {"submit_rule", "process"},
    "approver": {"approve_rule", "decide"},
}
DEMO_USERS = {"operator": "operator", "approver": "approver"}
ITERATIONS = 120_000
SESSION_IDLE_SECONDS = 30 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


def _hash(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations).hex()


class UserStore:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.path = self.dir / "users.json"
        self.users = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def ensure_demo_users(self) -> dict[str, str]:
        """Create the demo users if missing. Returns {username: passphrase} for newly created users only."""
        created = {}
        for name, role in DEMO_USERS.items():
            if name in self.users:
                continue
            passphrase = _token(12)
            salt = os.urandom(16)
            self.users[name] = {"role": role, "salt": salt.hex(), "hash": _hash(passphrase, salt), "iterations": ITERATIONS}
            created[name] = passphrase
        if created:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.users, indent=2), encoding="utf-8")
            os.chmod(self.path, 0o600)
            logins = self.dir / "demo_logins.txt"
            logins.write_text("".join(f"{u}: {p}\n" for u, p in created.items()), encoding="utf-8")
            os.chmod(logins, 0o600)
        return created

    def verify(self, username: str, passphrase: str) -> str | None:
        record = self.users.get(username)
        salt = bytes.fromhex(record["salt"]) if record else b"\0" * 16
        expected = record["hash"] if record else "0" * 64
        digest = _hash(passphrase, salt, record["iterations"] if record else ITERATIONS)
        if hmac.compare_digest(digest, expected) and record:
            return record["role"]
        return None

    @staticmethod
    def can(role: str | None, permission: str) -> bool:
        return bool(role) and permission in ROLES.get(role, set())


class Sessions:
    """In-memory sessions. Every visitor gets one (it carries the CSRF token); logging in rotates the id."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def create(self, user: str | None = None, role: str | None = None) -> tuple[str, dict]:
        sid = _token(32)
        session = {"user": user, "role": role, "csrf": _token(24), "seen": time.time(), "flash": None}
        with self._lock:
            self._sessions[sid] = session
        return sid, session

    def get(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        with self._lock:
            session = self._sessions.get(sid)
            if session and time.time() - session["seen"] > SESSION_IDLE_SECONDS:
                del self._sessions[sid]
                return None
            if session:
                session["seen"] = time.time()
            return session

    def destroy(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def locked_out(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if now - t < LOCKOUT_SECONDS]
            self._failures[key] = recent
            return len(recent) >= MAX_FAILURES

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.time())


def csrf_ok(session: dict | None, submitted: str | None) -> bool:
    return bool(session and submitted) and hmac.compare_digest(session["csrf"].encode(), submitted.encode("utf-8", "replace"))
