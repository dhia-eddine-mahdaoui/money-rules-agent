"""Append-only, hash-chained audit log (JSON Lines). Each entry commits to the previous entry's hash."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def append(self, event_type: str, data: dict) -> dict:
        existing = self.entries()
        previous = existing[-1]["hash"] if existing else GENESIS
        entry = {
            "seq": len(existing) + 1,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "type": event_type,
            "data": data,
            "prev_hash": previous,
        }
        entry["hash"] = _digest(entry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        return entry

    def verify(self) -> tuple[bool, str]:
        previous = GENESIS
        count = 0
        for count, entry in enumerate(self.entries(), start=1):
            if entry["seq"] != count:
                return False, f"sequence break at entry {count}"
            if entry["prev_hash"] != previous:
                return False, f"chain break at entry {count}"
            if _digest(entry) != entry["hash"]:
                return False, f"hash mismatch at entry {count} (entry was modified)"
            previous = entry["hash"]
        return True, f"{count} entries verified; chain intact"
