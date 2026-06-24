"""Pull recent flex (자유랭크) stats for a summoner from deeplol.gg's public API.

Two-step flow per summoner:
  1) GET /summoner/summoner?riot_id_name={name}&riot_id_tag_line={tag}&platform_id={region}
       → returns puu_id (and account level, etc.)
  2) GET /match/matches?puu_id=...&platform_id={region}&queue_type=ranked_flex_sr
                       &champion_id=0&offset=0&count=20&only_list=0&last_updated_at={now_ms}
       → returns match_json_list. Each match has participants_list, find ours by puu_id,
         and pull kills/deaths/assists from final_stat_dict.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass

import httpx

API_BASE = "https://b2c-api-cdn.deeplol.gg"
DEEPLOL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.deeplol.gg",
    "Referer": "https://www.deeplol.gg/",
    "X-DEEPLOL-SECRET": "false",
}

# queue_type values used by deeplol's match/matches endpoint.
# NB: deeplol seems to ignore this query param and return matches from every
# queue regardless, so we still have to filter by Riot's queue_id below.
QUEUE_FLEX = "ranked_flex_sr"
QUEUE_SOLO = "ranked_solo_5x5"

# Riot queue IDs we care about
QUEUE_ID_FLEX = 440  # 5v5 Ranked Flex SR


@dataclass
class FlexStats:
    games: int
    wins: int
    losses: int
    winrate: float
    avg_kills: float
    avg_deaths: float
    avg_assists: float
    kda: float

    def to_dict(self) -> dict:
        return {
            "games": self.games,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(self.winrate, 1),
            "avg_kills": round(self.avg_kills, 1),
            "avg_deaths": round(self.avg_deaths, 1),
            "avg_assists": round(self.avg_assists, 1),
            "kda": round(self.kda, 2),
        }


async def _resolve_summoner(
    summoner: dict, client: httpx.AsyncClient
) -> tuple[str | None, int | None]:
    """Look up a summoner on deeplol. Returns (puuid, level)."""
    url = f"{API_BASE}/summoner/summoner"
    params = {
        "riot_id_name": summoner["name"],
        "riot_id_tag_line": summoner["tag"],
        "platform_id": summoner["region"].upper(),
    }
    r = await client.get(url, params=params)
    if r.status_code != 200:
        return None, None
    info = (r.json() or {}).get("summoner_basic_info_dict") or {}
    return info.get("puu_id") or None, info.get("level")


async def _fetch_matches(
    puuid: str, region: str, queue_type: str, count: int, client: httpx.AsyncClient
) -> list[dict]:
    url = f"{API_BASE}/match/matches"
    params = {
        "puu_id": puuid,
        "platform_id": region.upper(),
        "queue_type": queue_type,
        "champion_id": "0",
        "offset": "0",
        "count": str(count),
        "only_list": "0",
        "last_updated_at": str(int(time.time() * 1000)),
    }
    r = await client.get(url, params=params)
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("match_json_list") or []


def _aggregate(matches: list[dict], puuid: str) -> FlexStats | None:
    games = wins = 0
    sum_k = sum_d = sum_a = 0
    for m in matches:
        basic = m.get("match_basic_dict") or {}
        if basic.get("is_remake"):
            continue
        # deeplol's queue_type query param doesn't actually filter, so enforce
        # Ranked Flex here. Without this, non-flex queues (Swift Play 480,
        # Solo 420, Normals, ARAM, etc.) would leak into the aggregate.
        if basic.get("queue_id") != QUEUE_ID_FLEX:
            continue
        my = next(
            (p for p in m.get("participants_list") or [] if p.get("puu_id") == puuid),
            None,
        )
        if my is None:
            continue
        stats = my.get("final_stat_dict") or {}
        games += 1
        if my.get("is_win"):
            wins += 1
        sum_k += int(stats.get("kills", 0) or 0)
        sum_d += int(stats.get("deaths", 0) or 0)
        sum_a += int(stats.get("assists", 0) or 0)
    if games == 0:
        return None
    return FlexStats(
        games=games,
        wins=wins,
        losses=games - wins,
        winrate=wins / games * 100,
        avg_kills=sum_k / games,
        avg_deaths=sum_d / games,
        avg_assists=sum_a / games,
        kda=(sum_k + sum_a) / max(1, sum_d),
    )


async def fetch_summoner_info(
    summoner: dict,
    client: httpx.AsyncClient | None = None,
    count: int = 50,
) -> dict | None:
    """Fetch deeplol level + recent ranked-flex aggregate. Returns:
      - None if the deeplol lookup failed (account not found)
      - {"puu_id": str, "level": int, [optional flex fields...]} otherwise

    A summoner with no recent flex games still returns level + puu_id so
    the card can show their account level / live-status even when the
    flex section is empty.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=30.0)
    try:
        puuid, level = await _resolve_summoner(summoner, client)
        if not puuid:
            return None
        out: dict = {"puu_id": puuid}
        if level is not None:
            out["level"] = level
        matches = await _fetch_matches(
            puuid, summoner["region"], QUEUE_FLEX, count, client
        )
        # Last-played: max (creation_timestamp + game_duration) across ALL
        # fetched matches, regardless of queue — i.e. when the most recent
        # game ENDED, which is what deeplol's own web UI shows. Using start
        # time alone was off by ~30-40 min for typical Summoner's Rift
        # games. Both fields are in unix seconds.
        last_played = None
        for m in matches:
            mb = m.get("match_basic_dict") or {}
            ct = mb.get("creation_timestamp")
            if ct is None:
                continue
            dur = mb.get("game_duration") or 0
            end_ts = int(ct) + int(dur)
            if last_played is None or end_ts > last_played:
                last_played = end_ts
        if last_played is not None:
            out["last_played_at"] = last_played
        flex = _aggregate(matches, puuid)
        if flex is not None:
            out.update(flex.to_dict())
        return out or None
    except httpx.HTTPError:
        return None
    finally:
        if own:
            await client.aclose()


INGAME_WORKER_URL = "https://ingame-check.deeplol-gg.workers.dev/"


async def fetch_live_status(
    puuid: str, region: str, client: httpx.AsyncClient | None = None
) -> dict | None:
    """Check whether a summoner is currently in a live game via deeplol's
    ingame-check Cloudflare worker. The worker proxies Riot's spectator-v5
    API: a 200 response with a `gameId` field means in-game; anything else
    (404-like body, errors, missing gameId) means offline. Returns the
    parsed JSON when in-game, None otherwise.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=10.0)
    try:
        # POST multipart with puu_id + platform_id, matching how deeplol's
        # frontend calls the worker.
        r = await client.post(
            INGAME_WORKER_URL,
            data={"puu_id": puuid, "platform_id": region.upper()},
        )
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        # Worker returns a `gameId` only when the player is in a live game;
        # offline responses come back as {"status": {"status_code": 500, ...}}.
        if isinstance(data, dict) and data.get("gameId"):
            return data
        return None
    except httpx.HTTPError:
        return None
    finally:
        if own:
            await client.aclose()


# Backwards-compat alias used elsewhere in the project.
fetch_flex_stats = fetch_summoner_info
