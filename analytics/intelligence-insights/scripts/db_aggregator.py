import json
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "dbname": "lab_monitoring_db",
    "user": "release_user",
    "password": "release_password",
    "host": "127.0.0.1",
    "port": 5432
}

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

METADATA_COLUMNS = {
    'id', 'server_id', 'ts', 'source_mode',
    'raw_extra', 'status', 'created_at'
}

# Key correlation pairs known to reveal hidden infrastructure bottlenecks
CORRELATION_PAIRS = [
    ("cpu_pct", "net_latency_ms", "CPU \u2194 Network Latency"),
    ("net_rx_bytes_sec", "disk_latency_ms", "Network Rx \u2194 Disk Latency"),
    ("disk_write_iops", "cpu_pct", "Disk Write \u2194 CPU"),
    ("ram_pct", "cpu_pct", "RAM \u2194 CPU"),
    ("packet_loss_pct", "net_latency_ms", "Packet Loss \u2194 Network Latency"),
    ("net_rx_bytes_sec", "net_tx_bytes_sec", "Network Rx \u2194 Tx"),
]


# ---------------------------------------------------------------------------
# Schema Introspection
# ---------------------------------------------------------------------------

def discover_telemetry_metrics(cursor):
    """
    Queries PostgreSQL system catalogs to introspect the metric_samples table.
    Filters out metadata columns, keeping only valid numeric metric fields.
    """
    query = """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'metric_samples';
    """
    cursor.execute(query)
    columns = cursor.fetchall()

    metric_fields = []
    for col in columns:
        col_name = col['column_name']
        data_type = col['data_type']
        if col_name not in METADATA_COLUMNS and data_type in ('numeric', 'bigint', 'integer', 'double precision'):
            metric_fields.append(col_name)

    return metric_fields


# ---------------------------------------------------------------------------
# Machine Info
# ---------------------------------------------------------------------------

def get_machine_info(cursor, server_id):
    """Fetch machine metadata from the machines table."""
    cursor.execute(
        "SELECT server_id, alias, hostname, ip_address, cpu_cores, ram_gb "
        "FROM machines WHERE server_id = %s", (server_id,)
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return {"server_id": server_id, "alias": "unknown", "hostname": "unknown"}


# ---------------------------------------------------------------------------
# 1. Daily Rollups (existing functionality)
# ---------------------------------------------------------------------------

def get_daily_rollups(cursor, server_id, metrics, lookback_days=7):
    """Time-bucketed daily averages and maximums for every metric."""
    select_clauses = ["date_trunc('day', ts) AS metric_day"]
    for m in metrics:
        select_clauses.append(f"ROUND(AVG({m})::numeric, 2) AS avg_{m}")
        select_clauses.append(f"ROUND(MAX({m})::numeric, 2) AS max_{m}")

    sql = f"""
        SELECT {', '.join(select_clauses)}
        FROM public.metric_samples
        WHERE server_id = %s AND ts >= NOW() - INTERVAL %s
        GROUP BY metric_day
        ORDER BY metric_day ASC;
    """
    cursor.execute(sql, (server_id, f"{lookback_days} days"))
    rows = cursor.fetchall()

    rollups = []
    for row in rows:
        row_dict = dict(row)
        if row_dict.get('metric_day'):
            row_dict['metric_day'] = row_dict['metric_day'].strftime('%Y-%m-%d')
        cleaned = {}
        for k, v in row_dict.items():
            cleaned[k] = float(v) if isinstance(v, Decimal) else v
        rollups.append(cleaned)

    return rollups


# ---------------------------------------------------------------------------
# 2. Predictive Capacity Runway (Time-Series Trend Analysis)
# ---------------------------------------------------------------------------

def get_trend_analysis(cursor, server_id, metrics, lookback_days=7):
    """
    Computes linear regression slope (units/day) and R-squared for each metric
    against time. Slope is derived from regr_slope(metric, epoch_seconds) * 86400.
    R-squared indicates statistical significance of the trend.
    """
    slope_clauses = []
    r2_clauses = []
    for m in metrics:
        slope_clauses.append(
            f"COALESCE(ROUND(regr_slope({m}, EXTRACT(epoch FROM ts))::numeric * 86400, 4), 0) AS trend_slope_{m}"
        )
        r2_clauses.append(
            f"COALESCE(ROUND(regr_r2({m}, EXTRACT(epoch FROM ts))::numeric, 4), 0) AS trend_r2_{m}"
        )

    sql = f"""
        SELECT {', '.join(slope_clauses + r2_clauses)}
        FROM public.metric_samples
        WHERE server_id = %s AND ts >= NOW() - INTERVAL %s
    """
    cursor.execute(sql, (server_id, f"{lookback_days} days"))
    row = cursor.fetchone()

    if not row:
        return {}

    result = {}
    for m in metrics:
        slope_key = f"trend_slope_{m}"
        r2_key = f"trend_r2_{m}"
        if slope_key in row and r2_key in row:
            slope_val = float(row[slope_key]) if row[slope_key] is not None else 0.0
            r2_val = float(row[r2_key]) if row[r2_key] is not None else 0.0
            result[m] = {
                "slope_per_day": slope_val,
                "r_squared": r2_val
            }
    return result


# ---------------------------------------------------------------------------
# 3. Temporal Rhythm / "Cron Blame" (Hourly Spike Analysis)
# ---------------------------------------------------------------------------

def get_temporal_analysis(cursor, server_id, metrics, lookback_days=7):
    """
    Hourly-bucketed aggregation to detect cron-like recurring patterns.
    Returns avg, max, and sample_count per hour for each metric so Hermes
    can determine if >80% of spikes fall within a tight daily window.
    """
    avg_clauses = [f"ROUND(AVG({m})::numeric, 2) AS avg_{m}" for m in metrics]
    max_clauses = [f"ROUND(MAX({m})::numeric, 2) AS max_{m}" for m in metrics]

    sql = f"""
        SELECT
            EXTRACT(HOUR FROM ts)::int AS hour_bucket,
            COUNT(*) AS sample_count,
            {', '.join(avg_clauses + max_clauses)}
        FROM public.metric_samples
        WHERE server_id = %s AND ts >= NOW() - INTERVAL %s
        GROUP BY hour_bucket
        ORDER BY hour_bucket
    """
    cursor.execute(sql, (server_id, f"{lookback_days} days"))
    rows = cursor.fetchall()

    result = []
    for row in rows:
        entry = {
            "hour": int(row["hour_bucket"]),
            "sample_count": int(row["sample_count"])
        }
        for m in metrics:
            avg_key = f"avg_{m}"
            max_key = f"max_{m}"
            if avg_key in row:
                entry[avg_key] = float(row[avg_key]) if row[avg_key] is not None else 0.0
            if max_key in row:
                entry[max_key] = float(row[max_key]) if row[max_key] is not None else 0.0
        result.append(entry)

    return result


# ---------------------------------------------------------------------------
# 4. Fleet Cohort Deviation (Fleet-Wide Context)
# ---------------------------------------------------------------------------

def get_fleet_statistics(cursor, metrics, lookback_days=7):
    """
    Compute fleet-wide average and standard deviation for each metric
    across all machines. Used to detect machines that deviate from the herd.
    """
    clauses = []
    for m in metrics:
        clauses.append(f"ROUND(AVG({m})::numeric, 2) AS fleet_avg_{m}")
        clauses.append(f"ROUND(STDDEV({m})::numeric, 2) AS fleet_stddev_{m}")

    sql = f"""
        SELECT {', '.join(clauses)}
        FROM public.metric_samples
        WHERE ts >= NOW() - INTERVAL %s
    """
    cursor.execute(sql, (f"{lookback_days} days",))
    row = cursor.fetchone()

    if not row:
        return {}

    result = {}
    for m in metrics:
        fa_key = f"fleet_avg_{m}"
        fs_key = f"fleet_stddev_{m}"
        entry = {}
        if fa_key in row:
            entry["fleet_avg"] = float(row[fa_key]) if row[fa_key] is not None else 0.0
        if fs_key in row:
            entry["fleet_stddev"] = float(row[fs_key]) if row[fs_key] is not None else 0.0
        if entry:
            result[m] = entry
    return result


def get_machine_z_scores(cursor, server_id, metrics, fleet_stats, lookback_days=7):
    """
    Compute z-scores for the target machine relative to fleet-wide stats.
    A |z-score| > 2 indicates a significant deviation from the fleet.
    """
    avg_clauses = [f"ROUND(AVG({m})::numeric, 2) AS avg_{m}" for m in metrics]

    sql = f"""
        SELECT {', '.join(avg_clauses)}
        FROM public.metric_samples
        WHERE server_id = %s AND ts >= NOW() - INTERVAL %s
    """
    cursor.execute(sql, (server_id, f"{lookback_days} days"))
    row = cursor.fetchone()

    if not row:
        return {}

    result = {}
    for m in metrics:
        if m not in fleet_stats:
            continue
        fs = fleet_stats[m]
        machine_avg = float(row.get(f"avg_{m}", 0) or 0)
        fleet_avg = fs.get("fleet_avg", 0)
        fleet_stddev = fs.get("fleet_stddev", 0)

        z_score = 0.0
        if fleet_stddev > 0:
            z_score = round((machine_avg - fleet_avg) / fleet_stddev, 2)

        result[m] = {
            "this_machine_avg": machine_avg,
            "fleet_avg": fleet_avg,
            "fleet_stddev": fleet_stddev,
            "z_score": z_score
        }
    return result


# ---------------------------------------------------------------------------
# 5. Cross-Metric Correlation (Hidden Bottleneck Detection)
# ---------------------------------------------------------------------------

def get_cross_correlations(cursor, server_id, lookback_days=7):
    """
    Compute Pearson correlation coefficients for predefined metric pairs.
    Strong correlations (|r| > 0.7) reveal hidden systemic relationships.
    """
    select_parts = []
    pair_metadata = []
    for col_a, col_b, label in CORRELATION_PAIRS:
        alias = f"corr_{col_a}_{col_b}".replace(".", "_")
        select_parts.append(f"COALESCE(ROUND(corr({col_a}, {col_b})::numeric, 4), 0) AS {alias}")
        pair_metadata.append((col_a, col_b, label, alias))

    sql = f"""
        SELECT {', '.join(select_parts)}
        FROM public.metric_samples
        WHERE server_id = %s AND ts >= NOW() - INTERVAL %s
    """
    cursor.execute(sql, (server_id, f"{lookback_days} days"))
    row = cursor.fetchone()

    if not row:
        return []

    results = []
    for col_a, col_b, label, alias in pair_metadata:
        val = float(row[alias]) if row[alias] is not None else 0.0
        abs_val = abs(val)
        if abs_val >= 0.7:
            strength = "strong"
        elif abs_val >= 0.4:
            strength = "moderate"
        else:
            strength = "weak"

        results.append({
            "pair": label,
            "metric_a": col_a,
            "metric_b": col_b,
            "pearson": val,
            "strength": strength
        })

    return results


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

def generate_enhanced_report(server_id, lookback_days=7):
    """
    Full single-machine analysis: rollups, trends, temporal patterns,
    fleet context, and cross-correlations.
    """
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        metrics = discover_telemetry_metrics(cursor)
        if not metrics:
            print("Error: No numeric telemetry metrics found in schema.")
            return None

        print(f"Dynamic Schema Engine discovered {len(metrics)} active metrics: {metrics}")

        machine_info = get_machine_info(cursor, server_id)
        daily_rollups = get_daily_rollups(cursor, server_id, metrics, lookback_days)
        trends = get_trend_analysis(cursor, server_id, metrics, lookback_days)
        temporal = get_temporal_analysis(cursor, server_id, metrics, lookback_days)
        fleet_stats = get_fleet_statistics(cursor, metrics, lookback_days)
        z_scores = get_machine_z_scores(cursor, server_id, metrics, fleet_stats, lookback_days)
        correlations = get_cross_correlations(cursor, server_id, lookback_days)

        return {
            "mode": "single",
            "machine": machine_info,
            "daily_rollups": daily_rollups,
            "trends": trends,
            "temporal_patterns": temporal,
            "fleet_context": z_scores,
            "correlations": correlations
        }

    except Exception as e:
        print(f"Database Analysis Error: {e}")
        return None
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()


def generate_fleet_overview(lookback_days=7):
    """Fleet-wide analysis across all registered machines."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("SELECT server_id, alias, hostname FROM machines")
        machines = cursor.fetchall()

        if not machines:
            return {"mode": "fleet", "machine_count": 0, "machines": [], "fleet_averages": {}}

        metrics = discover_telemetry_metrics(cursor)
        fleet_stats = get_fleet_statistics(cursor, metrics, lookback_days)

        machine_reports = []
        for machine in machines:
            sid = machine["server_id"]
            daily = get_daily_rollups(cursor, sid, metrics, lookback_days)
            trends = get_trend_analysis(cursor, sid, metrics, lookback_days)
            temporal = get_temporal_analysis(cursor, sid, metrics, lookback_days)
            z_scores = get_machine_z_scores(cursor, sid, metrics, fleet_stats, lookback_days)
            correlations = get_cross_correlations(cursor, sid, lookback_days)

            machine_reports.append({
                "machine": dict(machine),
                "daily_rollups": daily,
                "trends": trends,
                "temporal_patterns": temporal,
                "fleet_context": z_scores,
                "correlations": correlations
            })

        return {
            "mode": "fleet",
            "machine_count": len(machine_reports),
            "machines": machine_reports,
            "fleet_averages": fleet_stats
        }

    except Exception as e:
        print(f"Fleet Overview Error: {e}")
        return None
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    lookback = 7
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            lookback = int(sys.argv[idx + 1])

    if "--machine" in sys.argv and len(sys.argv) > sys.argv.index("--machine") + 1:
        idx = sys.argv.index("--machine")
        target = sys.argv[idx + 1]
        report = generate_enhanced_report(target, lookback)
    else:
        report = generate_fleet_overview(lookback)

    if report:
        print(json.dumps(report, cls=DecimalEncoder, indent=2))
    else:
        print(json.dumps({"error": "Failed to generate report"}, indent=2))
        sys.exit(1)
