"""RTG MMR Checker — Streamlit UI."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import plotly.graph_objects as go
import streamlit as st

from history import append_record, group_by_summoner, read_history
from mmr_fetcher import DEFAULT_HEADERS, fetch_mmr

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"

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


def get_summoners() -> list[dict]:
    return load_config()["summoners"]


def fetch_one_sync(summoner: dict):
    """Run async fetch in sync context."""
    async def go():
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0) as client:
            return await fetch_mmr(summoner, client=client)
    return asyncio.run(go())


def record_if_ok(summoner: dict, result) -> None:
    if result.ok and result.mmr is not None:
        append_record(
            history_path(),
            summoner,
            result.mmr,
            datetime.now(),
            rank=result.rank,
            actual_mmr=result.actual_mmr,
            actual_rank=result.actual_rank,
        )


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


def render_combined_chart(summoners: list[dict], grouped: dict[str, list[dict]]):
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
    fig.update_layout(
        template="plotly_dark",
        height=420,
        margin=dict(l=40, r=40, t=20, b=40),
        yaxis_title="MMR",
        xaxis_title="",
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
    fig.update_layout(
        template="plotly_dark",
        height=70,
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=False,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, range=[min(ys) - 5, max(ys) + 5] if ys else [0, 1]),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
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

summoners = get_summoners()
hist_rows = read_history(history_path())
grouped = group_by_summoner(hist_rows)

# Header row
header_left, header_right = st.columns([4, 1])
with header_left:
    st.title("RTG MMR Checker")
    if hist_rows:
        latest = max(r["timestamp"] for r in hist_rows)
        st.caption(f"Last update: {latest}  ·  예상 ~{format_seconds(len(summoners) * 5)}")
    else:
        st.caption("No data yet")

with header_right:
    st.write("")  # vertical spacer
    refresh_all_clicked = st.button(
        "Refresh All", type="primary", use_container_width=True
    )

# Refresh All flow
if refresh_all_clicked:
    progress = st.progress(0.0, text="Refreshing...")
    status = st.empty()
    start = time.time()
    ok_n = fail_n = 0
    for i, s in enumerate(summoners):
        result = fetch_one_sync(s)
        record_if_ok(s, result)
        if result.ok:
            ok_n += 1
        else:
            fail_n += 1
        elapsed = time.time() - start
        progress.progress(
            (i + 1) / len(summoners),
            text=f"({i+1}/{len(summoners)}) {s['name']}#{s['tag']}  ·  {format_seconds(elapsed)} 경과",
        )
    progress.empty()
    status.empty()
    elapsed = time.time() - start
    msg = f"Refresh complete ({format_seconds(elapsed)}): {ok_n} ok, {fail_n} failed"
    (st.success if fail_n == 0 else st.warning)(msg)
    # Rerun to pick up new history rows
    st.rerun()

# Combined comparison chart
st.subheader("Combined comparison")
st.plotly_chart(render_combined_chart(summoners, grouped), use_container_width=True)

# Per-owner cards
by_owner: dict[str, list[tuple[int, dict]]] = {}
for idx, s in enumerate(summoners):
    by_owner.setdefault(s.get("owner", "(no owner)"), []).append((idx, s))

for owner, group in by_owner.items():
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

                    if last and last.get("mmr", "").strip():
                        c1, c2 = st.columns([1, 2])
                        with c1:
                            st.markdown(
                                f"<div style='font-size:30px;font-weight:700;color:{color_for(idx)};line-height:1;'>{last['mmr']}</div>",
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
                        with st.spinner(f"Refreshing {key}..."):
                            result = fetch_one_sync(s)
                            record_if_ok(s, result)
                        if result.ok:
                            st.success(f"MMR: {result.mmr}")
                        else:
                            st.error(f"Failed: {result.error}")
                        st.rerun()
