"""GitHub Gist persistence for the MMR history CSV and deeplol stats JSON.

Streamlit Community Cloud has an ephemeral filesystem — local writes vanish
when the container restarts. We round-trip the two state files (mmr_history.csv,
deeplol_stats.json) through a single secret gist. On startup the app pulls the
gist contents into the local files; after each refresh the app pushes the
updated contents back.

Configuration via st.secrets (or env vars for local dev):
    [gist]
    token = "ghp_xxx..."         # PAT with the 'gist' scope
    gist_id = "abcdef0123456789"  # ID of the secret gist

Files inside the gist must be named exactly "mmr_history.csv" and
"deeplol_stats.json".
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import httpx

from history import CSV_HEADER

GIST_API = "https://api.github.com/gists"
HISTORY_FILENAME = "mmr_history.csv"
STATS_FILENAME = "deeplol_stats.json"


def _config():
    """Resolve gist token + ID. Prefer Streamlit secrets, fall back to env vars."""
    token = None
    gist_id = None
    try:  # avoid import at module top so non-Streamlit contexts work
        import streamlit as st  # type: ignore

        if "gist" in st.secrets:
            cfg = st.secrets["gist"]
            token = cfg.get("token")
            gist_id = cfg.get("gist_id")
    except Exception:
        pass
    token = token or os.environ.get("GIST_TOKEN")
    gist_id = gist_id or os.environ.get("GIST_ID")
    return token, gist_id


def configured() -> bool:
    token, gist_id = _config()
    return bool(token and gist_id)


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_files() -> dict[str, str] | None:
    """Pull current gist files. Returns {filename: content} or None on error."""
    token, gist_id = _config()
    if not token or not gist_id:
        return None
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(f"{GIST_API}/{gist_id}", headers=_headers(token))
        if r.status_code != 200:
            return None
        files = (r.json() or {}).get("files") or {}
        out: dict[str, str] = {}
        for name, info in files.items():
            if info.get("truncated"):
                # Large file: re-fetch via raw_url
                raw_url = info.get("raw_url")
                if raw_url:
                    with httpx.Client(timeout=30.0) as c:
                        rr = c.get(raw_url, headers=_headers(token))
                    if rr.status_code == 200:
                        out[name] = rr.text
                continue
            content = info.get("content")
            if content is not None:
                out[name] = content
        return out
    except httpx.HTTPError:
        return None


def push_files(files: dict[str, str]) -> bool:
    """PATCH the gist with new file contents. Skipping files preserves existing ones."""
    token, gist_id = _config()
    if not token or not gist_id:
        return False
    payload = {"files": {name: {"content": content} for name, content in files.items()}}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.patch(
                f"{GIST_API}/{gist_id}",
                headers=_headers(token),
                json=payload,
            )
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def hydrate_from_gist(history_path: str, stats_path: str) -> tuple[bool, bool]:
    """Pull gist files and write to local paths. Returns (history_ok, stats_ok)."""
    files = fetch_files()
    if not files:
        return (False, False)
    h_ok = s_ok = False
    # Write with plain utf-8 — the file content from the gist already preserves
    # whatever BOM existed in the source. Using utf-8-sig here would add a
    # second BOM and break csv.DictReader's column names.
    if HISTORY_FILENAME in files:
        Path(history_path).write_text(files[HISTORY_FILENAME], encoding="utf-8")
        h_ok = True
    if STATS_FILENAME in files:
        Path(stats_path).write_text(files[STATS_FILENAME], encoding="utf-8")
        s_ok = True
    return (h_ok, s_ok)


def push_local_files(history_path: str, stats_path: str) -> bool:
    """Read current local files and push them to the gist."""
    payload: dict[str, str] = {}
    try:
        payload[HISTORY_FILENAME] = Path(history_path).read_text(encoding="utf-8-sig")
    except OSError:
        pass
    try:
        payload[STATS_FILENAME] = Path(stats_path).read_text(encoding="utf-8")
    except OSError:
        pass
    if not payload:
        return False
    return push_files(payload)


def _parse_csv(text: str) -> list[dict]:
    if text.startswith("﻿"):
        text = text[1:]
    if not text.strip():
        return []
    return list(csv.DictReader(io.StringIO(text)))


def _merge_csv(local_text: str, remote_text: str) -> str:
    """Union local and remote rows keyed by (timestamp, name, tag) — keeps
    every distinct refresh recorded by any client. Stable ordering by
    timestamp."""
    local_rows = _parse_csv(local_text)
    remote_rows = _parse_csv(remote_text)
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict] = []
    for r in local_rows + remote_rows:
        key = (
            r.get("timestamp", ""),
            r.get("name", ""),
            r.get("tag", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    merged.sort(key=lambda r: r.get("timestamp", ""))
    buf = io.StringIO()
    # lineterminator='\n' keeps the string single-newline; Path.write_text
    # will translate to the platform native line ending exactly once.
    w = csv.DictWriter(buf, fieldnames=CSV_HEADER, lineterminator="\n")
    w.writeheader()
    for r in merged:
        w.writerow({k: r.get(k, "") for k in CSV_HEADER})
    return "﻿" + buf.getvalue()


def _merge_json(local_text: str, remote_text: str) -> str:
    """Per-summoner entry merge — newest `updated_at` wins. Also drops any
    remote entry that's missing from local AND older than local's most-recent
    update; this preserves intentional local deletions (e.g. when a summoner
    no longer has recent ranked-flex games)."""
    def load(text: str) -> dict:
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return {}

    local_d = load(local_text)
    remote_d = load(remote_text)
    local_max_ts = max(
        (v.get("updated_at", "") for v in local_d.values()), default=""
    )
    out = dict(remote_d)
    for k, v in local_d.items():
        existing = out.get(k)
        if existing is None or (v.get("updated_at", "") > existing.get("updated_at", "")):
            out[k] = v
    # Drop stale remote-only entries when local has clearly run since.
    for k in list(out.keys()):
        if k not in local_d and out[k].get("updated_at", "") < local_max_ts:
            del out[k]
    return json.dumps(out, ensure_ascii=False, indent=2)


def push_with_merge(history_path: str, stats_path: str) -> bool:
    """Pull remote state, merge with local, write back, and push the union.
    Prevents the 'lost update' race when multiple sessions hydrate, refresh,
    and push around the same time — each session's appends survive instead of
    being clobbered by whoever pushed last."""
    if not configured():
        return False
    remote = fetch_files() or {}

    if HISTORY_FILENAME in remote:
        try:
            local_csv = Path(history_path).read_text(encoding="utf-8-sig")
        except OSError:
            local_csv = ""
        merged_csv = _merge_csv(local_csv, remote[HISTORY_FILENAME])
        # Write merged result back without adding another BOM.
        Path(history_path).write_text(merged_csv, encoding="utf-8")

    if STATS_FILENAME in remote:
        try:
            local_json = Path(stats_path).read_text(encoding="utf-8")
        except OSError:
            local_json = ""
        merged_json = _merge_json(local_json, remote[STATS_FILENAME])
        Path(stats_path).write_text(merged_json, encoding="utf-8")

    return push_local_files(history_path, stats_path)
