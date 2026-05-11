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


def with_alpha(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


@st.cache_data(ttl=60)
def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def history_path() -> str:
    cfg = load_config()
    return str(ROOT / cfg.get("log_file", "mmr_history.csv"))


@st.cache_resource
def _hydrate_from_gist_once() -> dict:
    """Pull persistent state from the configured gist into local files exactly
    once per Streamlit session. Subsequent calls are no-ops thanks to caching.
    """
    if not gist_storage.configured():
        return {"configured": False}
    h_ok, s_ok = gist_storage.hydrate_from_gist(
        history_path(), str(DEEPLOL_STATS_PATH)
    )
    return {"configured": True, "history_loaded": h_ok, "stats_loaded": s_ok}


def push_state_to_gist() -> bool:
    if not gist_storage.configured():
        return False
    return gist_storage.push_local_files(history_path(), str(DEEPLOL_STATS_PATH))


def get_summoners() -> list[dict]:
    return load_config()["summoners"]


def fetch_one_sync(summoner: dict):
    """Fetch MMR + deeplol flex stats concurrently. Returns (mmr_result, stats_or_none)."""
    async def go():
        # Two clients: each uses different default headers.
        async with (
            httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0) as mmr_client,
            httpx.AsyncClient(headers=DEEPLOL_HEADERS, timeout=30.0) as dl_client,
        ):
            mmr_task = asyncio.create_task(fetch_mmr(summoner, client=mmr_client))
            stats_task = asyncio.create_task(fetch_flex_stats(summoner, client=dl_client))
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


def update_deeplol_stats(summoner: dict, stats) -> None:
    if stats is None:
        return
    data = load_deeplol_stats()
    key = f"{summoner['name']}#{summoner['tag']}"
    data[key] = {
        **stats.to_dict(),
        "updated_at": now_kst().isoformat(timespec="seconds"),
    }
    save_deeplol_stats(data)


def record_if_ok(summoner: dict, result, stats=None) -> None:
    if result.ok and result.mmr is not None:
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
):
    fig = go.Figure()
    for i, s in enumerate(summoners):
        key = f"{s['name']}#{s['tag']}"
        xs, ys = parse_history_for_chart(grouped.get(key, []))
        c = color_for(i)
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

    if not fresh_summoners:
        # Everyone was just refreshed by another client — bail out.
        oldest_age = max(a for _, a in skipped) if skipped else 0
        st.info(
            f"{len(skipped)}명 모두 {REFRESH_THROTTLE_MIN}분 이내에 갱신됨 "
            f"(가장 오래된 기록도 {oldest_age:.1f}분 전). "
            f"잠시 후 다시 시도하세요."
        )
    else:
        if skipped:
            st.info(
                f"{len(skipped)}명은 {REFRESH_THROTTLE_MIN}분 이내에 갱신되어 skip "
                f"(나머지 {len(fresh_summoners)}명만 갱신)"
            )
        progress = st.progress(0.0, text="Refreshing...")
        status = st.empty()
        start = time.time()
        ok_n = fail_n = 0
        total = len(fresh_summoners)
        for i, s in enumerate(fresh_summoners):
            result, stats = fetch_one_sync(s)
            record_if_ok(s, result, stats)
            if result.ok:
                ok_n += 1
            else:
                fail_n += 1
            elapsed = time.time() - start
            progress.progress(
                (i + 1) / total,
                text=f"({i+1}/{total}) {s['name']}#{s['tag']}  ·  {format_seconds(elapsed)} 경과",
            )
        progress.empty()
        status.empty()
        elapsed = time.time() - start
        msg = f"Refresh complete ({format_seconds(elapsed)}): {ok_n} ok, {fail_n} failed"
        (st.success if fail_n == 0 else st.warning)(msg)
        push_state_to_gist()
        # Rerun to pick up new history rows
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
    render_combined_chart(summoners, grouped_for_chart, x_range=chart_x_range),
    use_container_width=True,
)

# Per-owner cards
OWNER_ORDER = ["Sean", "함팀장님", "Wallace", "Motaju", "Michael", "Dani"]


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


def _owner_key(owner: str) -> tuple[int, str]:
    try:
        return (OWNER_ORDER.index(owner), "")
    except ValueError:
        return (len(OWNER_ORDER), owner)


by_owner: dict[str, list[tuple[int, dict]]] = {}
for idx, s in enumerate(summoners):
    by_owner.setdefault(s.get("owner", "(no owner)"), []).append((idx, s))

# Sort summoners within each owner by latest MMR desc.
for owner in by_owner:
    by_owner[owner].sort(key=lambda t: _last_mmr(t[1]), reverse=True)

for owner in sorted(by_owner.keys(), key=_owner_key):
    group = by_owner[owner]
    st.markdown(f"##### {owner}")
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

                    st.markdown(f"**{s['name']}**  `#{s['tag']}`")
                    st.caption(f"{s['region']} · {s['queue_type']}")

                    flex = deeplol_all.get(key)
                    if flex:
                        wr = flex.get("winrate", 0)
                        kda = flex.get("kda", 0)
                        wr_color = "#10b981" if wr >= 50 else "#ef4444"
                        kda_color = (
                            "#10b981" if kda >= 2.5
                            else "#ef4444" if kda < 1.5
                            else "#e8eef5"
                        )
                        st.markdown(
                            f"<div style='font-size:11px;color:#a0acbf;line-height:1.4;'>"
                            f"<span style='opacity:0.7;'>자유 </span>"
                            f"<span>{flex['games']}전 </span>"
                            f"<span style='color:#10b981;'>{flex['wins']}승</span> "
                            f"<span style='color:#ef4444;'>{flex['losses']}패</span>"
                            f" · <span style='color:{wr_color};font-weight:600;'>{wr:.0f}%</span>"
                            f" · <span style='color:{kda_color};font-weight:600;'>{kda:.2f} KDA</span>"
                            f"<br><span style='opacity:0.65;'>"
                            f"{flex['avg_kills']:.1f}/"
                            f"{flex['avg_deaths']:.1f}/"
                            f"{flex['avg_assists']:.1f}"
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

                            st.markdown(
                                f"<div style='line-height:1;'>"
                                f"<span style='font-size:30px;font-weight:700;color:{color_for(idx)};'>{last['mmr']}</span>"
                                f"{health_html}"
                                f"</div>"
                                f"{delta_html}",
                                unsafe_allow_html=True,
                            )
                            if last.get("rank"):
                                st.caption(last["rank"])
                            if last.get("actual_mmr", "").strip() and last.get("actual_rank", "").strip():
                                st.markdown(
                                    f"<span style='font-size:11px;color:#8a96a8;opacity:0.75;'>actual: {last['actual_mmr']} · {last['actual_rank']}</span>",
                                    unsafe_allow_html=True,
                                )
                        with c2:
                            st.plotly_chart(
                                render_sparkline(rows, color_for(idx)),
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
                        if age is not None and age < REFRESH_THROTTLE_MIN:
                            st.info(
                                f"{age:.1f}분 전에 갱신됨. "
                                f"{REFRESH_THROTTLE_MIN}분 이내에는 skip합니다."
                            )
                        else:
                            with st.spinner(f"Refreshing {key}..."):
                                result, stats = fetch_one_sync(s)
                                record_if_ok(s, result, stats)
                                push_state_to_gist()
                            if result.ok:
                                st.success(f"MMR: {result.mmr}")
                            else:
                                st.error(f"Failed: {result.error}")
                            st.rerun()
