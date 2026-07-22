#!/usr/bin/env python3
"""
Streamlit dashboard for lab health monitoring — PostgreSQL v4.

Layout:
  Header                 — title, live badge, last-refresh time
  Filters                — server + date range, applied on "Go"
  Threshold Alerts       — breach summary cards (click the table row for details)
  KPI Summary            — CPU, RAM, Disk, Latency current value + donut charts
  KPI Trends             — CPU %, RAM %, Disk % time-series (3 columns)
  Latency & App Metrics  — Latency card is a 3-view carousel (◀ ▶ arrows):
                            Net & Disk latency → Mesh ping summary table →
                            Mesh ping avg latency by route. All
                            three follow the sidebar's selected machine
                            (as mesh ping source) and date range. App
                            Metrics (uptime table) sits stacked below.
  Dynamic metrics        — any custom metrics registered by Hermes
  Detailed Records       — one merged metrics log (all records, selected server)
                            + breach events + app metric samples
                            + raw mesh ping results, as tabs

Design:
  - Single machine selection in the sidebar. All charts reflect the chosen machine.
  - Filter changes are applied only after pressing the "Go" button.
  - Every chart has a horizontal scrollbar (Plotly xaxis rangeslider).
  - Data is cached with a 30 s TTL for auto‑refresh.

Expected PostgreSQL tables (schema.sql / new_schema_postgres.sql):
  - machines, metric_samples, events, mesh_ping_results
"""

from __future__ import annotations
import os

import html as html_lib
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

import re
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Lab Health Dashboard (PG)",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Inline Mesh Ping Results Dashboard ---
def render_mesh_ping_results_dashboard():
    import os
    import pandas as pd
    import plotly.graph_objects as go
    import psycopg2
    import streamlit as st

    db_config = {
        "host": os.getenv("P1_DB_HOST", os.getenv("DB_HOST", "localhost")),
        "port": int(os.getenv("P1_DB_PORT", os.getenv("DB_PORT", "5432"))),
        "dbname": os.getenv("P1_DB_NAME", os.getenv("DB_NAME", "lab_monitoring_db")),
        "user": os.getenv("P1_DB_USER", os.getenv("DB_USER", "release_user")),
        "password": os.getenv("P1_DB_PASSWORD", os.getenv("DB_PASSWORD", "release_password")),
    }

    def qident(name):
        return '"' + str(name).replace('"', '""') + '"'

    def get_columns(conn, table_name):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            return [row[0] for row in cur.fetchall()]

    def pick_column(columns, candidates, required=True):
        lower_map = {c.lower(): c for c in columns}
        for candidate in candidates:
            if candidate.lower() in lower_map:
                return lower_map[candidate.lower()]
        if required:
            raise RuntimeError(
                "Could not find any of these columns in mesh_ping_results: "
                + ", ".join(candidates)
                + ". Actual columns: "
                + ", ".join(columns)
            )
        return None

    def table_exists(conn, table_name):
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
            return cur.fetchone()[0] is not None

    def load_mesh_ping_results(time_window_interval):
        table_name = "mesh_ping_results"

        with psycopg2.connect(**db_config) as conn:
            columns = get_columns(conn, table_name)

            if not columns:
                raise RuntimeError("Table mesh_ping_results does not exist or has no columns.")

            time_col = pick_column(
                columns,
                ["ts", "timestamp", "created_at", "checked_at", "sample_time", "time", "created_on"],
                required=False,
            )
            source_col = pick_column(
                columns,
                ["source_server_id", "source_machine_id", "source_id", "host_server_id", "host_machine_id", "server_id", "source"],
                required=True,
            )
            target_col = pick_column(
                columns,
                ["target_server_id", "target_machine_id", "target_id", "remote_server_id", "target", "target_ip", "destination_server_id"],
                required=True,
            )
            latency_col = pick_column(
                columns,
                ["latency_ms", "avg_latency_ms", "rtt_ms", "ping_latency_ms", "response_time_ms"],
                required=True,
            )
            success_col = pick_column(
                columns,
                ["success", "is_success", "ping_success", "reachable", "status"],
                required=False,
            )
            source_alias_col = pick_column(
                columns, ["source_alias", "source_name", "source_hostname"], required=False,
            )
            target_alias_col = pick_column(
                columns, ["target_alias", "target_name", "target_hostname"], required=False,
            )
            target_ip_col = pick_column(columns, ["target_ip"], required=False)

            # The `machines` lookup table is optional enrichment, not a hard
            # dependency: a freshly restored mesh_ping_results dump (e.g. from
            # the demo SQL export) won't have it, and the panel should still
            # render using the alias columns already on mesh_ping_results.
            machines_available = table_exists(conn, "machines")

            ts_expr = f"r.{qident(time_col)}" if time_col else "NOW()"
            if time_col and time_window_interval:
                where_clause = f"WHERE r.{qident(time_col)} >= NOW() - INTERVAL '{time_window_interval}'"
            else:
                where_clause = ""

            if success_col:
                success_expr = f"""
                    CASE
                        WHEN lower(r.{qident(success_col)}::text) IN ('true', 't', '1', 'success', 'successful', 'ok', 'passed', 'reachable') THEN TRUE
                        WHEN lower(r.{qident(success_col)}::text) IN ('false', 'f', '0', 'failed', 'failure', 'timeout', 'unreachable', 'error') THEN FALSE
                        ELSE r.{qident(latency_col)} IS NOT NULL
                    END
                """
            else:
                success_expr = f"r.{qident(latency_col)} IS NOT NULL"

            source_name_candidates = []
            target_name_candidates = []
            join_clause = ""
            if machines_available:
                source_name_candidates += ["src.alias", "src.hostname", "src.ip_address::text"]
                target_name_candidates += ["dst.alias", "dst.hostname", "dst.ip_address::text"]
                join_clause = f"""
                LEFT JOIN machines src
                    ON src.server_id::text = r.{qident(source_col)}::text
                    OR src.ip_address::text = r.{qident(source_col)}::text
                LEFT JOIN machines dst
                    ON dst.server_id::text = r.{qident(target_col)}::text
                    OR dst.ip_address::text = r.{qident(target_col)}::text
                """
            if source_alias_col:
                source_name_candidates.append(f"r.{qident(source_alias_col)}")
            if target_alias_col:
                target_name_candidates.append(f"r.{qident(target_alias_col)}")
            if target_ip_col:
                target_name_candidates.append(f"r.{qident(target_ip_col)}")
            source_name_candidates.append(f"r.{qident(source_col)}::text")
            target_name_candidates.append(f"r.{qident(target_col)}::text")

            sql = f"""
                SELECT
                    {ts_expr} AS ts,
                    COALESCE({", ".join(source_name_candidates)}) AS source_name,
                    COALESCE({", ".join(target_name_candidates)}) AS target_name,
                    r.{qident(source_col)}::text AS source_value,
                    r.{qident(target_col)}::text AS target_value,
                    r.{qident(latency_col)}::double precision AS latency_ms,
                    {success_expr} AS success
                FROM {qident(table_name)} r
                {join_clause}
                {where_clause}
                ORDER BY {ts_expr} DESC
            """

            return pd.read_sql_query(sql, conn), columns

    st.header("Mesh Ping Results")
    st.caption(
        "Route-level mesh ping view using mesh_ping_results. "
        "Each record represents source → target latency, not single-machine latency."
    )

    col_threshold, col_window = st.columns(2)
    with col_threshold:
        threshold_ms = st.number_input(
            "Expected latency threshold (ms)",
            min_value=1,
            max_value=1000,
            value=50,
            key="mesh_ping_results_threshold_ms",
        )
    with col_window:
        time_window_options = {
            "Last 2 hours": "2 hours",
            "Last 2 days": "2 days",
            "Last 7 days": "7 days",
            "Last 30 days": "30 days",
            "All time": None,
        }
        selected_window_label = st.selectbox(
            "Time window",
            list(time_window_options.keys()),
            index=len(time_window_options) - 1,  # default to "All time" so
            # imported/backfilled data (e.g. demo SQL dumps) isn't silently
            # filtered out just because it predates a fixed lookback window.
            key="mesh_ping_results_time_window",
        )
        time_window_interval = time_window_options[selected_window_label]

    try:
        df, table_columns = load_mesh_ping_results(time_window_interval)
    except Exception as exc:
        st.error(f"Could not load mesh_ping_results: {exc}")
        return

    st.caption("Source table: mesh_ping_results")

    if df.empty:
        st.warning("mesh_ping_results has no rows for the selected time window.")
        return

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["success"] = df["success"].fillna(False).astype(bool)

    df["status"] = df.apply(
        lambda row: (
            "FAILED"
            if not row["success"]
            else "BREACH"
            if pd.notna(row["latency_ms"]) and row["latency_ms"] > threshold_ms
            else "NORMAL"
        ),
        axis=1,
    )

    source_options = sorted(df["source_name"].dropna().unique())
    selected_source = st.selectbox(
        "Select source / host machine",
        source_options,
        key="mesh_ping_results_source_select",
    )

    source_df = df[df["source_name"] == selected_source].copy()

    if source_df.empty:
        st.warning("No mesh ping rows found for this source machine.")
        return

    agg_df = (
        source_df.groupby(["source_name", "target_name"])
        .agg(
            total_runs=("target_name", "count"),
            successful_runs=("success", lambda x: int((x == True).sum())),
            failed_runs=("success", lambda x: int((x == False).sum())),
            avg_latency_ms=("latency_ms", "mean"),
            max_latency_ms=("latency_ms", "max"),
            breach_count=("status", lambda x: int(((x == "BREACH") | (x == "FAILED")).sum())),
        )
        .reset_index()
        .sort_values("target_name")
    )

    agg_df["route_label"] = agg_df["source_name"] + " → " + agg_df["target_name"]

    total_records = int(len(source_df))
    total_breaches = int(((source_df["status"] == "BREACH") | (source_df["status"] == "FAILED")).sum())
    overall_avg = source_df.loc[source_df["success"] == True, "latency_ms"].mean()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source / host machine", selected_source)
    c2.metric("Total records", total_records)
    c3.metric("Breaches / failures", total_breaches)
    c4.metric("Average latency", "N/A" if pd.isna(overall_avg) else f"{overall_avg:.2f} ms")

    st.subheader("Mesh Ping Summary")
    st.caption(
        f"Threshold: {threshold_ms} ms &nbsp;·&nbsp; "
        "Breach % = percentage of pings exceeding the threshold or failed per route"
    )

    summary_df = agg_df[["source_name", "target_name", "breach_count", "total_runs"]].copy()
    summary_df["breach_pct"] = (summary_df["breach_count"] / summary_df["total_runs"] * 100).round(2)
    summary_df = summary_df.sort_values("breach_pct", ascending=False).reset_index(drop=True)

    def _color_breach_pct(val):
        if val > 50:
            return "color: #ff4b4b; font-weight: 700"
        elif val > 20:
            return "color: #ffa500; font-weight: 600"
        elif val > 0:
            return "color: #f0e68c"
        return "color: #4caf50"

    styled_summary = summary_df.style.format(
        {"breach_count": "{:.0f}", "total_runs": "{:.0f}"}
    ).map(
        _color_breach_pct, subset=["breach_pct"]
    )

    st.dataframe(
        styled_summary,
        column_config={
            "source_name": st.column_config.TextColumn("Host Machine Alias", width="medium"),
            "target_name": st.column_config.TextColumn("Target Machine Alias", width="medium"),
            "breach_pct": st.column_config.ProgressColumn(
                "Threshold Breach %",
                format="%.2f%%",
                min_value=0,
                max_value=100,
                width="large",
            ),
        },
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Average latency by mesh route")

    avg_fig = go.Figure()
    agg_df = agg_df.reset_index(drop=True)

    for i in range(1, len(agg_df)):
        prev_row = agg_df.iloc[i - 1]
        curr_row = agg_df.iloc[i]

        y1 = prev_row["avg_latency_ms"]
        y2 = curr_row["avg_latency_ms"]

        if pd.isna(y1) or pd.isna(y2):
            continue

        endpoint_is_breach = y2 > threshold_ms

        avg_fig.add_trace(
            go.Scatter(
                x=[prev_row["route_label"], curr_row["route_label"]],
                y=[y1, y2],
                mode="lines",
                line=dict(
                    color="red" if endpoint_is_breach else "#60a5fa",
                    width=4,
                ),
                showlegend=False,
                hoverinfo="skip",
            )
        )

    normal_df = agg_df[
        agg_df["avg_latency_ms"].notna()
        & (agg_df["avg_latency_ms"] <= threshold_ms)
    ]

    breach_df = agg_df[
        agg_df["avg_latency_ms"].notna()
        & (agg_df["avg_latency_ms"] > threshold_ms)
    ]

    if not normal_df.empty:
        avg_fig.add_trace(
            go.Scatter(
                x=normal_df["route_label"],
                y=normal_df["avg_latency_ms"],
                mode="markers",
                name="At / below threshold",
                marker=dict(size=10, color="#60a5fa"),
                customdata=normal_df[["source_name", "target_name"]],
                hovertemplate=(
                    "Source / host: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Average latency: %{y:.2f} ms<br>"
                    "Status: Normal<extra></extra>"
                ),
            )
        )

    if not breach_df.empty:
        avg_fig.add_trace(
            go.Scatter(
                x=breach_df["route_label"],
                y=breach_df["avg_latency_ms"],
                mode="markers",
                name="Above threshold",
                marker=dict(size=13, color="red"),
                customdata=breach_df[["source_name", "target_name"]],
                hovertemplate=(
                    "Source / host: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Average latency: %{y:.2f} ms<br>"
                    "Status: Above threshold<extra></extra>"
                ),
            )
        )

    avg_fig.add_hline(
        y=threshold_ms,
        line_dash="dash",
        opacity=0.45,
        annotation_text=f"Expected latency: {threshold_ms} ms",
    )

    avg_fig.update_layout(
        xaxis_title="Mesh route (source → target)",
        yaxis_title="Average latency (ms)",
        height=450,
    )

    st.plotly_chart(avg_fig, use_container_width=True)

    st.subheader("Latency breach and failure events")

    event_fig = go.Figure()

    normal_points = source_df[
        (source_df["success"] == True)
        & (source_df["latency_ms"].notna())
    ]

    breach_points = source_df[source_df["status"] == "BREACH"]
    failed_points = source_df[source_df["status"] == "FAILED"].copy()

    if not normal_points.empty:
        event_fig.add_trace(
            go.Scatter(
                x=normal_points["ts"],
                y=normal_points["latency_ms"],
                mode="markers",
                name="Normal ping",
                marker=dict(size=7, color="#60a5fa", opacity=0.35),
                customdata=normal_points[["source_name", "target_name", "status"]],
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Source / host: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Latency: %{y:.2f} ms<br>"
                    "Status: %{customdata[2]}<extra></extra>"
                ),
            )
        )

    if not breach_points.empty:
        event_fig.add_trace(
            go.Scatter(
                x=breach_points["ts"],
                y=breach_points["latency_ms"],
                mode="markers",
                name="Latency breach",
                marker=dict(size=12, color="red"),
                customdata=breach_points[["source_name", "target_name", "status"]],
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Source / host: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Latency: %{y:.2f} ms<br>"
                    "Status: %{customdata[2]}<extra></extra>"
                ),
            )
        )

    if not failed_points.empty:
        failed_points["plot_latency"] = threshold_ms

        event_fig.add_trace(
            go.Scatter(
                x=failed_points["ts"],
                y=failed_points["plot_latency"],
                mode="markers",
                name="Failed ping",
                marker=dict(size=13, color="red", symbol="x"),
                customdata=failed_points[["source_name", "target_name"]],
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Source / host: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Latency: no value<br>"
                    "Status: FAILED<extra></extra>"
                ),
            )
        )

    event_fig.add_hline(
        y=threshold_ms,
        line_dash="dash",
        opacity=0.45,
        annotation_text=f"Expected latency: {threshold_ms} ms",
    )

    event_fig.update_layout(
        xaxis_title="Timestamp",
        yaxis_title="Latency (ms)",
        height=500,
    )

    st.plotly_chart(event_fig, use_container_width=True)

    st.subheader("Raw mesh ping results")

    display_df = source_df[
        [
            "ts",
            "source_name",
            "target_name",
            "source_value",
            "target_value",
            "success",
            "latency_ms",
            "status",
        ]
    ].sort_values("ts", ascending=False)

    def color_rows(row):
        if row["status"] == "FAILED":
            return ["background-color: rgba(255, 0, 0, 0.25)"] * len(row)
        if row["status"] == "BREACH":
            return ["background-color: rgba(255, 165, 0, 0.20)"] * len(row)
        return ["background-color: rgba(0, 255, 0, 0.12)"] * len(row)

    st.dataframe(
        display_df.style.apply(color_rows, axis=1),
        use_container_width=True,
        hide_index=True,
    )
# --- End Inline Mesh Ping Results Dashboard ---

# --- Mesh Ping page switcher ---
dashboard_page = st.sidebar.radio(
    "Dashboard section",
    ["Main Dashboard", "Mesh Ping"],
    index=0,
)

if dashboard_page == "Mesh Ping":
    render_mesh_ping_results_dashboard()
    st.stop()
# --- End Mesh Ping page switcher ---

# ---------------------------------------------------------------------------
# Global styling
# ---------------------------------------------------------------------------
# Charts throughout this app are rendered with Plotly's dark template, so the
# surrounding chrome commits to a matching dark theme rather than following
# Streamlit's light/dark toggle (which would clash with the charts).

st.markdown(
    """
    <style>
    .stApp {
        background: radial-gradient(circle at 15% -10%, #181920 0%, #0d0d0d 55%) fixed;
    }
    .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1500px; }
    h1, h2, h3 { letter-spacing: -0.01em; }

    .dash-title {
        font-size: 2.05rem; font-weight: 800; margin: 0; line-height: 1.2;
        background: linear-gradient(90deg, #ffffff 0%, #9ec5f4 55%, #3987e5 100%);
        -webkit-background-clip: text; background-clip: text; color: transparent;
    }
    .dash-subtitle { color: #898781; font-size: 0.95rem; margin-top: 2px; }

    .section-header {
        display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
        border-bottom: 1px solid rgba(255,255,255,0.10);
        padding-bottom: 6px; margin: 0.4rem 0 0.9rem 0;
    }
    .section-header h2 { margin: 0; font-size: 1.25rem; color: #ffffff; }
    .section-header .section-sub { color: #898781; font-size: 0.85rem; }

    div[data-testid="stMetric"] {
        background: #17181d; border: 1px solid rgba(255,255,255,0.10);
        border-radius: 12px; padding: 12px 14px;
    }
    div[data-testid="stMetricLabel"] { color: #898781; }
    div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px !important; }
    div[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
    div[data-testid="stDataFrame"] * { font-variant-numeric: tabular-nums; }
    button[kind="primaryFormSubmit"], button[kind="secondaryFormSubmit"] {
        border-radius: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _section_header(title: str, subtitle: str = "") -> None:
    """Render a consistent section title with an optional muted subtitle."""
    sub_html = f'<span class="section-sub">{html_lib.escape(subtitle)}</span>' if subtitle else ""
    st.markdown(
        f'<div class="section-header"><h2>{title}</h2>{sub_html}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

header_l, header_r = st.columns([3, 1])
with header_l:
    st.markdown('<p class="dash-title">🖥️ Lab Health Monitoring</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="dash-subtitle">CPU · RAM · Disk · Latency · Threshold Breaches — per machine</p>',
        unsafe_allow_html=True,
    )
with header_r:
    st.badge("Live", icon="🟢", color="green")
    st.caption(f"Updated {datetime.now():%H:%M:%S} · auto-refresh every 30 s")

# ---------------------------------------------------------------------------
# DB connection helpers
# ---------------------------------------------------------------------------

PG_KWARGS = {
    "host": "localhost",
    "port": 5432,
    "user": "release_user",
    "password": os.getenv("P1_DB_PASSWORD", os.getenv("DB_PASSWORD", "release_password")),
    "dbname": "lab_monitoring_db",
}


def get_pg_connection():
    """Return a new psycopg2 connection."""
    try:
        import psycopg2  # noqa: T100
    except ImportError:
        st.error(
            "**psycopg2-binary** is required. "
            "Install it with: `pip install psycopg2-binary`"
        )
        st.stop()
    return psycopg2.connect(**PG_KWARGS)


def _execute_query(query: str, params: Optional[List[Any]] = None) -> pd.DataFrame:
    """Execute a SQL query via raw psycopg2 and return a DataFrame."""
    conn = get_pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            colnames = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        return pd.DataFrame(rows, columns=colnames)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dynamic metric registry
# ---------------------------------------------------------------------------

SAFE_SQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


@st.cache_data(ttl=30, show_spinner="Loading custom metric registry …")
def load_metric_registry() -> pd.DataFrame:
    """Load enabled custom metrics registered by Hermes."""
    try:
        df = _execute_query("""
            SELECT
                metric_key,
                display_name,
                column_name,
                unit,
                chart_group,
                preferred_viz,
                threshold_warning,
                threshold_critical,
                enabled
            FROM metric_registry
            WHERE enabled = true
            ORDER BY chart_group, display_name
        """)
    except Exception:
        return pd.DataFrame(columns=[
            "metric_key",
            "display_name",
            "column_name",
            "unit",
            "chart_group",
            "preferred_viz",
            "threshold_warning",
            "threshold_critical",
            "enabled",
        ])

    if df.empty:
        return df

    df["column_name"] = df["column_name"].astype(str)
    df = df[df["column_name"].apply(lambda c: bool(SAFE_SQL_IDENTIFIER.match(c)))]

    for col in ["threshold_warning", "threshold_critical"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


# ---------------------------------------------------------------------------
# Servers data (cached)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner="Loading server list …")
def _fetch_servers() -> pd.DataFrame:
    return _execute_query(
        "SELECT server_id, alias, hostname, ip_address FROM machines ORDER BY alias"
    )


try:
    servers_df = _fetch_servers()
except Exception as exc:
    st.error(f"Cannot connect to PostgreSQL: {exc}")
    st.stop()

server_options = servers_df["server_id"].tolist()
server_labels = [
    f"{row['alias']}  [{row['server_id'][:8]}…]" for _, row in servers_df.iterrows()
]

# ---------------------------------------------------------------------------
# Filters row — Server + Date range + Go button
# ---------------------------------------------------------------------------

now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
default_start = now_utc - timedelta(hours=6)

if "applied_server" not in st.session_state:
    st.session_state["applied_server"] = server_options[0] if server_options else None
if "applied_start_date" not in st.session_state:
    st.session_state["applied_start_date"] = default_start.date()
if "applied_end_date" not in st.session_state:
    st.session_state["applied_end_date"] = now_utc.date()

with st.container(border=True):
    st.markdown("**🔎 Filters**")
    with st.form("dashboard_filters_form", clear_on_submit=False):
        fcol1, fcol2, fcol3, fcol4 = st.columns([3, 2, 2, 1])

        with fcol1:
            pending_server = st.selectbox(
                "Select Server",
                options=server_options,
                index=server_options.index(st.session_state["applied_server"])
                if st.session_state["applied_server"] in server_options else 0,
                format_func=lambda sid: next(
                    (lbl for s, lbl in zip(server_options, server_labels) if s == sid),
                    sid[:8],
                ),
            )

        with fcol2:
            pending_start_date = st.date_input(
                "From Date",
                value=st.session_state["applied_start_date"],
            )

        with fcol3:
            pending_end_date = st.date_input(
                "To Date",
                value=st.session_state["applied_end_date"],
            )

        with fcol4:
            st.write("")
            submitted_filters = st.form_submit_button(
                "Go", use_container_width=True, type="primary",
            )

if submitted_filters:
    st.session_state["applied_server"] = pending_server
    st.session_state["applied_start_date"] = pending_start_date
    st.session_state["applied_end_date"] = pending_end_date
    st.rerun()

selected_server = st.session_state["applied_server"]
selected_servers = [selected_server]
start_ts = datetime.combine(st.session_state["applied_start_date"], datetime.min.time())
end_ts = datetime.combine(st.session_state["applied_end_date"], datetime.max.time())

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 📡 Connection")
    with st.container(border=True):
        st.code(
            f"postgresql://{PG_KWARGS['user']}@{PG_KWARGS['host']}:{PG_KWARGS['port']}/{PG_KWARGS['dbname']}",
            language="text",
        )
        st.badge("Connected", icon="✅", color="green")

    st.markdown("### ⚙️ Options")
    include_non_ok = st.checkbox("Include non‑ok metric samples", value=False)

    if st.button("⟳ Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption("↻ Auto‑refreshes every 30 s (cache TTL)")

    st.divider()
    st.markdown("### 🖥️ Current View")
    _current_server_label = next(
        (lbl for s, lbl in zip(server_options, server_labels) if s == st.session_state["applied_server"]),
        "—",
    )
    st.caption(f"**Server:** {_current_server_label}")
    st.caption(
        f"**Range:** {st.session_state['applied_start_date']} → {st.session_state['applied_end_date']}"
    )

# ---------------------------------------------------------------------------
# Data loaders (cached)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner="Loading metric samples …")
def load_metrics(
    start_ts: Optional[datetime],
    end_ts: Optional[datetime],
    server_ids: List[str],
    include_non_ok_samples: bool,
) -> pd.DataFrame:
    where_clauses: List[str] = []
    params: List[Any] = []

    if start_ts is not None:
        where_clauses.append("ms.ts >= %s")
        params.append(start_ts)
    if end_ts is not None:
        where_clauses.append("ms.ts <= %s")
        params.append(end_ts)
    if server_ids:
        where_clauses.append("ms.server_id = ANY(%s::uuid[])")
        params.append(server_ids)
    if not include_non_ok_samples:
        where_clauses.append("(ms.status = 'ok' OR ms.status IS NULL)")

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    # -- Only include registered columns that actually exist in metric_samples --
    registry_df = load_metric_registry()
    custom_cols: List[str] = []

    if not registry_df.empty:
        try:
            actual_cols_df = _execute_query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'metric_samples' AND table_schema = 'public'"
            )
            existing_cols: set = set(actual_cols_df["column_name"].astype(str))
        except Exception:
            existing_cols = set()

        custom_cols = registry_df["column_name"].dropna().astype(str).tolist()
        custom_cols = [
            c for c in custom_cols
            if SAFE_SQL_IDENTIFIER.match(c) and c in existing_cols
        ]

    custom_select_sql = ""
    for col in custom_cols:
        custom_select_sql += f",\n            ms.{col}"

    query = f"""
        SELECT
            CAST(ms.server_id AS VARCHAR) AS server_id,
            ms.ts,
            ms.cpu_pct,
            ms.ram_pct,
            ms.disk_pct,
            ms.disk_latency_ms,
            ms.net_latency_ms,
            ms.packet_loss_pct
            {custom_select_sql},
            COALESCE(NULLIF(m.alias, ''), m.hostname, CAST(ms.server_id AS VARCHAR)) AS label,
            m.hostname,
            m.ip_address
        FROM metric_samples ms
        LEFT JOIN machines m ON m.server_id = ms.server_id
        WHERE {where_sql}
        ORDER BY ms.ts ASC, ms.server_id ASC
    """

    df = _execute_query(query, params)

    if df.empty:
        df["series_label"] = pd.Series(dtype="string")
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["server_id"] = df["server_id"].astype(str)

    numeric_cols = [
        "cpu_pct",
        "ram_pct",
        "disk_pct",
        "disk_latency_ms",
        "net_latency_ms",
        "packet_loss_pct",
    ] + custom_cols

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["series_label"] = df["label"] + " [" + df["server_id"].str[:8] + "]"
    return df


@st.cache_data(ttl=30, show_spinner="Loading events …")
def load_events(
    start_ts: Optional[datetime],
    end_ts: Optional[datetime],
    server_ids: List[str],
) -> pd.DataFrame:
    where_clauses: List[str] = ["e.event_type = 'threshold_breach'"]
    params: List[Any] = []

    if start_ts is not None:
        where_clauses.append("e.ts >= %s")
        params.append(start_ts)
    if end_ts is not None:
        where_clauses.append("e.ts <= %s")
        params.append(end_ts)
    if server_ids:
        where_clauses.append("e.server_id = ANY(%s::uuid[])")
        params.append(server_ids)

    where_sql = " AND ".join(where_clauses)

    query = f"""
        SELECT
            CAST(e.server_id AS VARCHAR) AS server_id,
            e.ts,
            e.event_type,
            e.severity,
            e.metric,
            e.value,
            e.threshold,
            e.message,
            e.consecutive_breaches,
            COALESCE(NULLIF(m.alias, ''), m.hostname, CAST(e.server_id AS VARCHAR)) AS label
        FROM events e
        LEFT JOIN machines m ON m.server_id = e.server_id
        WHERE {where_sql}
        ORDER BY e.ts DESC
    """

    df = _execute_query(query, params)

    if df.empty:
        df["series_label"] = pd.Series(dtype="string")
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["server_id"] = df["server_id"].astype(str)
    for col in ["value", "threshold"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["series_label"] = df["label"] + " [" + df["server_id"].str[:8] + "]"
    return df


@st.cache_data(ttl=30, show_spinner="Loading network checks …")
def load_network_checks(
    start_ts: Optional[datetime],
    end_ts: Optional[datetime],
    server_ids: List[str],
) -> pd.DataFrame:
    where_clauses: List[str] = []
    params: List[Any] = []

    if start_ts is not None:
        where_clauses.append("ncs.ts >= %s")
        params.append(start_ts)
    if end_ts is not None:
        where_clauses.append("ncs.ts <= %s")
        params.append(end_ts)
    if server_ids:
        where_clauses.append("ncs.server_id = ANY(%s::uuid[])")
        params.append(server_ids)

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    query = f"""
        SELECT
            CAST(ncs.server_id AS VARCHAR) AS server_id,
            ncs.ts,
            ncs.target,
            ncs.check_type,
            ncs.latency_ms,
            ncs.packet_loss_pct,
            ncs.status
        FROM network_check_samples ncs
        WHERE {where_sql}
        ORDER BY ncs.ts ASC
    """

    df = _execute_query(query, params)

    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["server_id"] = df["server_id"].astype(str)
    for col in ["latency_ms", "packet_loss_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Mesh ping loader — route-level (source → target) latency checks.
# Filtered by the selected machine as the source, same as every other chart.
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner="Loading mesh ping results …")
def load_mesh_ping_data(
    start_ts: Optional[datetime],
    end_ts: Optional[datetime],
    source_server_id: Optional[str],
) -> pd.DataFrame:
    if not source_server_id:
        return pd.DataFrame()

    where_clauses: List[str] = ["r.source_server_id = %s::uuid"]
    params: List[Any] = [source_server_id]

    if start_ts is not None:
        where_clauses.append("r.ts >= %s")
        params.append(start_ts)
    if end_ts is not None:
        where_clauses.append("r.ts <= %s")
        params.append(end_ts)

    where_sql = " AND ".join(where_clauses)

    query = f"""
        SELECT
            r.id,
            r.ts,
            CAST(r.source_server_id AS VARCHAR) AS source_server_id,
            COALESCE(NULLIF(r.source_alias, ''), sm.alias, CAST(r.source_server_id AS VARCHAR)) AS source_name,
            CAST(r.target_server_id AS VARCHAR) AS target_server_id,
            COALESCE(NULLIF(r.target_alias, ''), tm.alias, CAST(r.target_server_id AS VARCHAR)) AS target_name,
            CAST(r.target_ip AS VARCHAR) AS target_ip,
            r.success,
            r.latency_ms
        FROM mesh_ping_results r
        LEFT JOIN machines sm ON sm.server_id = r.source_server_id
        LEFT JOIN machines tm ON tm.server_id = r.target_server_id
        WHERE {where_sql}
        ORDER BY r.ts ASC
    """

    df = _execute_query(query, params)

    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce")
    df["success"] = df["success"].fillna(False).astype(bool)
    return df


def _annotate_mesh_status(df: pd.DataFrame, threshold_ms: float) -> pd.DataFrame:
    """Add a NORMAL / BREACH / FAILED status column to a mesh ping dataframe."""
    df = df.copy()
    df["status"] = df.apply(
        lambda row: (
            "FAILED"
            if not row["success"]
            else "BREACH"
            if pd.notna(row["latency_ms"]) and row["latency_ms"] > threshold_ms
            else "NORMAL"
        ),
        axis=1,
    )
    return df


# ---------------------------------------------------------------------------
# App metrics loader
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner="Loading app metrics …")
def load_app_metrics(
    start_ts: Optional[datetime],
    end_ts: Optional[datetime],
    server_ids: List[str],
) -> pd.DataFrame:
    where_clauses: List[str] = []
    params: List[Any] = []

    if start_ts is not None:
        where_clauses.append("ams.ts >= %s")
        params.append(start_ts)
    if end_ts is not None:
        where_clauses.append("ams.ts <= %s")
        params.append(end_ts)
    if server_ids:
        where_clauses.append("ams.server_id = ANY(%s::uuid[])")
        params.append(server_ids)

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

    query = f"""
        SELECT
            CAST(ams.server_id AS VARCHAR) AS server_id,
            ams.ts,
            ams.app_name,
            ams.cpu_pct,
            ams.rss_memory_mb,
            ams.process_count,
            ams.thread_count,
            ams.listening_sockets,
            ams.status,
            COALESCE(NULLIF(m.alias, ''), m.hostname, CAST(ams.server_id AS VARCHAR)) AS label,
            m.hostname,
            m.ip_address
        FROM app_metric_samples ams
        LEFT JOIN machines m ON m.server_id = ams.server_id
        WHERE {where_sql}
        ORDER BY ams.ts DESC, ams.app_name ASC
    """

    df = _execute_query(query, params)

    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["server_id"] = df["server_id"].astype(str)
    df["app_name"] = df["app_name"].astype(str)
    df["status"] = df["status"].astype(str).str.lower()

    for col in ["cpu_pct", "rss_memory_mb"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["process_count", "thread_count", "listening_sockets"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    df["is_up"] = df["status"].eq("running")
    df["is_failure"] = ~df["status"].eq("running")
    df["series_label"] = df["label"] + " [" + df["server_id"].str[:8] + "]"

    return df


# ---------------------------------------------------------------------------
# App metrics helper
# ---------------------------------------------------------------------------


def build_app_metrics_summary(app_df: pd.DataFrame) -> pd.DataFrame:
    if app_df.empty:
        return pd.DataFrame(columns=[
            "App Name",
            "Up %",
            "Down %",
            "Running %",
            "Stopped %",
            "Error %",
            "Restarting %",
            "Failure Count",
            "Total Samples",
            "Last Status",
            "Last Seen",
        ])

    rows = []

    for app_name, grp in app_df.groupby("app_name"):
        total = len(grp)
        if total == 0:
            continue

        status_counts = grp["status"].value_counts()

        running_count = int(status_counts.get("running", 0))
        stopped_count = int(status_counts.get("stopped", 0))
        error_count = int(status_counts.get("error", 0))
        restarting_count = int(status_counts.get("restarting", 0))
        failure_count = stopped_count + error_count + restarting_count

        latest_row = grp.sort_values("ts", ascending=False).iloc[0]

        rows.append({
            "App Name": app_name,
            "Up %": round((running_count / total) * 100, 2),
            "Down %": round((failure_count / total) * 100, 2),
            "Running %": round((running_count / total) * 100, 2),
            "Stopped %": round((stopped_count / total) * 100, 2),
            "Error %": round((error_count / total) * 100, 2),
            "Restarting %": round((restarting_count / total) * 100, 2),
            "Failure Count": failure_count,
            "Total Samples": total,
            "Last Status": latest_row.get("status", ""),
            "Last Seen": latest_row.get("ts", pd.NaT),
        })

    return pd.DataFrame(rows).sort_values(
        by=["Failure Count", "Down %", "App Name"],
        ascending=[False, False, True],
    )


# ---------------------------------------------------------------------------
# Layout helper
# ---------------------------------------------------------------------------


def _apply_scroll_layout(
    fig: go.Figure, height: int, yaxis_title: str, width: int = 1000,
    show_legend: bool = False,
) -> None:
    fig.update_layout(
        width=width,
        height=height,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(20,20,30,0.92)", font=dict(size=11)),
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=show_legend,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            font=dict(size=9),
        ),
        xaxis=dict(rangeslider=dict(visible=True, thickness=0.06), type="date"),
        yaxis=dict(title=yaxis_title),
    )


def _make_empty_fig() -> go.Figure:
    """Return a minimal figure with 'No data' annotation."""
    fig = go.Figure()
    fig.add_annotation(text="No data", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
    fig.update_layout(height=320, template="plotly_dark",
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig


def _style_pie_chart(fig: go.Figure) -> None:
    """Apply consistent dark styling to a pie chart."""
    fig.update_layout(
        height=300,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=10, b=90),
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.2,
            xanchor="center",
            x=0.5,
            font=dict(size=10),
        ),
    )


# ---------------------------------------------------------------------------
# Pie chart builders — summary stats
# ---------------------------------------------------------------------------


def make_cpu_summary_pie(df: pd.DataFrame) -> go.Figure:
    """Pie chart: distribution of CPU samples across Low (≤50%), Medium (50-85%), High (>85%) ranges."""
    cpu_vals = df["cpu_pct"].dropna()
    if cpu_vals.empty:
        return _make_empty_fig()

    low = int((cpu_vals <= 50).sum())
    medium = int(((cpu_vals > 50) & (cpu_vals <= 85)).sum())
    high = int((cpu_vals > 85).sum())

    labels = ["Low (≤50%)", "Medium (50‑85%)", "High (>85%)"]
    values = [low, medium, high]

    fig = go.Figure(data=[go.Pie(
        labels=labels,
        values=values,
        hole=0.55,
        marker=dict(colors=["#0ca30c", "#fab219", "#d03b3b"]),
        textposition="inside",
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b>: %{value} samples (%{percent})<extra></extra>",
    )])
    _style_pie_chart(fig)
    return fig


def make_ram_summary_pie(df: pd.DataFrame) -> go.Figure:
    """Pie chart: % of samples where RAM usage was <50%, 50-86%, >86%."""
    ram_vals = df["ram_pct"].dropna()
    if ram_vals.empty:
        return _make_empty_fig()

    low = int((ram_vals < 50).sum())
    mid = int(((ram_vals >= 50) & (ram_vals <= 86)).sum())
    high = int((ram_vals > 86).sum())

    labels = ["<50%", "50–86%", ">86%"]
    values = [low, mid, high]

    fig = go.Figure(data=[go.Pie(
        labels=labels,
        values=values,
        hole=0.55,
        marker=dict(colors=["#0ca30c", "#fab219", "#d03b3b"]),
        textposition="inside",
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b>: %{value} samples (%{percent})<extra></extra>",
    )])
    _style_pie_chart(fig)
    return fig


def make_disk_summary_pie(df: pd.DataFrame) -> go.Figure:
    """Pie chart: average free vs used disk percentage."""
    disk_vals = df["disk_pct"].dropna()
    if disk_vals.empty:
        return _make_empty_fig()

    avg_used = disk_vals.mean()
    avg_free = 100.0 - avg_used

    fig = go.Figure(data=[go.Pie(
        labels=["Used", "Free"],
        values=[avg_used, avg_free],
        hole=0.55,
        marker=dict(colors=["#d03b3b", "#0ca30c"]),
        textposition="inside",
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b>: %{value:.1f}%<extra></extra>",
    )])
    _style_pie_chart(fig)
    return fig


def make_latency_summary_pie(df: pd.DataFrame) -> go.Figure:
    """Pie chart: distribution of network latency samples across Very Good (<50ms), Average (50-100ms), Poor (>100ms)."""
    latency_vals = df["net_latency_ms"].dropna()
    if latency_vals.empty:
        return _make_empty_fig()

    very_good = int((latency_vals < 50).sum())
    average = int(((latency_vals >= 50) & (latency_vals <= 100)).sum())
    poor = int((latency_vals > 100).sum())

    labels = ["Very Good (<50ms)", "Average (50‑100ms)", "Poor (>100ms)"]
    values = [very_good, average, poor]

    fig = go.Figure(data=[go.Pie(
        labels=labels,
        values=values,
        hole=0.55,
        marker=dict(colors=["#0ca30c", "#fab219", "#d03b3b"]),
        textposition="inside",
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b>: %{value} samples (%{percent})<extra></extra>",
    )])
    _style_pie_chart(fig)
    return fig


# ---------------------------------------------------------------------------
# Chart builders — time-series (single machine mode)
# ---------------------------------------------------------------------------


def _build_single_trace(
    grp: pd.DataFrame, y_col: str, label: str, value_unit: str = "",
    line_color: Optional[str] = None,
) -> go.Scatter:
    """Build one trace for the selected machine with timestamp in tooltip."""
    line_cfg = dict(width=3)
    if line_color:
        line_cfg["color"] = line_color
    return go.Scatter(
        x=grp["ts"], y=grp[y_col],
        mode="lines+markers",
        name=label, line=line_cfg,
        marker=dict(size=5, color=line_color) if line_color else dict(size=5),
        hovertemplate=(
            f"<b>{label}</b><br>"
            "Time: %{x|%Y-%m-%d %H:%M:%S}<br>"
            f"Value: %{{y:.2f}}{value_unit}"
            "<extra></extra>"
        ),
    )


def _make_single_machine_chart(
    chart_df: pd.DataFrame,
    y_col: str,
    height: int,
    yaxis_title: str,
    value_unit: str = "",
    y_range: Optional[Tuple[Optional[float], Optional[float]]] = None,
    line_color: Optional[str] = None,
) -> go.Figure:
    """Single machine time-series chart."""
    fig = go.Figure()
    for sid, grp in chart_df.sort_values("ts").groupby("series_label"):
        fig.add_trace(_build_single_trace(grp, y_col, sid, value_unit, line_color=line_color))
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    _apply_scroll_layout(fig, height, yaxis_title, show_legend=False)
    if y_range:
        fig.update_yaxes(range=y_range)
    return fig


def make_metric_chart(
    df: pd.DataFrame,
    y_col: str,
    title: str,
    height: int,
    selected_label: str,
    yaxis_title: str = "Usage (%)",
    y_range: Optional[Tuple[Optional[float], Optional[float]]] = None,
    breach_threshold: Optional[float] = None,
    value_unit: str = "%",
) -> go.Figure:
    chart_df = df.dropna(subset=["ts", y_col, "server_id"]).copy()
    if chart_df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No data", xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(height=height, xaxis=dict(rangeslider=dict(visible=True)))
        return fig

    single = chart_df[chart_df["series_label"] == selected_label].copy()
    if single.empty:
        fig = go.Figure()
        fig.add_annotation(
            text=f"No data for {selected_label}",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
        )
        fig.update_layout(height=height, xaxis=dict(rangeslider=dict(visible=True)))
        return fig

    if breach_threshold is not None and breach_threshold > 0:
        single = single[single[y_col] > breach_threshold].copy()

        if single.empty:
            fig = go.Figure()
            fig.add_annotation(
                text=f"No {title} breaches above {breach_threshold:g}{value_unit}",
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
                showarrow=False,
            )
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            )
            _apply_scroll_layout(fig, height, yaxis_title, show_legend=False)
            if y_range:
                fig.update_yaxes(range=y_range)
            fig.add_hline(
                y=breach_threshold,
                line_dash="dash",
                line_color="red",
                annotation_text=f"Threshold: {breach_threshold:g}{value_unit}",
                annotation_position="top left",
            )
            return fig

        fig = _make_single_machine_chart(
            single,
            y_col,
            height,
            yaxis_title,
            value_unit=value_unit,
            y_range=y_range,
            line_color="red",
        )
        fig.add_hline(
            y=breach_threshold,
            line_dash="dash",
            line_color="red",
            annotation_text=f"Threshold: {breach_threshold:g}{value_unit}",
            annotation_position="top left",
        )
        return fig

    fig = _make_single_machine_chart(
        single,
        y_col,
        height,
        yaxis_title,
        value_unit=value_unit,
        y_range=y_range,
    )
    return fig


def make_latency_chart(
    df: pd.DataFrame,
    height: int,
    selected_label: str,
) -> go.Figure:
    single = df[df["series_label"] == selected_label].copy()

    fig = go.Figure()
    s_net = single.dropna(subset=["ts", "net_latency_ms"]).sort_values("ts")
    if not s_net.empty:
        fig.add_trace(_build_single_trace(s_net, "net_latency_ms", f"{selected_label} (net)", " ms"))
    s_disk = single.dropna(subset=["ts", "disk_latency_ms"]).sort_values("ts")
    if not s_disk.empty:
        t = go.Scatter(
            x=s_disk["ts"], y=s_disk["disk_latency_ms"],
            mode="lines+markers", name=f"{selected_label} (disk)",
            line=dict(dash="dot", width=3), marker=dict(size=5),
            hovertemplate=(
                f"<b>{selected_label}</b><br>"
                "Time: %{x|%Y-%m-%d %H:%M:%S}<br>"
                "Disk Latency: %{y:.2f} ms<extra></extra>"
            ),
        )
        fig.add_trace(t)
    if not fig.data:
        fig.add_annotation(text=f"No latency data for {selected_label}",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False)
    fig.update_layout(
        title=f"Latency — {selected_label}",
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    _apply_scroll_layout(fig, height, "Latency (ms)")
    return fig


def make_mesh_route_chart(mesh_df: pd.DataFrame, height: int, threshold_ms: float) -> go.Figure:
    """Average mesh-ping latency per target route, for the selected source machine."""
    if mesh_df.empty:
        return _make_empty_fig()

    agg_df = (
        mesh_df.groupby("target_name")
        .agg(
            avg_latency_ms=("latency_ms", "mean"),
            breach_count=("latency_ms", lambda s: int((s > threshold_ms).sum())),
        )
        .reset_index()
        .sort_values("target_name")
        .reset_index(drop=True)
    )

    fig = go.Figure()

    for i in range(1, len(agg_df)):
        prev_row, curr_row = agg_df.iloc[i - 1], agg_df.iloc[i]
        y1, y2 = prev_row["avg_latency_ms"], curr_row["avg_latency_ms"]
        if pd.isna(y1) or pd.isna(y2):
            continue
        fig.add_trace(go.Scatter(
            x=[prev_row["target_name"], curr_row["target_name"]],
            y=[y1, y2],
            mode="lines",
            line=dict(color="red" if y2 > threshold_ms else "#60a5fa", width=4),
            showlegend=False,
            hoverinfo="skip",
        ))

    normal_df = agg_df[agg_df["avg_latency_ms"].notna() & (agg_df["avg_latency_ms"] <= threshold_ms)]
    breach_df = agg_df[agg_df["avg_latency_ms"].notna() & (agg_df["avg_latency_ms"] > threshold_ms)]

    if not normal_df.empty:
        fig.add_trace(go.Scatter(
            x=normal_df["target_name"], y=normal_df["avg_latency_ms"],
            mode="markers", name="At / below threshold",
            marker=dict(size=10, color="#60a5fa"),
            hovertemplate="Target: %{x}<br>Average latency: %{y:.2f} ms<extra></extra>",
        ))
    if not breach_df.empty:
        fig.add_trace(go.Scatter(
            x=breach_df["target_name"], y=breach_df["avg_latency_ms"],
            mode="markers", name="Above threshold",
            marker=dict(size=13, color="red"),
            hovertemplate="Target: %{x}<br>Average latency: %{y:.2f} ms<extra></extra>",
        ))

    fig.add_hline(
        y=threshold_ms, line_dash="dash", opacity=0.45,
        annotation_text=f"Expected latency: {threshold_ms:g} ms",
    )
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    _apply_scroll_layout(fig, height, "Average latency (ms)", show_legend=True)
    # Route labels, not timestamps — override the date-axis default from
    # _apply_scroll_layout (built for time-series charts) with a category
    # axis, and drop the now-meaningless date rangeslider.
    fig.update_xaxes(type="category", rangeslider=dict(visible=False), title="Target machine")
    return fig


def build_mesh_ping_summary(mesh_df: pd.DataFrame, threshold_ms: float) -> pd.DataFrame:
    """Summarize mesh ping results for the selected source machine and date range."""
    if mesh_df.empty:
        return pd.DataFrame()

    summary_source_df = mesh_df.dropna(subset=["source_name", "target_name"]).copy()
    summary_source_df["source_name"] = summary_source_df["source_name"].astype(str).str.strip()
    summary_source_df["target_name"] = summary_source_df["target_name"].astype(str).str.strip()
    summary_source_df["latency_ms"] = pd.to_numeric(summary_source_df["latency_ms"], errors="coerce")
    summary_source_df = summary_source_df[
        (summary_source_df["source_name"] != "")
        & (summary_source_df["target_name"] != "")
    ].copy()

    if summary_source_df.empty:
        return pd.DataFrame()

    summary_df = (
        summary_source_df.groupby(["source_name", "target_name"])
        .agg(
            breach_count=("latency_ms", lambda s: int((s > threshold_ms).sum())),
            total_runs=("latency_ms", "count"),
        )
        .reset_index()
    )
    summary_df = summary_df[summary_df["total_runs"] > 0].copy()
    if summary_df.empty:
        return pd.DataFrame()

    summary_df["breach_pct"] = (
        summary_df["breach_count"] / summary_df["total_runs"] * 100
    ).round(2)
    return summary_df.sort_values("breach_pct", ascending=False).reset_index(drop=True)


def render_mesh_ping_summary_table(
    mesh_df: pd.DataFrame,
    threshold_ms: float,
    selected_label: str,
) -> None:
    summary_df = build_mesh_ping_summary(mesh_df, threshold_ms)

    if summary_df.empty:
        st.info(f"No mesh ping summary data for {selected_label} in the selected range.")
        return

    st.subheader("Mesh Ping Summary")
    st.caption(
        f"Threshold: {threshold_ms:g} ms · "
        f"{len(mesh_df):,} records · {selected_label} as source"
    )

    table_df = summary_df[
        [
            "source_name",
            "target_name",
            "breach_pct",
            "breach_count",
            "total_runs",
        ]
    ].copy()

    def _color_breach_pct(val):
        if val > 50:
            return "color: #ff4b4b; font-weight: 700"
        if val > 20:
            return "color: #ffa500; font-weight: 600"
        if val > 0:
            return "color: #f0e68c; font-weight: 600"
        return "color: #4caf50; font-weight: 600"

    styled_table = table_df.style.format(
        {
            "breach_count": "{:.0f}",
            "total_runs": "{:.0f}",
        }
    ).map(_color_breach_pct, subset=["breach_pct"])

    table_height = min(max((len(table_df) + 1) * 35 + 3, 110), 420)

    st.dataframe(
        styled_table,
        use_container_width=True,
        hide_index=True,
        height=table_height,
        column_config={
            "source_name": st.column_config.TextColumn("Host Machine Alias", width="medium"),
            "target_name": st.column_config.TextColumn("Target Machine Alias", width="medium"),
            "breach_pct": st.column_config.ProgressColumn(
                "Threshold Breach %",
                format="%.2f%%",
                min_value=0,
                max_value=100,
                width="large",
            ),
            "breach_count": st.column_config.NumberColumn("Threshold Breaches", format="%d"),
            "total_runs": st.column_config.NumberColumn("Total Runs", format="%d"),
        },
    )


# ---------------------------------------------------------------------------
# App body
# ---------------------------------------------------------------------------

metrics_df = pd.DataFrame()
events_df = pd.DataFrame()
network_df = pd.DataFrame()
app_metrics_df = pd.DataFrame()

with st.spinner("Loading data …"):
    try:
        metrics_df = load_metrics(start_ts, end_ts, selected_servers, include_non_ok)
        events_df = load_events(start_ts, end_ts, selected_servers)
        network_df = load_network_checks(start_ts, end_ts, selected_servers)
        app_metrics_df = load_app_metrics(start_ts, end_ts, selected_servers)
    except Exception as exc:
        st.error("Failed to load data from PostgreSQL.")
        st.exception(exc)
        st.stop()

if metrics_df.empty and events_df.empty and network_df.empty and app_metrics_df.empty:
    st.warning("No data found for the selected filters. Try expanding the time range or selecting a different server.")
    st.stop()

# ---- Derive the formatted label for the selected server ----
if not metrics_df.empty:
    mask = metrics_df["server_id"] == selected_server
    if mask.any():
        selected_label = metrics_df.loc[mask, "series_label"].iloc[0]
    else:
        selected_label = selected_server[:8]
else:
    selected_label = selected_server[:8]

# ---- Latest sample for the selected server (used for "current value" tiles) ----
if not metrics_df.empty:
    _sel_sorted = metrics_df[metrics_df["server_id"] == selected_server].sort_values("ts")
    latest_metrics_row = _sel_sorted.iloc[-1] if not _sel_sorted.empty else None
else:
    latest_metrics_row = None


def _latest_value(col: str) -> Optional[float]:
    if latest_metrics_row is None or col not in latest_metrics_row.index:
        return None
    val = latest_metrics_row[col]
    if pd.isna(val):
        return None
    return float(val)


# ---------------------------------------------------------------------------
# Breach threshold defaults (session state)
# ---------------------------------------------------------------------------

# Breach thresholds: defaults to 0.0 (disabled — charts show all data normally)
DEFAULT_CPU_THRESHOLD = 0.0
DEFAULT_RAM_THRESHOLD = 0.0
DEFAULT_DISK_THRESHOLD = 0.0

for key, default_value in {
    "cpu_threshold_pct": DEFAULT_CPU_THRESHOLD,
    "ram_threshold_pct": DEFAULT_RAM_THRESHOLD,
    "disk_threshold_pct": DEFAULT_DISK_THRESHOLD,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default_value

# Hardcoded fallback defaults for the Threshold Breaches Summary
# (only used when the user hasn't set a custom threshold)
FALLBACK_CPU_THRESHOLD = 90.0
FALLBACK_RAM_THRESHOLD = 95.0
FALLBACK_DISK_THRESHOLD = 85.0
LATENCY_BREACH_THRESHOLD = 120.0

# ===== Threshold Alerts =====

_section_header("⚠️ Threshold Alerts", selected_label)

raw_cpu = float(st.session_state.get("cpu_threshold_pct", DEFAULT_CPU_THRESHOLD))
raw_ram = float(st.session_state.get("ram_threshold_pct", DEFAULT_RAM_THRESHOLD))
raw_disk = float(st.session_state.get("disk_threshold_pct", DEFAULT_DISK_THRESHOLD))

# Use custom threshold if set (> 0), otherwise fall back to hardcoded defaults
cpu_threshold_val = raw_cpu if raw_cpu > 0 else FALLBACK_CPU_THRESHOLD
ram_threshold_val = raw_ram if raw_ram > 0 else FALLBACK_RAM_THRESHOLD
disk_threshold_val = raw_disk if raw_disk > 0 else FALLBACK_DISK_THRESHOLD

if not metrics_df.empty:
    cpu_val = metrics_df["cpu_pct"].dropna()
    ram_val = metrics_df["ram_pct"].dropna()
    disk_val = metrics_df["disk_pct"].dropna()
    lat_val = metrics_df["net_latency_ms"].dropna()

    cpu_breaches = int((cpu_val > cpu_threshold_val).sum())
    ram_breaches = int((ram_val > ram_threshold_val).sum())
    disk_breaches = int((disk_val > disk_threshold_val).sum())
    latency_breaches = int((lat_val > LATENCY_BREACH_THRESHOLD).sum())
else:
    cpu_breaches = ram_breaches = disk_breaches = latency_breaches = 0

breaches_summary_df = pd.DataFrame({
    "Resource Metric": ["CPU", "RAM", "Disk", "Network Latency"],
    "Breach Threshold": [
        f"> {cpu_threshold_val:g}%",
        f"> {ram_threshold_val:g}%",
        f"> {disk_threshold_val:g}%",
        f"> {LATENCY_BREACH_THRESHOLD:g} ms",
    ],
    "Total Breaches": [cpu_breaches, ram_breaches, disk_breaches, latency_breaches],
})

# -------- Alert cards --------

alert_cols = st.columns(4, gap="small")
alert_defs = [
    ("💻", "CPU", cpu_breaches),
    ("🧠", "RAM", ram_breaches),
    ("💾", "Disk", disk_breaches),
    ("📶", "Network Latency", latency_breaches),
]
for col, (icon, name, count) in zip(alert_cols, alert_defs):
    with col:
        with st.container(border=True):
            st.markdown(f"**{icon} {name}**")
            st.metric("Breaches", f"{count:,}", label_visibility="collapsed")
            thresh_str = breaches_summary_df.loc[
                breaches_summary_df["Resource Metric"] == name, "Breach Threshold"
            ].iloc[0]
            if count > 0:
                st.badge(f"{count:,} breach{'es' if count != 1 else ''}", icon="🔴", color="red")
            else:
                st.badge("Healthy", icon="🟢", color="green")
            st.caption(f"Threshold: {thresh_str}")

# -------- Interactive Breach Details (click on a row) --------

# Map display names to metric column and threshold value
BREACH_METRIC_CONFIG = {
    "CPU": {"col": "cpu_pct", "threshold": cpu_threshold_val, "unit": "%"},
    "RAM": {"col": "ram_pct", "threshold": ram_threshold_val, "unit": "%"},
    "Disk": {"col": "disk_pct", "threshold": disk_threshold_val, "unit": "%"},
    "Network Latency": {"col": "net_latency_ms", "threshold": LATENCY_BREACH_THRESHOLD, "unit": " ms"},
}

with st.container(border=True):
    st.caption("👆 Click a row to inspect the matching samples")

    # Display the table with row selection enabled
    breach_selection = st.dataframe(
        breaches_summary_df,
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        column_config={
            "Resource Metric": st.column_config.TextColumn("Resource Metric"),
            "Breach Threshold": st.column_config.TextColumn("Breach Threshold"),
            "Total Breaches": st.column_config.NumberColumn("Total Breaches", format="%d"),
        },
    )

# Handle row selection — filter metrics_df directly for breach samples
if breach_selection.selection and breach_selection.selection.rows:
    selected_row_idx = breach_selection.selection.rows[0]
    selected_metric_display = breaches_summary_df.iloc[selected_row_idx]["Resource Metric"]
    selected_threshold_str = breaches_summary_df.iloc[selected_row_idx]["Breach Threshold"]
    selected_breach_count = int(breaches_summary_df.iloc[selected_row_idx]["Total Breaches"])

    if selected_breach_count > 0 and not metrics_df.empty:
        metric_config = BREACH_METRIC_CONFIG.get(selected_metric_display)
        if metric_config:
            metric_col = metric_config["col"]
            metric_thresh = metric_config["threshold"]
            metric_unit = metric_config["unit"]

            # Get actual breach samples from metrics_df (metric_samples table)
            breach_samples = metrics_df[
                (metrics_df["server_id"] == selected_server) &
                (metrics_df[metric_col].notna()) &
                (metrics_df[metric_col] > metric_thresh)
            ].copy()

            if not breach_samples.empty:
                breach_samples = breach_samples.sort_values("ts", ascending=False).reset_index(drop=True)

                @st.dialog(f"Breach Details: {selected_metric_display} ({selected_threshold_str})")
                def show_breach_details():
                    st.markdown(
                        f"**Filter period:** {st.session_state['applied_start_date']} → {st.session_state['applied_end_date']}  ·  "
                        f"**Server:** {selected_label}  ·  "
                        f"**Threshold:** > {metric_thresh:g}{metric_unit}"
                    )
                    st.divider()

                    # Full scrollable table of all breach samples
                    extra_cols = ["label", "hostname", "cpu_pct", "ram_pct", "disk_pct", "net_latency_ms", "disk_latency_ms"]
                    available_extra = [c for c in extra_cols if c in breach_samples.columns and c != metric_col]
                    show_cols = ["ts", metric_col] + available_extra

                    col_config = {
                        "ts": st.column_config.DatetimeColumn("Timestamp"),
                        metric_col: st.column_config.NumberColumn(
                            f"{selected_metric_display} ({metric_unit.strip()})", format="%.2f"
                        ),
                    }
                    for c in available_extra:
                        if c in ["cpu_pct", "ram_pct", "disk_pct", "net_latency_ms", "disk_latency_ms"]:
                            col_config[c] = st.column_config.NumberColumn(c, format="%.2f")
                        else:
                            col_config[c] = st.column_config.TextColumn(c)

                    st.dataframe(
                        breach_samples[show_cols],
                        use_container_width=True,
                        hide_index=True,
                        column_config=col_config,
                    )

                show_breach_details()
            else:
                st.info(f"No breach samples found in metric data for {selected_metric_display}.")
        else:
            st.warning(f"Unknown metric: {selected_metric_display}")
    else:
        st.toast(f"No breaches to show for {selected_metric_display}.", icon="ℹ️")

# ===== KPI Summary — current value + distribution =====

_section_header("KPI Summary", selected_label)

p1, p2, p3, p4 = st.columns(4, gap="small")

_cpu_now = _latest_value("cpu_pct")
_ram_now = _latest_value("ram_pct")
_disk_now = _latest_value("disk_pct")
_lat_now = _latest_value("net_latency_ms")

with p1:
    with st.container(border=True):
        st.markdown("**💻 CPU**")
        if _cpu_now is not None:
            st.metric("Current", f"{_cpu_now:.1f}%", label_visibility="collapsed")
            st.badge(
                "Above threshold" if _cpu_now > cpu_threshold_val else "Normal",
                icon="🔴" if _cpu_now > cpu_threshold_val else "🟢",
                color="red" if _cpu_now > cpu_threshold_val else "green",
            )
        st.plotly_chart(
            make_cpu_summary_pie(metrics_df),
            use_container_width=True, config={"scrollZoom": True},
            key="chart_cpu_donut",
        )

with p2:
    with st.container(border=True):
        st.markdown("**🧠 RAM**")
        if _ram_now is not None:
            st.metric("Current", f"{_ram_now:.1f}%", label_visibility="collapsed")
            st.badge(
                "Above threshold" if _ram_now > ram_threshold_val else "Normal",
                icon="🔴" if _ram_now > ram_threshold_val else "🟢",
                color="red" if _ram_now > ram_threshold_val else "green",
            )
        st.plotly_chart(
            make_ram_summary_pie(metrics_df),
            use_container_width=True, config={"scrollZoom": True},
            key="chart_ram_donut",
        )

with p3:
    with st.container(border=True):
        st.markdown("**💾 Disk**")
        if _disk_now is not None:
            st.metric("Current", f"{_disk_now:.1f}%", label_visibility="collapsed")
            st.badge(
                "Above threshold" if _disk_now > disk_threshold_val else "Normal",
                icon="🔴" if _disk_now > disk_threshold_val else "🟢",
                color="red" if _disk_now > disk_threshold_val else "green",
            )
        st.plotly_chart(
            make_disk_summary_pie(metrics_df),
            use_container_width=True, config={"scrollZoom": True},
            key="chart_disk_donut",
        )

with p4:
    with st.container(border=True):
        st.markdown("**📶 Latency**")
        if _lat_now is not None:
            st.metric("Current", f"{_lat_now:.1f} ms", label_visibility="collapsed")
            st.badge(
                "Above threshold" if _lat_now > LATENCY_BREACH_THRESHOLD else "Normal",
                icon="🔴" if _lat_now > LATENCY_BREACH_THRESHOLD else "🟢",
                color="red" if _lat_now > LATENCY_BREACH_THRESHOLD else "green",
            )
        st.plotly_chart(
            make_latency_summary_pie(metrics_df),
            use_container_width=True, config={"scrollZoom": True},
            key="chart_latency_donut",
        )

# ===== KPI Trends — CPU · RAM · Disk =====

_section_header("KPI Trends", selected_label)

chart_h = 420

c1, c2, c3 = st.columns(3, gap="small")

with c1:
    with st.container(border=True):
        st.markdown("**💻 CPU Usage**")
        cpu_filter_col, cpu_reset_col = st.columns([4, 1])
        with cpu_filter_col:
            cpu_threshold = st.number_input(
                "CPU breach threshold (%)",
                min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
                key="cpu_threshold_pct",
            )
        with cpu_reset_col:
            st.write("")
            st.button(
                "↺", key="reset_cpu_threshold", help="Reset to default",
                on_click=lambda: st.session_state.update(cpu_threshold_pct=DEFAULT_CPU_THRESHOLD),
            )

        st.plotly_chart(
            make_metric_chart(metrics_df, "cpu_pct", "CPU", chart_h,
                              selected_label=selected_label, y_range=[0, 100],
                              breach_threshold=cpu_threshold),
            use_container_width=False, config={"scrollZoom": True},
            key="chart_cpu_trend",
        )

with c2:
    with st.container(border=True):
        st.markdown("**🧠 RAM Usage**")
        ram_filter_col, ram_reset_col = st.columns([4, 1])
        with ram_filter_col:
            ram_threshold = st.number_input(
                "RAM breach threshold (%)",
                min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
                key="ram_threshold_pct",
            )
        with ram_reset_col:
            st.write("")
            st.button(
                "↺", key="reset_ram_threshold", help="Reset to default",
                on_click=lambda: st.session_state.update(ram_threshold_pct=DEFAULT_RAM_THRESHOLD),
            )

        st.plotly_chart(
            make_metric_chart(metrics_df, "ram_pct", "RAM", chart_h,
                              selected_label=selected_label, y_range=[0, 100],
                              breach_threshold=ram_threshold),
            use_container_width=False, config={"scrollZoom": True},
            key="chart_ram_trend",
        )

with c3:
    with st.container(border=True):
        st.markdown("**💾 Disk Usage**")
        disk_filter_col, disk_reset_col = st.columns([4, 1])
        with disk_filter_col:
            disk_threshold = st.number_input(
                "Disk breach threshold (%)",
                min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
                key="disk_threshold_pct",
            )
        with disk_reset_col:
            st.write("")
            st.button(
                "↺", key="reset_disk_threshold", help="Reset to default",
                on_click=lambda: st.session_state.update(disk_threshold_pct=DEFAULT_DISK_THRESHOLD),
            )

        st.plotly_chart(
            make_metric_chart(metrics_df, "disk_pct", "Disk", chart_h,
                              selected_label=selected_label, y_range=[0, 100],
                              breach_threshold=disk_threshold),
            use_container_width=False, config={"scrollZoom": True},
            key="chart_disk_trend",
        )

# ===== Latency (+ Mesh Ping) · App Metrics =====

_section_header("Latency & Application Health", selected_label)

LATENCY_CAROUSEL_VIEWS = [
    ("latency", "📶 Latency (Net & Disk)"),
    ("mesh_summary", "🌐 Mesh Ping Summary"),
    ("mesh_route", "🌐 Mesh Ping — Avg Latency by Route"),
]

if "latency_carousel_idx" not in st.session_state:
    st.session_state["latency_carousel_idx"] = 0

with st.container(border=True):
    nav_prev, nav_title, nav_next = st.columns([1, 12, 1])
    with nav_prev:
        if st.button("◀", key="latency_carousel_prev", use_container_width=True):
            st.session_state["latency_carousel_idx"] = (
                st.session_state["latency_carousel_idx"] - 1
            ) % len(LATENCY_CAROUSEL_VIEWS)
    with nav_next:
        if st.button("▶", key="latency_carousel_next", use_container_width=True):
            st.session_state["latency_carousel_idx"] = (
                st.session_state["latency_carousel_idx"] + 1
            ) % len(LATENCY_CAROUSEL_VIEWS)

    current_view_key, current_view_label = LATENCY_CAROUSEL_VIEWS[
        st.session_state["latency_carousel_idx"]
    ]
    with nav_title:
        st.markdown(f"**{current_view_label}**")
        st.caption(
            f"View {st.session_state['latency_carousel_idx'] + 1} of "
            f"{len(LATENCY_CAROUSEL_VIEWS)} — use ◀ ▶ to switch"
        )

    if current_view_key == "latency":
        st.plotly_chart(
            make_latency_chart(metrics_df, chart_h, selected_label=selected_label),
            use_container_width=False, config={"scrollZoom": True},
            key="chart_latency_line",
        )
    else:
        mesh_threshold_ms = st.number_input(
            "Expected mesh-ping latency threshold (ms)",
            min_value=1, max_value=1000, value=50,
            key="mesh_carousel_threshold_ms",
        )
        mesh_df = load_mesh_ping_data(start_ts, end_ts, selected_server)

        if mesh_df.empty:
            st.info(f"No mesh ping data for {selected_label} in the selected range.")
        elif current_view_key == "mesh_summary":
            render_mesh_ping_summary_table(
                mesh_df,
                mesh_threshold_ms,
                selected_label,
            )
        else:
            st.plotly_chart(
                make_mesh_route_chart(mesh_df, chart_h, mesh_threshold_ms),
                use_container_width=True, config={"scrollZoom": True},
                key="chart_mesh_route",
            )

# ---- App Metrics, stacked below Latency ----

with st.container(border=True):
    st.markdown("**🧩 App Metrics**")

    app_summary_df = build_app_metrics_summary(app_metrics_df)

    if app_summary_df.empty:
        st.info("No app metric data available for the selected filters.")
    else:
        app_table_event = st.dataframe(
            app_summary_df[["App Name", "Up %", "Down %"]],
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            column_config={
                "App Name": st.column_config.TextColumn("Application"),
                "Up %": st.column_config.ProgressColumn(
                    "Uptime", format="%.1f%%", min_value=0, max_value=100,
                ),
                "Down %": st.column_config.NumberColumn("Down %", format="%.2f%%"),
            },
        )
        st.caption("👆 Click a row to inspect failure samples")

        selected_rows = app_table_event.selection.rows if app_table_event.selection else []

        if selected_rows:
            selected_idx = selected_rows[0]
            selected_app_name = app_summary_df.iloc[selected_idx]["App Name"]
            selected_failure_count = int(app_summary_df.iloc[selected_idx]["Failure Count"])

            if selected_failure_count > 0:
                failure_rows = app_metrics_df[
                    (app_metrics_df["app_name"] == selected_app_name)
                    & (app_metrics_df["status"].isin(["stopped", "error", "restarting"]))
                ].copy()

                failure_rows = failure_rows.sort_values("ts", ascending=False)

                @st.dialog(f"Failure Details — {selected_app_name}")
                def show_failure_details():
                    st.caption(
                        "Detailed non-running app metric samples for the selected app."
                    )

                    display_cols = [
                        "ts",
                        "app_name",
                        "status",
                        "cpu_pct",
                        "rss_memory_mb",
                        "process_count",
                        "thread_count",
                        "listening_sockets",
                        "label",
                        "hostname",
                        "ip_address",
                    ]

                    existing_cols = [col for col in display_cols if col in failure_rows.columns]

                    st.dataframe(
                        failure_rows[existing_cols],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "ts": st.column_config.DatetimeColumn("Timestamp"),
                            "app_name": st.column_config.TextColumn("App Name"),
                            "status": st.column_config.TextColumn("Status"),
                            "cpu_pct": st.column_config.NumberColumn("CPU %", format="%.2f%%"),
                            "rss_memory_mb": st.column_config.NumberColumn("RSS Memory (MB)", format="%.2f"),
                            "process_count": st.column_config.NumberColumn("Processes"),
                            "thread_count": st.column_config.NumberColumn("Threads"),
                            "listening_sockets": st.column_config.NumberColumn("Listening Sockets"),
                            "label": st.column_config.TextColumn("Machine"),
                            "hostname": st.column_config.TextColumn("Hostname"),
                            "ip_address": st.column_config.TextColumn("IP Address"),
                        },
                    )

                show_failure_details()
            else:
                st.success(f"No failures found for {selected_app_name}.")

# ===== Dynamic Registered Metrics =====

registry_df = load_metric_registry()

if not registry_df.empty:
    visible_metrics = [
        row for _, row in registry_df.iterrows()
        if row["column_name"] in metrics_df.columns
    ]

    if visible_metrics:
        _section_header("Custom Metrics", selected_label)

        # Render all registered metrics as normal dashboard charts:
        # 3 charts per row, dynamically based on how many metrics exist.
        for row_start in range(0, len(visible_metrics), 3):
            chart_cols = st.columns(3, gap="small")

            for col_idx, metric in enumerate(visible_metrics[row_start:row_start + 3]):
                col_name = metric["column_name"]
                display_name = metric["display_name"]
                unit = metric["unit"] if pd.notna(metric["unit"]) else ""
                warning_threshold = metric["threshold_warning"]

                yaxis_title = f"{display_name} ({unit})" if unit else display_name
                value_unit = f" {unit}" if unit else ""

                threshold_value = None
                if pd.notna(warning_threshold):
                    threshold_value = float(warning_threshold)

                with chart_cols[col_idx]:
                    with st.container(border=True):
                        st.markdown(f"**{display_name}**")
                        st.plotly_chart(
                            make_metric_chart(
                                metrics_df,
                                col_name,
                                display_name,
                                chart_h,
                                selected_label=selected_label,
                                yaxis_title=yaxis_title,
                                breach_threshold=threshold_value,
                                value_unit=value_unit,
                            ),
                            use_container_width=False,
                            config={"scrollZoom": True},
                            key=f"chart_dynamic_{col_name}",
                        )

# ---------------------------------------------------------------------------
# Detailed Records — merged metrics log + breach events + app metrics
# ---------------------------------------------------------------------------

_section_header("📜 Detailed Records", f"Full history — {selected_label}")

tab_metrics, tab_events, tab_apps, tab_mesh = st.tabs(
    ["📈 Metrics Log", "⚠️ Breach Events", "🧩 App Metrics", "🌐 Mesh Pings"]
)

with tab_metrics:
    if metrics_df.empty:
        st.info("No metric samples found.")
    else:
        selected_metrics_log = metrics_df[metrics_df["server_id"] == selected_server].copy()

        if selected_metrics_log.empty:
            st.info(f"No metric samples found for {selected_label} in the selected range.")
        else:
            # Exactly the metrics charted elsewhere in this dashboard, in one table.
            base_cols = ["ts", "cpu_pct", "ram_pct", "disk_pct", "net_latency_ms", "disk_latency_ms"]
            custom_metric_cols = []
            if not registry_df.empty:
                custom_metric_cols = [
                    c for c in registry_df["column_name"].dropna().astype(str).tolist()
                    if c in selected_metrics_log.columns and c not in base_cols
                ]
            log_cols = [c for c in base_cols + custom_metric_cols if c in selected_metrics_log.columns]

            selected_metrics_log = selected_metrics_log[log_cols].sort_values("ts", ascending=False)

            st.caption(
                f"{len(selected_metrics_log):,} records · {selected_label} · "
                f"{st.session_state['applied_start_date']} → {st.session_state['applied_end_date']}"
            )

            col_config: dict = {
                "ts": st.column_config.DatetimeColumn("Timestamp", format="YYYY-MM-DD HH:mm:ss"),
                "cpu_pct": st.column_config.NumberColumn("CPU %", format="%.2f%%"),
                "ram_pct": st.column_config.NumberColumn("RAM %", format="%.2f%%"),
                "disk_pct": st.column_config.NumberColumn("Disk %", format="%.2f%%"),
                "net_latency_ms": st.column_config.NumberColumn("Net Latency (ms)", format="%.2f"),
                "disk_latency_ms": st.column_config.NumberColumn("Disk Latency (ms)", format="%.2f"),
            }
            for c in custom_metric_cols:
                reg_row = registry_df[registry_df["column_name"] == c].iloc[0]
                unit = reg_row["unit"] if pd.notna(reg_row["unit"]) else ""
                label = f"{reg_row['display_name']} ({unit})" if unit else reg_row["display_name"]
                col_config[c] = st.column_config.NumberColumn(label, format="%.2f")

            st.dataframe(
                selected_metrics_log,
                use_container_width=True,
                hide_index=True,
                column_config=col_config,
            )

            st.download_button(
                "⬇️ Download CSV",
                data=selected_metrics_log.to_csv(index=False).encode("utf-8"),
                file_name="metrics_log.csv",
                mime="text/csv",
                key="dl_metrics_log",
            )

with tab_events:
    if events_df.empty:
        st.info("No threshold‑breach events found.")
    else:
        events_show = events_df[
            ["ts", "server_id", "label", "severity", "metric", "value", "threshold", "message"]
        ].sort_values("ts", ascending=False)

        st.caption(f"{len(events_show):,} breach events")

        st.dataframe(
            events_show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "ts": st.column_config.DatetimeColumn("Timestamp", format="YYYY-MM-DD HH:mm:ss"),
                "server_id": st.column_config.TextColumn("Server ID"),
                "label": st.column_config.TextColumn("Machine"),
                "severity": st.column_config.TextColumn("Severity"),
                "metric": st.column_config.TextColumn("Metric"),
                "value": st.column_config.NumberColumn("Value", format="%.2f"),
                "threshold": st.column_config.NumberColumn("Threshold", format="%.2f"),
                "message": st.column_config.TextColumn("Message", width="large"),
            },
        )

        st.download_button(
            "⬇️ Download CSV",
            data=events_show.to_csv(index=False).encode("utf-8"),
            file_name="breach_events.csv",
            mime="text/csv",
            key="dl_events",
        )

with tab_apps:
    if app_metrics_df.empty:
        st.info("No app metric samples found.")
    else:
        apps_show = app_metrics_df.sort_values("ts", ascending=False)

        st.caption(f"{len(apps_show):,} app metric samples")

        st.dataframe(
            apps_show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "ts": st.column_config.DatetimeColumn("Timestamp", format="YYYY-MM-DD HH:mm:ss"),
                "server_id": st.column_config.TextColumn("Server ID"),
                "label": st.column_config.TextColumn("Machine"),
                "app_name": st.column_config.TextColumn("Application"),
                "status": st.column_config.TextColumn("Status"),
                "cpu_pct": st.column_config.NumberColumn("CPU %", format="%.2f%%"),
                "rss_memory_mb": st.column_config.NumberColumn("RSS Memory (MB)", format="%.2f"),
                "process_count": st.column_config.NumberColumn("Processes"),
                "thread_count": st.column_config.NumberColumn("Threads"),
                "listening_sockets": st.column_config.NumberColumn("Listening Sockets"),
                "hostname": st.column_config.TextColumn("Hostname"),
                "ip_address": st.column_config.TextColumn("IP Address"),
            },
        )

        st.download_button(
            "⬇️ Download CSV",
            data=apps_show.to_csv(index=False).encode("utf-8"),
            file_name="app_metric_samples.csv",
            mime="text/csv",
            key="dl_apps",
        )

with tab_mesh:
    mesh_records_df = load_mesh_ping_data(start_ts, end_ts, selected_server)

    if mesh_records_df.empty:
        st.info(f"No mesh ping results found for {selected_label} in the selected range.")
    else:
        mesh_show = mesh_records_df.sort_values("ts", ascending=False)

        st.caption(f"{len(mesh_show):,} mesh ping records · {selected_label} (as source)")

        st.dataframe(
            mesh_show[["ts", "source_name", "target_name", "target_ip", "success", "latency_ms"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "ts": st.column_config.DatetimeColumn("Timestamp", format="YYYY-MM-DD HH:mm:ss"),
                "source_name": st.column_config.TextColumn("Source"),
                "target_name": st.column_config.TextColumn("Target"),
                "target_ip": st.column_config.TextColumn("Target IP"),
                "success": st.column_config.CheckboxColumn("Success"),
                "latency_ms": st.column_config.NumberColumn("Latency (ms)", format="%.2f"),
            },
        )

        st.download_button(
            "⬇️ Download CSV",
            data=mesh_show.to_csv(index=False).encode("utf-8"),
            file_name="mesh_ping_results.csv",
            mime="text/csv",
            key="dl_mesh",
        )

st.caption(f"Lab Health Dashboard · PostgreSQL · rendered {datetime.now():%Y-%m-%d %H:%M:%S}")
