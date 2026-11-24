"""Append-only, hash-chained event log (JSON Lines). Each entry commits to the previous entry's hash.

The log is the system of record: workspace state is a fold over these events (see projection.py).
Appends take an exclusive file lock so the CLI and the web server can share one data directory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path

try:  # POSIX only; on other platforms the in-process lock still protects a single server
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def append(self, event_type: str, data: dict) -> dict:
        with self._lock, self.path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            lines = [line for line in handle.read().splitlines() if line.strip()]
            last = json.loads(lines[-1]) if lines else None
            entry = {
                "seq": (last["seq"] + 1) if last else 1,
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "type": event_type,
                "data": data,
                "prev_hash": last["hash"] if last else GENESIS,
            }
            entry["hash"] = _digest(entry)
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
        return entry

    def verify(self) -> tuple[bool, str]:
        previous = GENESIS
        count = 0
        try:
            entries = self.entries()
        except json.JSONDecodeError:
            return False, "log contains a line that is not valid JSON"
        for count, entry in enumerate(entries, start=1):
            if entry.get("seq") != count:
                return False, f"sequence break at entry {count}"
            if entry.get("prev_hash") != previous:
                return False, f"chain break at entry {count}"
            if _digest(entry) != entry.get("hash"):
                return False, f"hash mismatch at entry {count} (entry was modified)"
            previous = entry["hash"]
        return True, f"{count} entries verified; chain intact"
