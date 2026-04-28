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

# queue_type values used by deeplol's match/matches endpoint
QUEUE_FLEX = "ranked_flex_sr"
QUEUE_SOLO = "ranked_solo_5x5"


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


async def _resolve_puuid(
    summoner: dict, client: httpx.AsyncClient
) -> str | None:
    url = f"{API_BASE}/summoner/summoner"
    params = {
        "riot_id_name": summoner["name"],
        "riot_id_tag_line": summoner["tag"],
        "platform_id": summoner["region"].upper(),
    }
    r = await client.get(url, params=params)
    if r.status_code != 200:
        return None
    info = (r.json() or {}).get("summoner_basic_info_dict") or {}
    return info.get("puu_id") or None


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


async def fetch_flex_stats(
    summoner: dict,
    client: httpx.AsyncClient | None = None,
    count: int = 20,
) -> FlexStats | None:
    """Fetch recent ranked-flex stats for one summoner. Returns None on failure."""
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=30.0)
    try:
        puuid = await _resolve_puuid(summoner, client)
        if not puuid:
            return None
        matches = await _fetch_matches(
            puuid, summoner["region"], QUEUE_FLEX, count, client
        )
        return _aggregate(matches, puuid)
    except httpx.HTTPError:
        return None
    finally:
        if own:
            await client.aclose()
