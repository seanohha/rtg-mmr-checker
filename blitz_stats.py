"""Pull basic account info (puuid, level, last-played) from blitz.gg.

blitz.gg exposes a clean JSON endpoint on lol.v2.iesdev.com that returns
account + summoner + champion mastery in a single call, without CloudFront
gating or JS-challenge hurdles. We use it for the three "basic" fields —
puu_id, summoner level, and last-played timestamp (derived from the max
champion mastery `last_play_time`) — and let deeplol keep providing the
flex KDA aggregate and live-game check.

  GET https://lol.v2.iesdev.com/riot_account_v2/lol/{REGION}/{name}/{tag}
  → {"account": {puuid, game_name, tag_line},
     "summoner": {summoner_level, revision_date, profile_icon_id},
     "champion_mastery": {"<champ_id>": {last_play_time (ms), ...}, ...},
     "league_lol": {"RANKED_FLEX_SR": [...snapshots...], "RANKED_SOLO_5x5": [...]}
    }
"""
from __future__ import annotations

import urllib.parse

import httpx

BLITZ_BASE = "https://lol.v2.iesdev.com"
BLITZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://blitz.gg",
    "Referer": "https://blitz.gg/",
}


# tier → numeric base MMR. Every apex-tier (Master+) uses the same base
# because their internal LP-only score is >= 2800 anyway.
TIER_TO_BASE = {
    "IRON":        0,
    "BRONZE":     400,
    "SILVER":     800,
    "GOLD":      1200,
    "PLATINUM":  1600,
    "EMERALD":   2000,
    "DIAMOND":   2400,
    "MASTER":    2800,
    "GRANDMASTER": 2800,
    "CHALLENGER":  2800,
}
DIVISION_TO_OFFSET = {"IV": 0, "III": 100, "II": 200, "I": 300}


def rank_to_mmr(tier: str | None, division: str | None, lp: int | float | None) -> int | None:
    """Convert (tier, division, LP) → numeric MMR proxy on a 0-2800+ scale.
    Each tier spans 400 points, each division 100. Returns None if the tier
    isn't recognised."""
    if not tier:
        return None
    base = TIER_TO_BASE.get(tier.upper())
    if base is None:
        return None
    lp_int = int(lp or 0)
    # Apex tiers have no divisions — LP just accumulates on top of base.
    if base >= 2800:
        return base + lp_int
    off = DIVISION_TO_OFFSET.get((division or "IV").upper(), 0)
    return base + off + lp_int


def format_rank(tier: str | None, division: str | None, lp: int | float | None) -> str:
    """Format like rankedkings does: 'Bronze II (14 LP)'."""
    if not tier:
        return ""
    name = tier.capitalize()
    lp_int = int(lp or 0)
    if not division or TIER_TO_BASE.get(tier.upper(), 0) >= 2800:
        return f"{name} ({lp_int} LP)"
    return f"{name} {division} ({lp_int} LP)"


async def fetch_blitz_flex_current(
    summoner: dict, client: httpx.AsyncClient | None = None
) -> dict | None:
    """Fetch the latest RANKED_FLEX_SR snapshot from blitz.gg and package it
    like an rankedkings MMR result. Returns:

      {"mmr": int, "rank": str, "tier": str, "division": str, "lp": int,
       "wins": int, "losses": int} or None if the account has no flex rank.

    This is the fallback source we use when rankedkings is offline.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=BLITZ_HEADERS, timeout=15.0)
    try:
        url = (
            f"{BLITZ_BASE}/riot_account_v2/lol/{summoner['region'].upper()}"
            f"/{urllib.parse.quote(summoner['name'])}/{summoner['tag']}"
        )
        r = await client.get(url)
        if r.status_code != 200:
            return None
        try:
            data = r.json() or {}
        except Exception:
            return None
        league = data.get("league_lol") or {}
        history = league.get("RANKED_FLEX_SR") or []
        if not history:
            return None
        # Blitz returns snapshots newest-first sometimes and oldest-first
        # other times; pick the one with the largest `timestamp`.
        latest = max(history, key=lambda x: x.get("timestamp", 0))
        tier = latest.get("tier")
        division = latest.get("rank")
        lp = latest.get("league_points", 0)
        mmr = rank_to_mmr(tier, division, lp)
        if mmr is None:
            return None
        return {
            "mmr": int(mmr),
            "rank": format_rank(tier, division, lp),
            "tier": tier,
            "division": division,
            "lp": int(lp or 0),
            "wins": latest.get("wins"),
            "losses": latest.get("losses"),
        }
    except httpx.HTTPError:
        return None
    finally:
        if own:
            await client.aclose()


async def fetch_blitz_basic(
    summoner: dict, client: httpx.AsyncClient | None = None
) -> dict | None:
    """Return {puu_id, level?, last_played_at?} for a summoner, or None on
    lookup failure. `last_played_at` is unix seconds (Riot ships ms, we
    convert to match the rest of our app)."""
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=BLITZ_HEADERS, timeout=15.0)
    try:
        url = (
            f"{BLITZ_BASE}/riot_account_v2/lol/{summoner['region'].upper()}"
            f"/{urllib.parse.quote(summoner['name'])}/{summoner['tag']}"
        )
        r = await client.get(url)
        if r.status_code != 200:
            return None
        try:
            data = r.json() or {}
        except Exception:
            return None
        account = data.get("account") or {}
        puuid = account.get("puuid")
        if not puuid:
            return None
        out: dict = {"puu_id": puuid}
        summ = data.get("summoner") or {}
        level = summ.get("summoner_level")
        if level is not None:
            try:
                out["level"] = int(level)
            except (TypeError, ValueError):
                pass
        mastery = data.get("champion_mastery") or {}
        # champion_mastery[<id>].last_play_time is ms since epoch. Take the
        # most recent across all champions as the summoner's last-played
        # signal (Riot's mastery service updates promptly at game end).
        last_ms = 0
        for entry in mastery.values():
            t = (entry or {}).get("last_play_time") or 0
            if t > last_ms:
                last_ms = t
        if last_ms > 0:
            out["last_played_at"] = int(last_ms // 1000)
        return out
    except httpx.HTTPError:
        return None
    finally:
        if own:
            await client.aclose()
