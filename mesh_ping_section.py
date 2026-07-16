import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import streamlit as st


DB_CONFIG = {
    "host": os.getenv("P1_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("P1_DB_PORT", "5432")),
    "dbname": os.getenv("P1_DB_NAME", "lab_monitoring_db"),
    "user": os.getenv("P1_DB_USER", "release_user"),
    "password": os.getenv("P1_DB_PASSWORD", os.getenv("DB_PASSWORD", "")),
}


def query_df(sql, params=None):
    with psycopg2.connect(**DB_CONFIG) as conn:
        return pd.read_sql_query(sql, conn, params=params or [])


def make_demo_data():
    now = datetime.now()
    rows = []

    source = "gcp_latest"
    targets = [
        "gcp_testing",
        "gcp_previous",
        "web-prod-01",
        "web-prod-02",
        "web-prod-03",
        "web-prod-04",
    ]

    demo_latencies = {
        "gcp_testing": [22, 25, 23, 26, 21, 24],
        "gcp_previous": [45, 49, 52, 48, 51, 47],
        "web-prod-01": [35, 38, 34, 36, 37, 35],
        "web-prod-02": [72, 85, 91, 77, 88, 95],
        "web-prod-03": [18, 20, 19, 21, 18, 22],
        "web-prod-04": [None, None, 65, None, 70, None],
    }

    for target in targets:
        for i, latency in enumerate(demo_latencies[target]):
            success = latency is not None
            rows.append(
                {
                    "ts": now - timedelta(hours=i * 4),
                    "source_name": source,
                    "target_name": target,
                    "target_ip": "demo",
                    "success": success,
                    "latency_ms": latency,
                }
            )

    return pd.DataFrame(rows)


def load_real_data():
    sql = """
        SELECT
            mps.ts,
            COALESCE(s.alias, s.hostname, s.ip_address::text) AS source_name,
            COALESCE(t.alias, t.hostname, mps.target_ip::text) AS target_name,
            mps.target_ip::text AS target_ip,
            mps.success,
            mps.latency_ms
        FROM mesh_ping_samples mps
        JOIN machines s ON s.server_id = mps.source_server_id
        JOIN machines t ON t.server_id = mps.target_server_id
        WHERE mps.ts >= NOW() - INTERVAL '2 days'
        ORDER BY mps.ts DESC;
    """
    try:
        return query_df(sql)
    except Exception as e:
        st.warning(f"Could not load real mesh ping data: {e}")
        return pd.DataFrame()


def add_colored_latency_line(fig, avg_df, threshold_ms):
    avg_df = avg_df.copy()

    if "route_label" not in avg_df.columns:
        avg_df["route_label"] = avg_df["source_name"] + " → " + avg_df["target_name"]

    avg_df = avg_df.sort_values("route_label").reset_index(drop=True)

    # For segment A → B, color is determined by the far endpoint B.
    # So each segment uses the status of the point it ends at.
    for i in range(1, len(avg_df)):
        prev_row = avg_df.iloc[i - 1]
        curr_row = avg_df.iloc[i]

        y1 = prev_row["avg_latency_ms"]
        y2 = curr_row["avg_latency_ms"]

        if pd.isna(y1) or pd.isna(y2):
            continue

        endpoint_is_breach = y2 > threshold_ms

        fig.add_trace(
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

    normal_df = avg_df[
        avg_df["avg_latency_ms"].notna()
        & (avg_df["avg_latency_ms"] <= threshold_ms)
    ]

    breach_df = avg_df[
        avg_df["avg_latency_ms"].notna()
        & (avg_df["avg_latency_ms"] > threshold_ms)
    ]

    if not normal_df.empty:
        fig.add_trace(
            go.Scatter(
                x=normal_df["route_label"],
                y=normal_df["avg_latency_ms"],
                mode="markers",
                name="At / below threshold",
                marker=dict(size=10, color="#60a5fa"),
                customdata=normal_df[["source_name", "target_name"]],
                hovertemplate=(
                    "Source: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Average latency: %{y:.2f} ms<br>"
                    "Status: Normal<extra></extra>"
                ),
            )
        )

    if not breach_df.empty:
        fig.add_trace(
            go.Scatter(
                x=breach_df["route_label"],
                y=breach_df["avg_latency_ms"],
                mode="markers",
                name="Above threshold",
                marker=dict(size=13, color="red"),
                customdata=breach_df[["source_name", "target_name"]],
                hovertemplate=(
                    "Source: %{customdata[0]}<br>"
                    "Target: %{customdata[1]}<br>"
                    "Average latency: %{y:.2f} ms<br>"
                    "Status: Above threshold<extra></extra>"
                ),
            )
        )


def render_mesh_ping_section():
    st.header("Mesh Ping Dashboard")
    st.caption(
        "Mesh ping view showing average latency and threshold breaches "
        "for a selected source machine over the last 2 days."
    )

    threshold_ms = st.number_input(
        "Expected latency threshold (ms)",
        min_value=1,
        max_value=1000,
        value=50,
    )

    real_df = load_real_data()

    if real_df.empty:
        st.info(
            "No real mesh_ping_samples rows found in local database. "
            "Showing demo data to validate graph layout."
        )
        df = make_demo_data()
        data_mode = "Demo data"
    else:
        df = real_df
        data_mode = "Real database data"

    st.caption(f"Data mode: **{data_mode}**")

    source_options = sorted(df["source_name"].dropna().unique())
    source_name = st.selectbox("Select source machine", source_options)

    source_df = df[df["source_name"] == source_name].copy()

    if source_df.empty:
        st.warning("No data found for this source machine.")
        st.stop()

    source_df["status"] = source_df.apply(
        lambda row: (
            "FAILED"
            if not row["success"]
            else "BREACH"
            if pd.notna(row["latency_ms"]) and row["latency_ms"] > threshold_ms
            else "NORMAL"
        ),
        axis=1,
    )

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
    c1.metric("Source machine", source_name)
    c2.metric("Total records", total_records)
    c3.metric("Breaches / failures", total_breaches)
    c4.metric("Average latency", "N/A" if pd.isna(overall_avg) else f"{overall_avg:.2f} ms")

    st.divider()

    st.subheader("Average latency by mesh route")

    avg_fig = go.Figure()
    add_colored_latency_line(avg_fig, agg_df, threshold_ms)

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

    st.subheader("Breach and failure events over time")

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
                customdata=normal_points[["target_name", "status"]],
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Target: %{customdata[0]}<br>"
                    "Latency: %{y:.2f} ms<br>"
                    "Status: %{customdata[1]}<extra></extra>"
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
                customdata=breach_points[["target_name", "status"]],
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Target: %{customdata[0]}<br>"
                    "Latency: %{y:.2f} ms<br>"
                    "Status: %{customdata[1]}<extra></extra>"
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
                customdata=failed_points[["target_name", "status"]],
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Target: %{customdata[0]}<br>"
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

    st.subheader("Raw mesh ping records")

    display_df = source_df[
        [
            "ts",
            "source_name",
            "target_name",
            "target_ip",
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

    st.caption(
        "The dashboard reads from mesh_ping_samples when rows exist. "
        "Demo data is only used when the local table is empty."
    )
