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

import os
from pathlib import Path

import httpx

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
