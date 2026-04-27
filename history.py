"""CSV-backed MMR history store."""
from __future__ import annotations

import csv
import os
from datetime import datetime
from threading import Lock
from typing import Iterable

CSV_HEADER = [
    "timestamp", "name", "tag", "region", "queue_type", "owner",
    "mmr", "rank", "actual_mmr", "actual_rank",
]

_write_lock = Lock()


def _read_header(path: str) -> list[str] | None:
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f)
            return next(r, None)
    except FileNotFoundError:
        return None


def _ensure_file(path: str) -> None:
    """Create the CSV with the current header, or migrate an older header in place."""
    header = _read_header(path)
    if header is None:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(CSV_HEADER)
        return
    if header == CSV_HEADER:
        return
    # Migrate: rewrite file with new header, preserving existing data.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_HEADER})


def append_record(
    path: str,
    summoner: dict,
    mmr: int | str,
    timestamp: datetime | None = None,
    rank: str | None = None,
    actual_mmr: int | None = None,
    actual_rank: str | None = None,
) -> dict:
    ts = (timestamp or datetime.now()).isoformat(timespec="seconds")
    row = {
        "timestamp": ts,
        "name": summoner["name"],
        "tag": summoner["tag"],
        "region": summoner["region"],
        "queue_type": summoner["queue_type"],
        "owner": summoner.get("owner", ""),
        "mmr": str(mmr),
        "rank": rank or "",
        "actual_mmr": "" if actual_mmr is None else str(actual_mmr),
        "actual_rank": actual_rank or "",
    }
    with _write_lock:
        _ensure_file(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADER).writerow(row)
    return row


def read_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def group_by_summoner(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Group history rows by 'name#tag' key."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        key = f"{r['name']}#{r['tag']}"
        out.setdefault(key, []).append(r)
    for k in out:
        out[k].sort(key=lambda r: r["timestamp"])
    return out
