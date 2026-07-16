"""
seed_mesh_pings.py

Seeds the mesh_ping_results table with 7 days of synthetic mesh-ping data for
three mock machines, PLUS a fleet of healthy web-prod machines to establish
a baseline fleet context.

Each machine is designed to trigger a specific Conditional Brilliance insight:

    Machine                    | Insight Triggered        | Why
    ---------------------------|--------------------------|-------------------------------------------
    mock-cron-target-02        | Temporal Rhythm          | Elevated pings ONLY during 02:00-03:00 UTC
    mock-network-target-03     | Fleet Context Deviation  | Random flapping; high latency z-score
    mock-analytics-target-01   | Ghost Town               | 100 % ping failure from ALL sources
    web-prod-* (fleet baseline)| (none — healthy herd)    | Sub-2ms, 99.9 % success

The "right answer" for each:
    cron:      Mesh latency spikes at 02:00-03:00 UTC daily — confirms cron hypothesis.
    network:   This machine is a severe outlier vs. fleet — issue is host-local, not fleet-wide.
    analytics: Zero connectivity — flag as isolation/crash, NOT as over-provisioned.
"""

import datetime
import random
import psycopg2
from psycopg2.extras import execute_values

DB_CONFIG = {
    "dbname": "lab_monitoring_db",
    "user": "release_user",
    "password": "release_password",
    "host": "127.0.0.1",
    "port": 5432
}

# Column order for metric_samples (matching the real monitoring INSERT from p1_fixed.py):
# (server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct,
#  disk_pct, disk_read_iops, disk_write_iops, disk_latency_ms,
#  net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms, packet_loss_pct,
#  load_avg_1m, load_avg_5m, load_avg_15m, process_count,
#  uptime_seconds, systemd_failed_units_count, raw_extra, status)

# ---------------------------------------------------------------------------
# Server IDs
# ---------------------------------------------------------------------------
CRON_ID = "2b22c5e6-d886-46b3-9791-551fd68feba2"
NETWORK_ID = "3c33c5e6-d886-46b3-9791-551fd68febc3"
ANALYTICS_ID = "870bfbbd-0110-47bb-b04d-b7674e58cff2"

CRON_ALIAS = "mock-cron-target-02"
NETWORK_ALIAS = "mock-network-target-03"
ANALYTICS_ALIAS = "mock-analytics-target-01"

# Healthy fleet machines (a sample of web-prod servers for baseline)
HEALTHY_FLEET = [
    ("1fbd6e5d-b64b-4b4c-9754-c996e6829796", "web-prod-01", "10.0.1.1"),
    ("418c7c66-2020-4462-a1e5-cbbb80c1578d", "web-prod-02", "10.0.1.2"),
    ("eff280f1-4c5a-489f-8156-b21b847a452e", "web-prod-03", "10.0.1.3"),
]

MOCK_IPS = {
    CRON_ALIAS: "10.0.100.2",
    NETWORK_ALIAS: "10.0.100.3",
    ANALYTICS_ALIAS: "10.0.100.1",
}
for _, alias, ip in HEALTHY_FLEET:
    MOCK_IPS[alias] = ip


def build_normal_ping(source_id, source_alias, target_id, target_alias, target_ip, ts):
    """Normal healthy mesh ping: sub-2ms, ~99.9 % success."""
    success = random.random() > 0.001  # 0.1 % natural packet loss
    latency = round(random.uniform(0.3, 2.0), 2) if success else None
    return (source_id, source_alias, target_id, target_alias, target_ip, success, latency, ts)


def build_cron_ping(source_id, source_alias, target_id, target_alias, target_ip, ts):
    """
    Ping TO mock-cron-target-02.
    During 02:00-03:00 UTC: elevated latency (10-50ms, 5% failure).
    Outside that window: normal latency.
    """
    is_backup_window = (2 <= ts.hour < 3)
    if is_backup_window:
        success = random.random() > 0.05  # 5 % failure
        latency = round(random.uniform(10.0, 50.0), 2) if success else None
    else:
        success = random.random() > 0.001
        latency = round(random.uniform(0.3, 3.0), 2) if success else None
    return (source_id, source_alias, target_id, target_alias, target_ip, success, latency, ts)


def build_network_ping(source_id, source_alias, target_id, target_alias, target_ip, ts):
    """
    Ping TO mock-network-target-03.
    70% normal, 30% flapping — during flapping, 200-500ms or failure.
    The flapping is random (not time-based) to simulate intermittent degradation.
    """
    is_flapping = (random.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) < 3)  # ~30% of pings
    if is_flapping:
        success = random.random() > 0.25  # 25 % failure during flapping
        latency = round(random.uniform(180.0, 500.0), 2) if success else None
    else:
        success = random.random() > 0.001
        latency = round(random.uniform(0.3, 2.0), 2) if success else None
    return (source_id, source_alias, target_id, target_alias, target_ip, success, latency, ts)


def build_analytics_ping(source_id, source_alias, target_id, target_alias, target_ip, ts):
    """
    Ping TO mock-analytics-target-01.
    100% failure — always. This machine is completely isolated (Ghost Town).
    """
    return (source_id, source_alias, target_id, target_alias, target_ip, False, None, ts)


def _seed_analytics_metrics(conn, cursor):
    """
    Seed metric_samples for mock-analytics-target-01 with near-zero values
    to complete the Ghost Town picture: negligible CPU, tiny RAM, zero throughput.
    """
    end_time = datetime.datetime.now(datetime.timezone.utc)
    start_time = end_time - datetime.timedelta(days=7)
    interval = datetime.timedelta(minutes=5)

    metrics = []
    current_time = start_time
    while current_time < end_time:
        # Ghost Town: barely alive — ~1% CPU, ~2% RAM, zero disk/network activity
        # Column order matches p1_fixed.py's INSERT: server_id, ts, source_mode,
        # cpu_pct, ram_pct, swap_pct, disk_pct, disk_read_iops, disk_write_iops,
        # disk_latency_ms, net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms,
        # packet_loss_pct, load_avg_1m, load_avg_5m, load_avg_15m, process_count,
        # uptime_seconds, systemd_failed_units_count, raw_extra, status
        metrics.append((
            ANALYTICS_ID, current_time, "standard",
            0.8,    # cpu_pct — barely a pulse
            2.1,    # ram_pct
            0.0,    # swap_pct
            12.0,   # disk_pct — static, no growth
            0.0,    # disk_read_iops
            0.0,    # disk_write_iops
            0.0,    # disk_latency_ms
            0,      # net_rx_bytes_sec — nothing (bigint)
            0,      # net_tx_bytes_sec — nothing (bigint)
            0.0,    # net_latency_ms
            0.0,    # packet_loss_pct
            0.01,   # load_avg_1m
            0.01,   # load_avg_5m
            0.01,   # load_avg_15m
            2,      # process_count — almost nothing running
            0,      # uptime_seconds
            0,      # systemd_failed_units_count
            '{}',   # raw_extra — empty jsonb
            "partial"  # status — agent responds but nobody can reach it
        ))
        current_time += interval

    # Clear any existing data for this machine
    cursor.execute(
        "DELETE FROM metric_samples WHERE server_id = %s",
        (ANALYTICS_ID,)
    )

    # Batch insert (matching p1_fixed.py column order)
    insert_sql = """
        INSERT INTO metric_samples
            (server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct,
             disk_pct, disk_read_iops, disk_write_iops, disk_latency_ms,
             net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms, packet_loss_pct,
             load_avg_1m, load_avg_5m, load_avg_15m, process_count,
             uptime_seconds, systemd_failed_units_count, raw_extra, status)
        VALUES %s
    """
    execute_values(cursor, insert_sql, metrics)
    conn.commit()
    print(f"Seeded {len(metrics)} metric_samples rows for mock-analytics-target-01 (Ghost Town: near-zero activity).")


def seed_mesh_data():
    """Generate and insert 7 days of mesh ping data for all scenarios."""
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()

    end_time = datetime.datetime.now(datetime.timezone.utc)
    start_time = end_time - datetime.timedelta(days=7)
    interval = datetime.timedelta(minutes=5)

    all_rows = []
    current_time = start_time

    while current_time < end_time:
        ts = current_time

        # ------------------------------------------------------------------
        # 1. Healthy fleet ↔ fleet (baseline)
        # ------------------------------------------------------------------
        for src_id, src_alias, src_ip in HEALTHY_FLEET:
            for tgt_id, tgt_alias, tgt_ip in HEALTHY_FLEET:
                if src_id == tgt_id:
                    continue  # don't ping yourself
                row = build_normal_ping(src_id, src_alias, tgt_id, tgt_alias, tgt_ip, ts)
                all_rows.append(row)

        # ------------------------------------------------------------------
        # 2. Healthy fleet → mock-cron-target-02 (temporal rhythm)
        # ------------------------------------------------------------------
        for src_id, src_alias, _ in HEALTHY_FLEET:
            row = build_cron_ping(src_id, src_alias, CRON_ID, CRON_ALIAS, MOCK_IPS[CRON_ALIAS], ts)
            all_rows.append(row)

        # ------------------------------------------------------------------
        # 3. Healthy fleet → mock-network-target-03 (fleet deviation)
        # ------------------------------------------------------------------
        for src_id, src_alias, _ in HEALTHY_FLEET:
            row = build_network_ping(src_id, src_alias, NETWORK_ID, NETWORK_ALIAS, MOCK_IPS[NETWORK_ALIAS], ts)
            all_rows.append(row)

        # ------------------------------------------------------------------
        # 4. Healthy fleet → mock-analytics-target-01 (Ghost Town)
        # ------------------------------------------------------------------
        for src_id, src_alias, _ in HEALTHY_FLEET:
            row = build_analytics_ping(src_id, src_alias, ANALYTICS_ID, ANALYTICS_ALIAS, MOCK_IPS[ANALYTICS_ALIAS], ts)
            all_rows.append(row)

        # ------------------------------------------------------------------
        # 5. Mock machines → each other (full mesh)
        # ------------------------------------------------------------------
        mock_machines = [
            (CRON_ID, CRON_ALIAS, MOCK_IPS[CRON_ALIAS]),
            (NETWORK_ID, NETWORK_ALIAS, MOCK_IPS[NETWORK_ALIAS]),
            (ANALYTICS_ID, ANALYTICS_ALIAS, MOCK_IPS[ANALYTICS_ALIAS]),
        ]
        for src_id, src_alias, src_ip in mock_machines:
            for tgt_id, tgt_alias, tgt_ip in mock_machines:
                if src_id == tgt_id:
                    continue
                # Pick the right scenario based on target
                if tgt_id == CRON_ID:
                    row = build_cron_ping(src_id, src_alias, tgt_id, tgt_alias, tgt_ip, ts)
                elif tgt_id == NETWORK_ID:
                    row = build_network_ping(src_id, src_alias, tgt_id, tgt_alias, tgt_ip, ts)
                elif tgt_id == ANALYTICS_ID:
                    row = build_analytics_ping(src_id, src_alias, tgt_id, tgt_alias, tgt_ip, ts)
                else:
                    row = build_normal_ping(src_id, src_alias, tgt_id, tgt_alias, tgt_ip, ts)
                all_rows.append(row)

        # ------------------------------------------------------------------
        # 6. Mock machines → healthy fleet (reverse direction)
        # ------------------------------------------------------------------
        for tgt_id, tgt_alias, tgt_ip in HEALTHY_FLEET:
            # cron pings healthy
            row = build_normal_ping(CRON_ID, CRON_ALIAS, tgt_id, tgt_alias, tgt_ip, ts)
            all_rows.append(row)
            # network pings healthy
            row = build_normal_ping(NETWORK_ID, NETWORK_ALIAS, tgt_id, tgt_alias, tgt_ip, ts)
            all_rows.append(row)
            # analytics pings healthy — IT can still send packets out,
            # but nobody can reach IT. This means the machine is alive
            # but its services are not listening.
            row = build_normal_ping(ANALYTICS_ID, ANALYTICS_ALIAS, tgt_id, tgt_alias, tgt_ip, ts)
            all_rows.append(row)

        current_time += interval

    # Clear existing mesh data for our mock machines to allow re-runs
    mock_ids = [CRON_ID, NETWORK_ID, ANALYTICS_ID]
    fleet_ids = [m[0] for m in HEALTHY_FLEET]
    all_mock_ids = mock_ids + fleet_ids

    cursor.execute(
        "DELETE FROM mesh_ping_results WHERE "
        "source_server_id = ANY(%s::uuid[]) OR target_server_id = ANY(%s::uuid[])",
        (all_mock_ids, all_mock_ids)
    )
    conn.commit()
    print(f"Cleared existing mesh data for {len(all_mock_ids)} machines.")

    # Batch insert mesh pings
    insert_sql = """
        INSERT INTO mesh_ping_results
            (source_server_id, source_alias, target_server_id, target_alias,
             target_ip, success, latency_ms, ts)
        VALUES %s
    """
    value_rows = [
        (src_id, src_alias, tgt_id, tgt_alias, tgt_ip, success, latency, ts)
        for (src_id, src_alias, tgt_id, tgt_alias, tgt_ip, success, latency, ts) in all_rows
    ]

    execute_values(cursor, insert_sql, value_rows)
    conn.commit()
    print(f"Seeded {len(value_rows)} mesh ping rows across 7 days.")

    # ------------------------------------------------------------------
    # Bonus: seed metric_samples for mock-analytics-target-01 with
    # near-zero values so the Ghost Town analysis is complete
    # ------------------------------------------------------------------
    _seed_analytics_metrics(conn, cursor)

    cursor.close()
    conn.close()

    print()
    print("--- Scenario Summary ---")
    print(f"  mock-cron-target-02:     Elevated mesh latency during 02:00-03:00 UTC (Temporal Rhythm test)")
    print(f"  mock-network-target-03:  Intermittent flapping 180-500ms (Fleet Context deviation test)")
    print(f"  mock-analytics-target-01: 100% ping failure from ALL sources (Ghost Town test)")
    print(f"  web-prod-* fleet:        Sub-2ms baseline (healthy herd)")


if __name__ == "__main__":
    seed_mesh_data()
