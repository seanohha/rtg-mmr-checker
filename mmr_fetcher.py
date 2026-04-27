"""Fetch MMR from rankedkings.com's JSON API.

The page at https://rankedkings.com/mmr-checker submits to a JSON endpoint:
    GET https://api.rankedkings.com/lol-mmr/v2/check/{REGION}/{NAME}%23{TAG}/{QUEUE}/false

Response (200) on success:
    {"status": "SUCCESS", "mmr": 764, "lp": 14, "rank": "Bronze II (14 LP)",
     "tier": "BRONZE", "division": "II", "health": {...}, ...}

Response on lookup failure: 200 with empty body, or a non-SUCCESS status.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import httpx

API_URL = "https://api.rankedkings.com/lol-mmr/v2/check/{region}/{riot_id}/{queue}/false"

# Map config queue_type strings → API queue codes
QUEUE_MAP = {
    "ranked solo": "RANKED_SOLO",
    "ranked flex": "RANKED_FLEX",
    "normal draft": "NORMAL_DRAFT",
    "swift play": "SWIFTPLAY",
    "swiftplay": "SWIFTPLAY",
    "aram": "ARAM",
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://rankedkings.com",
    "Referer": "https://rankedkings.com/",
}


@dataclass
class MMRResult:
    ok: bool
    mmr: int | None = None
    rank: str | None = None
    tier: str | None = None
    division: str | None = None
    lp: int | None = None
    actual_mmr: int | None = None
    actual_rank: str | None = None
    raw: dict | None = None
    error: str | None = None


def _queue_code(queue_type: str) -> str:
    key = queue_type.strip().lower()
    return QUEUE_MAP.get(key, queue_type.replace(" ", "_").upper())


def _build_url(summoner: dict) -> str:
    riot_id = f"{summoner['name']}#{summoner['tag']}"
    return API_URL.format(
        region=summoner["region"],
        riot_id=urllib.parse.quote(riot_id, safe=""),
        queue=_queue_code(summoner["queue_type"]),
    )


async def fetch_mmr(
    summoner: dict,
    client: httpx.AsyncClient | None = None,
    max_attempts: int = 8,
    poll_delay: float = 3.0,
) -> MMRResult:
    """Fetch MMR; on HTTP 202 (queued) the API computes asynchronously, so retry."""
    import asyncio

    url = _build_url(summoner)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0)
    try:
        last_status = None
        for attempt in range(max_attempts):
            try:
                r = await client.get(url)
            except httpx.TimeoutException:
                return MMRResult(ok=False, error="timeout")
            except httpx.HTTPError as e:
                return MMRResult(ok=False, error=f"http error: {e}")

            last_status = r.status_code
            if r.status_code == 202:
                # Queued — wait then retry.
                await asyncio.sleep(poll_delay)
                continue
            if r.status_code != 200:
                return MMRResult(ok=False, error=f"HTTP {r.status_code}")

            body = r.text.strip()
            if not body:
                return MMRResult(ok=False, error="empty response (summoner not found?)")
            try:
                data = r.json()
            except Exception as e:
                return MMRResult(ok=False, error=f"invalid json: {e}")
            if data.get("status") != "SUCCESS" or data.get("mmr") is None:
                return MMRResult(
                    ok=False,
                    error=f"status={data.get('status')!r}",
                    raw=data,
                )
            actual = (data.get("health") or {}).get("actual") or {}
            return MMRResult(
                ok=True,
                mmr=int(data["mmr"]),
                rank=data.get("rank"),
                tier=data.get("tier"),
                division=data.get("division"),
                lp=data.get("lp"),
                actual_mmr=actual.get("mmr"),
                actual_rank=actual.get("rank"),
                raw=data,
            )
        return MMRResult(
            ok=False,
            error=f"still queued after {max_attempts} attempts (last status {last_status})",
        )
    finally:
        if own_client:
            await client.aclose()
