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
