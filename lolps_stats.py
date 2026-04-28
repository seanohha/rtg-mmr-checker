"""Pull recent-game aggregate stats from lol.ps for a summoner.

The summoner page (`https://lol.ps/summoner/{name}_{tag}?region={region}`) is
server-side rendered with the recent-match list embedded inline as JS-like
state. Each entry has match_id, champion_id, lane, win, kills, deaths, assists.
Parsing this is much cheaper than calling the per-match API endpoints.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import httpx

MATCH_RE = re.compile(
    r'\{match_id:"(KR_\d+|[A-Z]+\d?_\d+)",'
    r'champion_id:(\d+),'
    r'lane:"([^"]*)",'
    r"win:(\d),"
    r"kills:(\d+),"
    r"deaths:(\d+),"
    r"assists:(\d+),"
    r"timestamp:(\d+)\}"
)


@dataclass
class RecentStats:
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


def _build_url(summoner: dict) -> str:
    name_tag = f"{summoner['name']}_{summoner['tag']}"
    return (
        f"https://lol.ps/summoner/{urllib.parse.quote(name_tag)}"
        f"?region={summoner['region'].lower()}"
    )


def _aggregate(matches: list[dict]) -> RecentStats | None:
    # Filter out remakes (k=d=a=0 → short games / dodges)
    real = [m for m in matches if (m["kills"] + m["deaths"] + m["assists"]) > 0]
    if not real:
        return None
    games = len(real)
    wins = sum(1 for m in real if m["win"])
    sum_k = sum(m["kills"] for m in real)
    sum_d = sum(m["deaths"] for m in real)
    sum_a = sum(m["assists"] for m in real)
    return RecentStats(
        games=games,
        wins=wins,
        losses=games - wins,
        winrate=wins / games * 100,
        avg_kills=sum_k / games,
        avg_deaths=sum_d / games,
        avg_assists=sum_a / games,
        kda=(sum_k + sum_a) / max(1, sum_d),
    )


def parse_html(html: str) -> RecentStats | None:
    matches: list[dict] = []
    for m in MATCH_RE.finditer(html):
        matches.append(
            dict(
                match_id=m.group(1),
                champion_id=int(m.group(2)),
                lane=m.group(3),
                win=int(m.group(4)),
                kills=int(m.group(5)),
                deaths=int(m.group(6)),
                assists=int(m.group(7)),
                timestamp=int(m.group(8)),
            )
        )
    if not matches:
        return None
    return _aggregate(matches)


async def fetch_recent_stats(
    summoner: dict, client: httpx.AsyncClient | None = None
) -> RecentStats | None:
    url = _build_url(summoner)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html",
            },
            timeout=30.0,
            follow_redirects=True,
        )
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        return parse_html(r.text)
    except httpx.HTTPError:
        return None
    finally:
        if own_client:
            await client.aclose()
