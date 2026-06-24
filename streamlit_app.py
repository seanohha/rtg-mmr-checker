"""RTG MMR Checker — Streamlit UI."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import plotly.graph_objects as go
import streamlit as st

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    """Naive datetime in Asia/Seoul, independent of server timezone."""
    return datetime.now(KST).replace(tzinfo=None)

from deeplol_stats import DEEPLOL_HEADERS, fetch_flex_stats
try:
    from deeplol_stats import fetch_live_status
except ImportError:
    # Defensive: if the deployed deeplol_stats is from an older revision that
    # doesn't define fetch_live_status, fall back to a no-op so the rest of
    # the app still loads.
    fetch_live_status = None  # type: ignore
import gist_storage
from history import append_record, group_by_summoner, read_history
from mmr_fetcher import DEFAULT_HEADERS, fetch_mmr

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DEEPLOL_STATS_PATH = ROOT / "deeplol_stats.json"

PALETTE = [
    "#f59e0b", "#3b82f6", "#10b981", "#ef4444", "#a855f7",
    "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#6366f1",
    "#14b8a6", "#eab308", "#8b5cf6", "#22d3ee",
]


def color_for(idx: int) -> str:
    return PALETTE[idx % len(PALETTE)]


# Per-owner hues in rainbow order: red → orange → yellow → green → blue → purple.
# Each tuple is (hue°, saturation%) — lightness is varied per summoner based
# on MMR rank within the owner.
OWNER_HUES: list[tuple[int, int]] = [
    (358, 78),  # red
    (28, 92),   # orange
    (48, 90),   # yellow
    (140, 60),  # green
    (215, 78),  # blue
    (278, 60),  # purple
]
# Fallback for >6 owners.
OWNER_HUE_FALLBACK = (200, 10)  # neutral gray-blue


def owner_summoner_color(owner_rank: int, mmr_rank: int, total: int) -> str:
    """Return an HSL color for a summoner, shaded by their MMR rank inside
    the owner's group. owner_rank picks the hue; mmr_rank=0 (highest MMR)
    gets the darkest/most-saturated shade, total-1 gets the lightest."""
    h, base_s = (
        OWNER_HUES[owner_rank]
        if 0 <= owner_rank < len(OWNER_HUES)
        else OWNER_HUE_FALLBACK
    )
    if total <= 1:
        return f"hsl({h}, {base_s}%, 55%)"
    # t=0 for top-MMR (deep, saturated), t=1 for bottom (light, pale).
    t = mmr_rank / (total - 1)
    lightness = 35.0 + t * 45.0          # 35% → 80%
    saturation = max(35.0, base_s - t * 22.0)  # base → ~base-22, floor 35
    return f"hsl({h}, {saturation:.0f}%, {lightness:.0f}%)"


def with_alpha(hex_color: str, alpha: float) -> str:
    """Accept either a #rrggbb hex or an hsl(...) string and return an
    rgba/hsla string with the given alpha."""
    if hex_color.startswith("hsl("):
        return "hsla" + hex_color[3:-1] + f", {alpha})"
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


@st.cache_data(ttl=60)
def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def history_path() -> str:
    cfg = load_config()
    return str(ROOT / cfg.get("log_file", "mmr_history.csv"))


@st.cache_data(ttl=60, show_spinner=False)
def _hydrate_from_gist_once() -> dict:
    """Pull persistent state from the configured gist into local files.
    cache_data(ttl=60) re-runs at most once per minute, so the container
    picks up updates pushed by other sessions without waiting for a redeploy.
    """
    if not gist_storage.configured():
        return {"configured": False}
    h_ok, s_ok = gist_storage.hydrate_from_gist(
        history_path(), str(DEEPLOL_STATS_PATH)
    )
    return {"configured": True, "history_loaded": h_ok, "stats_loaded": s_ok}


def push_state_to_gist() -> bool:
    """Push local state to gist. Merges with whatever's already in the gist
    first so concurrent sessions don't clobber each other's appends."""
    if not gist_storage.configured():
        return False
    return gist_storage.push_with_merge(history_path(), str(DEEPLOL_STATS_PATH))


def get_summoners() -> list[dict]:
    return load_config()["summoners"]


def fetch_one_sync(summoner: dict, with_mmr: bool = True):
    """Fetch deeplol info (always) + optionally MMR. Returns (mmr_result_or_None,
    info_dict_or_None). When with_mmr=False the rankedkings call is skipped
    entirely — useful for throttled refreshes where we still want fresh
    last_played_at / level / flex stats from deeplol."""
    async def go():
        async with (
            httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0) as mmr_client,
            httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=30.0) as dl_client,
        ):
            stats_task = asyncio.create_task(fetch_flex_stats(summoner, client=dl_client))
            if not with_mmr:
                stats = await stats_task
                return None, stats
            mmr_task = asyncio.create_task(fetch_mmr(summoner, client=mmr_client))
            return await asyncio.gather(mmr_task, stats_task)
    return asyncio.run(go())


def load_deeplol_stats() -> dict:
    if not DEEPLOL_STATS_PATH.exists():
        return {}
    try:
        return json.loads(DEEPLOL_STATS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_deeplol_stats(data: dict) -> None:
    DEEPLOL_STATS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_deeplol_stats(summoner: dict, info: dict | None) -> None:
    """Persist the deeplol entry. `info` is a dict (level + optional flex
    fields) or None when the lookup itself failed."""
    data = load_deeplol_stats()
    key = f"{summoner['name']}#{summoner['tag']}"
    if not info:
        # Lookup failed entirely — drop any stale entry.
        if key in data:
            del data[key]
            save_deeplol_stats(data)
        return
    data[key] = {
        **info,
        "updated_at": now_kst().isoformat(timespec="seconds"),
    }
    save_deeplol_stats(data)


def record_if_ok(summoner: dict, result, stats=None) -> None:
    """Persist MMR (CSV) and deeplol info (JSON). `result` may be None when
    the MMR fetch was skipped by the throttle — only the deeplol entry is
    updated in that case."""
    if result is not None and result.ok and result.mmr is not None:
        append_record(
            history_path(),
            summoner,
            result.mmr,
            now_kst(),
            rank=result.rank,
            actual_mmr=result.actual_mmr,
            actual_rank=result.actual_rank,
        )
    update_deeplol_stats(summoner, stats)


def parse_history_for_chart(rows: list[dict]) -> tuple[list[str], list[int]]:
    xs, ys = [], []
    for r in rows:
        mmr = r.get("mmr", "").strip()
        if not mmr:
            continue
        try:
            ys.append(int(mmr))
            xs.append(r["timestamp"])
        except ValueError:
            continue
    return xs, ys


def render_combined_chart(
    summoners: list[dict],
    grouped: dict[str, list[dict]],
    x_range: tuple[str, str] | None = None,
    color_map: dict[str, str] | None = None,
):
    fig = go.Figure()

    def _latest_mmr(s: dict) -> int:
        rows = grouped.get(f"{s['name']}#{s['tag']}", [])
        for r in reversed(rows):
            raw = (r.get("mmr") or "").strip()
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    continue
        return -1

    # Stable index preserved so palette lookups still match the original
    # config order if the caller didn't supply color_map.
    sorted_summoners = sorted(
        enumerate(summoners),
        key=lambda pair: _latest_mmr(pair[1]),
        reverse=True,
    )
    for i, s in sorted_summoners:
        key = f"{s['name']}#{s['tag']}"
        xs, ys = parse_history_for_chart(grouped.get(key, []))
        c = (color_map or {}).get(key) or color_for(i)
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines+markers",
                name=key,
                line=dict(color=c, width=2),
                marker=dict(size=8, color=c, line=dict(color="#0f1419", width=1)),
                hovertemplate="%{x}<br>%{y} MMR<extra>" + key + "</extra>",
            )
        )
    xaxis_kwargs: dict = {"title": ""}
    if x_range is not None:
        xaxis_kwargs["range"] = list(x_range)
    fig.update_layout(
        template="plotly_dark",
        height=420,
        margin=dict(l=40, r=40, t=20, b=40),
        xaxis=xaxis_kwargs,
        yaxis=dict(
            title="MMR",
            showgrid=True,
            dtick=50,
            gridcolor="rgba(255,255,255,0.18)",
            gridwidth=1,
            minor=dict(
                showgrid=True,
                dtick=10,
                gridcolor="rgba(255,255,255,0.05)",
                gridwidth=1,
            ),
        ),
        legend=dict(orientation="v", x=1.02, xanchor="left", y=1, yanchor="top"),
        hovermode="closest",
    )
    return fig


def render_sparkline(rows: list[dict], color: str):
    xs, ys = parse_history_for_chart(rows)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs or [0],
            y=ys or [0],
            mode="lines+markers",
            line=dict(color=color, width=2),
            marker=dict(size=4, color=color, line=dict(color="#0f1419", width=1)),
            fill="tozeroy" if ys else None,
            fillcolor=with_alpha(color, 0.2) if ys else None,
            hovertemplate="%{x}<br>%{y} MMR<extra></extra>",
        )
    )

    annotations = []
    y_pad = 5
    if ys:
        max_v = max(ys)
        min_v = min(ys)
        max_idx = ys.index(max_v)
        min_idx = ys.index(min_v)
        annotations.append(dict(
            x=xs[max_idx], y=max_v, text=str(max_v),
            showarrow=False, yshift=8, xanchor="center",
            font=dict(size=9, color="#e8eef5"),
        ))
        if max_v != min_v:
            annotations.append(dict(
                x=xs[min_idx], y=min_v, text=str(min_v),
                showarrow=False, yshift=-8, xanchor="center",
                font=dict(size=9, color="#8a96a8"),
            ))
            y_pad = max(8, (max_v - min_v) * 0.25)

    fig.update_layout(
        template="plotly_dark",
        height=80,
        margin=dict(l=4, r=4, t=10, b=10),
        showlegend=False,
        xaxis=dict(visible=False),
        yaxis=dict(
            visible=False,
            range=[min(ys) - y_pad, max(ys) + y_pad] if ys else [0, 1],
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        annotations=annotations,
    )
    return fig


def format_seconds(s: float) -> str:
    s = int(round(max(0, s)))
    if s < 60:
        return f"{s}초"
    return f"{s // 60}분 {s % 60}초" if s % 60 else f"{s // 60}분"


def relative_time_kor(ts_epoch: int | float | None) -> str | None:
    """'5분 전' / '2시간 전' / '3일 전' / '2주 전' etc. None if input is None."""
    if not ts_epoch:
        return None
    try:
        ts = datetime.fromtimestamp(int(ts_epoch), tz=KST).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return None
    diff = (now_kst() - ts).total_seconds()
    if diff < 0:
        return "방금"
    if diff < 60:
        return "방금"
    if diff < 3600:
        return f"{int(diff // 60)}분 전"
    if diff < 86400:
        return f"{int(diff // 3600)}시간 전"
    if diff < 86400 * 7:
        return f"{int(diff // 86400)}일 전"
    if diff < 86400 * 30:
        return f"{int(diff // (86400 * 7))}주 전"
    if diff < 86400 * 365:
        return f"{int(diff // (86400 * 30))}달 전"
    return f"{int(diff // (86400 * 365))}년 전"


# ---- UI -------------------------------------------------------------

st.set_page_config(page_title="RTG MMR Checker", page_icon="🎮", layout="wide")

# Slim down default Streamlit padding so the layout feels denser.
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    div[data-testid="stMetricValue"] { font-size: 28px; color: #f59e0b; }
    div[data-testid="column"] > div { gap: 0.25rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

_hydrate_from_gist_once()  # Pulls remote state into local files on first run.
summoners = get_summoners()
hist_rows = read_history(history_path())
grouped = group_by_summoner(hist_rows)
deeplol_all = load_deeplol_stats()


def _last_mmr(s: dict) -> int:
    """Latest recorded MMR for sorting; missing data sorts last."""
    rows = grouped.get(f"{s['name']}#{s['tag']}", [])
    if not rows:
        return -1
    raw = (rows[-1].get("mmr") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return -1


# Group summoners by owner; within each owner sort by MMR desc.
by_owner: dict[str, list[tuple[int, dict]]] = {}
for _idx, _s in enumerate(summoners):
    by_owner.setdefault(_s.get("owner", "(no owner)"), []).append((_idx, _s))
for _o in by_owner:
    by_owner[_o].sort(key=lambda t: _last_mmr(t[1]), reverse=True)

# Owner sections ordered by summoner count desc, then owner name.
ordered_owners = sorted(by_owner.keys(), key=lambda o: (-len(by_owner[o]), o))

# Build per-summoner color: owner hue + lightness shaded by MMR rank.
color_by_key: dict[str, str] = {}
for _owner_rank, _owner in enumerate(ordered_owners):
    _group = by_owner[_owner]
    for _mmr_rank, (_idx, _s) in enumerate(_group):
        _key = f"{_s['name']}#{_s['tag']}"
        color_by_key[_key] = owner_summoner_color(
            _owner_rank, _mmr_rank, len(_group)
        )


@st.cache_data(ttl=60, show_spinner="deeplol 정보 갱신 중…")
def refresh_deeplol_cached(
    summoner_tuples: tuple[tuple[str, str, str], ...]
) -> dict[str, dict]:
    """Pull fresh level / last_played / flex aggregate from deeplol for every
    summoner in parallel. Cached at 60s so each page load refreshes the
    visible info even without a manual Refresh click. Returns {key: info}.
    """
    async def go():
        async with httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=20.0) as c:
            tasks = [
                fetch_flex_stats(
                    {"name": n, "tag": t, "region": r}, client=c
                )
                for n, t, r in summoner_tuples
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, dict] = {}
        for (n, t, _r), info in zip(summoner_tuples, results):
            if isinstance(info, Exception) or not info:
                continue
            out[f"{n}#{t}"] = info
        return out
    return asyncio.run(go())


@st.cache_data(ttl=30, show_spinner=False)
def fetch_live_statuses_cached(puuid_region_pairs: tuple[tuple[str, str], ...]) -> dict[str, bool]:
    """Concurrently check live-game status for many summoners. TTL 30s so
    cards show fresh in-game state without hammering deeplol on every
    rerun. Returns {puu_id: bool}."""
    if fetch_live_status is None:
        return {}

    async def go():
        async with httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=10.0) as c:
            tasks = [fetch_live_status(p, r, client=c) for p, r in puuid_region_pairs]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, bool] = {}
        for (p, _), res in zip(puuid_region_pairs, results):
            out[p] = bool(res) and not isinstance(res, Exception)
        return out
    return asyncio.run(go())


# Auto-refresh deeplol level / last_played / flex aggregate on every page
# load (60s TTL). The result overlays the gist-hydrated deeplol_all so cards
# always show recent data without requiring a manual Refresh click.
_summoner_tuples = tuple((s["name"], s["tag"], s["region"]) for s in summoners)
_fresh_deeplol = refresh_deeplol_cached(_summoner_tuples)
for _k, _fresh in _fresh_deeplol.items():
    _existing = deeplol_all.get(_k, {})
    _merged = {**_existing, **_fresh}
    # Preserve cached last_played_at when fresh fetch transiently returns no
    # matches (deeplol sometimes serves an empty match list for a few seconds).
    if "last_played_at" not in _fresh and _existing.get("last_played_at"):
        _merged["last_played_at"] = _existing["last_played_at"]
    # Same defensive merge for flex aggregate fields — don't blank them out.
    if "games" not in _fresh:
        for _flex_key in ("games", "wins", "losses", "winrate",
                          "avg_kills", "avg_deaths", "avg_assists", "kda"):
            if _flex_key in _existing:
                _merged.setdefault(_flex_key, _existing[_flex_key])
    deeplol_all[_k] = _merged

# Build (puu_id, region) pairs for summoners that have a cached puu_id.
_pairs: list[tuple[str, str]] = []
for s in summoners:
    key = f"{s['name']}#{s['tag']}"
    entry = deeplol_all.get(key) or {}
    if entry.get("puu_id"):
        _pairs.append((entry["puu_id"], s["region"]))
live_by_puuid = (
    fetch_live_statuses_cached(tuple(_pairs)) if _pairs else {}
)

# Header row
header_left, header_right = st.columns([4, 1])
def _fmt_ts(ts: str) -> str:
    """Render an ISO timestamp from CSV as 'YYYY-MM-DD HH:MM' (KST)."""
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts


with header_left:
    st.title("RTG MMR Checker")
    if hist_rows:
        latest = max(r["timestamp"] for r in hist_rows)
        st.caption(
            f"Last update: {_fmt_ts(latest)} (KST)  ·  "
            f"예상 ~{format_seconds(len(summoners) * 5)}"
        )
    else:
        st.caption("No data yet")

with header_right:
    st.write("")  # vertical spacer
    refresh_all_clicked = st.button(
        "Refresh All", type="primary", use_container_width=True
    )

REFRESH_THROTTLE_MIN = 10


def _last_refresh_age_min(summoner: dict) -> float | None:
    """Minutes since the most recent CSV record for this summoner. None if no
    record exists or the timestamp can't be parsed."""
    rows = grouped.get(f"{summoner['name']}#{summoner['tag']}", [])
    if not rows:
        return None
    try:
        ts = datetime.fromisoformat(rows[-1]["timestamp"])
    except (ValueError, KeyError, TypeError):
        return None
    return (now_kst() - ts).total_seconds() / 60.0


# Refresh All flow
if refresh_all_clicked:
    fresh_summoners = []
    skipped: list[tuple[dict, float]] = []
    for s in summoners:
        age = _last_refresh_age_min(s)
        if age is not None and age < REFRESH_THROTTLE_MIN:
            skipped.append((s, age))
        else:
            fresh_summoners.append(s)

    if skipped and not fresh_summoners:
        st.info(
            f"{len(skipped)}명 모두 {REFRESH_THROTTLE_MIN}분 이내에 MMR 갱신됨 — "
            f"deeplol 정보(레벨/최근 플레이)만 갱신합니다."
        )
    elif skipped:
        st.info(
            f"{len(skipped)}명은 {REFRESH_THROTTLE_MIN}분 이내에 MMR 갱신되어 "
            f"deeplol 정보만 갱신 (나머지 {len(fresh_summoners)}명은 풀 갱신)"
        )
    progress = st.progress(0.0, text="Refreshing...")
    status = st.empty()
    start = time.time()
    ok_n = fail_n = skip_n = 0
    total = len(summoners)
    skipped_set = {f"{s['name']}#{s['tag']}" for s, _ in skipped}
    for i, s in enumerate(summoners):
        key = f"{s['name']}#{s['tag']}"
        is_throttled = key in skipped_set
        # Throttled summoners still get a deeplol-only fetch so level /
        # last_played_at update — only the rankedkings MMR call is skipped.
        result, stats = fetch_one_sync(s, with_mmr=not is_throttled)
        record_if_ok(s, result, stats)
        if is_throttled:
            skip_n += 1
        elif result and result.ok:
            ok_n += 1
        else:
            fail_n += 1
        elapsed = time.time() - start
        suffix = " (deeplol only)" if is_throttled else ""
        progress.progress(
            (i + 1) / total,
            text=f"({i+1}/{total}) {s['name']}#{s['tag']}{suffix}  ·  {format_seconds(elapsed)} 경과",
        )
    progress.empty()
    status.empty()
    elapsed = time.time() - start
    parts = [f"{ok_n} ok"]
    if fail_n:
        parts.append(f"{fail_n} failed")
    if skip_n:
        parts.append(f"{skip_n} deeplol-only")
    msg = f"Refresh complete ({format_seconds(elapsed)}): " + ", ".join(parts)
    (st.success if fail_n == 0 else st.warning)(msg)
    push_state_to_gist()
    st.rerun()

# Combined comparison chart
st.subheader("Combined comparison")

RANGE_OPTIONS = {
    "전체": None,
    "최근 3달": 90,
    "최근 1달": 30,
    "최근 1주": 7,
}
selected_range = st.segmented_control(
    "기간",
    options=list(RANGE_OPTIONS.keys()),
    default="전체",
    label_visibility="collapsed",
    key="combined_range",
)
range_days = RANGE_OPTIONS.get(selected_range or "전체")


def _filter_by_range(rows: list[dict], days: int | None) -> list[dict]:
    """Filter rows to the window. Include one anchor point from before the
    cutoff so the chart can draw a connecting line into the window (otherwise
    summoners with a single in-window point would render as lone dots)."""
    if days is None:
        return rows
    cutoff = (now_kst() - timedelta(days=days)).isoformat(timespec="seconds")
    in_range, before = [], []
    for r in rows:
        if (r.get("timestamp") or "") >= cutoff:
            in_range.append(r)
        else:
            before.append(r)
    if in_range and before:
        return [before[-1]] + in_range
    return in_range


grouped_for_chart = {k: _filter_by_range(v, range_days) for k, v in grouped.items()}
chart_x_range: tuple[str, str] | None = None
if range_days is not None:
    _now = now_kst()
    chart_x_range = (
        (_now - timedelta(days=range_days)).isoformat(timespec="seconds"),
        _now.isoformat(timespec="seconds"),
    )
st.plotly_chart(
    render_combined_chart(
        summoners,
        grouped_for_chart,
        x_range=chart_x_range,
        color_map=color_by_key,
    ),
    use_container_width=True,
)

# Per-owner cards — by_owner / ordered_owners / color_by_key already built above.
for owner in ordered_owners:
    group = by_owner[owner]
    st.markdown(f"##### {owner} ({len(group)})")
    cols_per_row = 4
    for row_start in range(0, len(group), cols_per_row):
        row = group[row_start : row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, (idx, s) in zip(cols, row):
            with col:
                with st.container(border=True):
                    key = f"{s['name']}#{s['tag']}"
                    rows = grouped.get(key, [])
                    last = rows[-1] if rows else None

                    info = deeplol_all.get(key) or {}
                    level = info.get("level")
                    level_badge = (
                        f" <span style='font-size:11px;color:#a0acbf;"
                        f"background:#1a2028;padding:1px 6px;border-radius:8px;"
                        f"margin-left:4px;'>Lv.{level}</span>"
                        if level
                        else ""
                    )
                    in_game = bool(
                        info.get("puu_id")
                        and live_by_puuid.get(info["puu_id"])
                    )
                    ingame_badge = (
                        " <span title='현재 게임 중' style='font-size:11px;"
                        "color:#fff;background:#ef4444;padding:1px 6px;"
                        "border-radius:8px;margin-left:4px;font-weight:600;'>"
                        "🔴 In Game</span>"
                        if in_game
                        else ""
                    )
                    # Only show "last played" when NOT currently in a game —
                    # the In Game badge already conveys recency.
                    last_played_html = ""
                    if not in_game:
                        rel = relative_time_kor(info.get("last_played_at"))
                        if rel:
                            last_played_html = (
                                f" <span title='마지막 게임 종료' "
                                f"style='font-size:11px;color:#a0acbf;"
                                f"background:#1a2028;padding:1px 6px;"
                                f"border-radius:8px;margin-left:4px;'>"
                                f"🕒 {rel}</span>"
                            )
                    st.markdown(
                        f"**{s['name']}**  `#{s['tag']}`"
                        f"{level_badge}{ingame_badge}{last_played_html}",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"{s['region']} · {s['queue_type']}")

                    if info.get("games"):
                        wr = info.get("winrate", 0)
                        kda = info.get("kda", 0)
                        wr_color = "#10b981" if wr >= 50 else "#ef4444"
                        kda_color = (
                            "#10b981" if kda >= 2.5
                            else "#ef4444" if kda < 1.5
                            else "#e8eef5"
                        )
                        st.markdown(
                            f"<div style='font-size:11px;color:#a0acbf;line-height:1.4;'>"
                            f"<span style='opacity:0.7;'>자유 </span>"
                            f"<span>{info['games']}전 </span>"
                            f"<span style='color:#10b981;'>{info['wins']}승</span> "
                            f"<span style='color:#ef4444;'>{info['losses']}패</span>"
                            f" · <span style='color:{wr_color};font-weight:600;'>{wr:.0f}%</span>"
                            f" · <span style='color:{kda_color};font-weight:600;'>{kda:.2f} KDA</span>"
                            f"<br><span style='opacity:0.65;'>"
                            f"{info['avg_kills']:.1f}/"
                            f"{info['avg_deaths']:.1f}/"
                            f"{info['avg_assists']:.1f}"
                            f"</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                    if last and last.get("mmr", "").strip():
                        c1, c2 = st.columns([1, 2])
                        with c1:
                            health_html = ""
                            try:
                                mmr_n = int(last["mmr"])
                                actual_n = int(last.get("actual_mmr", "").strip())
                                diff = mmr_n - actual_n
                                color = "#10b981" if diff >= 0 else "#ef4444"
                                sign = "+" if diff >= 0 else ""
                                health_html = (
                                    f"<span style='font-size:14px;color:{color};"
                                    f"font-weight:600;margin-left:6px;'>"
                                    f"({sign}{diff})</span>"
                                )
                            except (ValueError, AttributeError):
                                pass

                            # Delta against the latest *different* MMR — repeated identical
                                # refreshes don't reset the indicator, so users see actual
                                # MMR movement (e.g. ▲ +20) until the next real change.
                            delta_html = ""
                            try:
                                cur_mmr = int(last["mmr"])
                                prev_mmr = None
                                prev_ts = None
                                for r in reversed(rows[:-1]):
                                    raw = (r.get("mmr") or "").strip()
                                    if not raw:
                                        continue
                                    try:
                                        v = int(raw)
                                    except ValueError:
                                        continue
                                    if v != cur_mmr:
                                        prev_mmr = v
                                        prev_ts = r.get("timestamp")
                                        break
                                if prev_mmr is not None:
                                    d = cur_mmr - prev_mmr
                                    arrow = "▲" if d > 0 else "▼"
                                    dc = "#10b981" if d > 0 else "#ef4444"
                                    sgn = "+" if d > 0 else ""
                                    title = (
                                        f"directly previous {prev_mmr} at {prev_ts}"
                                        if prev_ts else ""
                                    )
                                    delta_html = (
                                        f"<div title='{title}' "
                                        f"style='font-size:11px;color:{dc};font-weight:600;'>"
                                        f"{arrow} {sgn}{d}</div>"
                                    )
                            except (ValueError, AttributeError, KeyError):
                                pass

                            card_color = color_by_key.get(key, color_for(idx))
                            st.markdown(
                                f"<div style='line-height:1;'>"
                                f"<span style='font-size:30px;font-weight:700;color:{card_color};'>{last['mmr']}</span>"
                                f"{health_html}"
                                f"</div>"
                                f"{delta_html}",
                                unsafe_allow_html=True,
                            )
                            if last.get("rank"):
                                st.caption(last["rank"])
                            if last.get("actual_mmr", "").strip() and last.get("actual_rank", "").strip():
                                st.markdown(
                                    f"<div style='font-size:13px;color:#cbd5e1;"
                                    f"font-weight:600;line-height:1.3;margin-top:2px;'>"
                                    f"<span style='font-size:11px;color:#8a96a8;"
                                    f"font-weight:400;'>actual </span>"
                                    f"<span>{last['actual_mmr']}</span>"
                                    f"<span style='color:#8a96a8;font-weight:400;'> · </span>"
                                    f"<span>{last['actual_rank']}</span>"
                                    f"</div>",
                                    unsafe_allow_html=True,
                                )
                        with c2:
                            st.plotly_chart(
                                render_sparkline(rows, color_by_key.get(key, color_for(idx))),
                                use_container_width=True,
                                config={"displayModeBar": False},
                                key=f"spark-{idx}",
                            )
                    else:
                        st.markdown(
                            "<div style='font-size:30px;color:#8a96a8;'>—</div>",
                            unsafe_allow_html=True,
                        )

                    btn_col, time_col = st.columns([1, 1])
                    with btn_col:
                        clicked = st.button("Refresh", key=f"r-{idx}")
                    with time_col:
                        if last:
                            st.caption(last["timestamp"])

                    if clicked:
                        age = _last_refresh_age_min(s)
                        throttled = age is not None and age < REFRESH_THROTTLE_MIN
                        with st.spinner(
                            f"Refreshing {key}..."
                            + (" (deeplol only)" if throttled else "")
                        ):
                            result, stats = fetch_one_sync(s, with_mmr=not throttled)
                            record_if_ok(s, result, stats)
                            push_state_to_gist()
                        if throttled:
                            st.info(
                                f"MMR은 {age:.1f}분 전 갱신되어 skip — "
                                f"deeplol 정보(레벨/최근 플레이)만 갱신됨."
                            )
                        elif result and result.ok:
                            st.success(f"MMR: {result.mmr}")
                        else:
                            err = (result and result.error) or "unknown error"
                            st.error(f"Failed: {err}")
                        st.rerun()
