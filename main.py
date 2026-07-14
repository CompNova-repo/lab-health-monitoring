#!/usr/bin/env python3
"""
main.py — Hermes-driven orchestrator for adding new monitoring metrics.

Usage (Hermes provides the shell command):
    python3 main.py --metric "Established Connections" \\
                    --command "ss -t state established | wc -l" \\
                    --display-name "Established Connections" \\
                    --unit "count" --db-type "INTEGER"

Usage (pre-canned catalog — backward compat):
    python3 main.py --metric "inode_usage_percentage"

This script:
  1. Resolves the metric name to a shell command (from --command or the catalog).
  2. Loads ALL active machines from the machines table and tries each one via SSH
     until one succeeds (gracefully skipping unreachable machines).
  3. Writes a plugin script into new-metrics/<key>.py that can execute the command
     and return {"value": ..., "status": ..., "raw_output": ...}.
  4. Appends the metric to metric_registry.yaml.
  5. Adds a dynamic column to metric_samples in PostgreSQL.

Hermes should NEVER edit this file to add new metrics — use --command instead.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    psycopg2 = None


ROOT = Path(__file__).resolve().parent
NEW_METRICS_DIR = ROOT / "new-metrics"
REGISTRY_FILE = ROOT / "metric_registry.yaml"

DB_DSN = {
    "host": os.environ.get("P1_DB_HOST", "127.0.0.1"),
    "port": os.environ.get("P1_DB_PORT", "5432"),
    "dbname": os.environ.get("P1_DB_NAME", "lab_monitoring_db"),
    "user": os.environ.get("P1_DB_USER", "release_user"),
    "password": os.environ.get("P1_DB_PASSWORD", "release_password"),
    "connect_timeout": 5,
}

# ---------------------------------------------------------------------------
# Hardcoded catalog — only used as a fallback when --command is NOT provided.
# New metrics should use --command instead of adding entries here.
# ---------------------------------------------------------------------------
METRIC_COMMANDS = {
    "inode_usage_percentage": {
        "display_name": "Inode Usage Percentage",
        "unit": "%",
        "db_type": "DOUBLE PRECISION",
        "command": "df -Pi / | awk 'NR==2 {gsub(/%/, \"\", $5); print $5}'",
    },
    "cpu_iowait_percentage": {
        "display_name": "CPU I/O Wait Percentage",
        "unit": "%",
        "db_type": "DOUBLE PRECISION",
        "command": "top -bn1 | awk '/Cpu\\(s\\)/ {gsub(/,/, \"\"); for(i=1;i<=NF;i++) if ($i ~ /wa/) print $(i-1)}'",
    },
    "load_average_1m": {
        "display_name": "Load Average 1m",
        "unit": "",
        "db_type": "DOUBLE PRECISION",
        "command": "cat /proc/loadavg | awk '{print $1}'",
    },
    "established_connections": {
        "display_name": "Established Connections",
        "unit": "count",
        "db_type": "INTEGER",
        "command": "ss -t state established | wc -l",
    },
    "time_wait_connections": {
        "display_name": "TIME_WAIT Connections",
        "unit": "count",
        "db_type": "INTEGER",
        "command": "ss -t state time-wait | wc -l",
    },
}

METRIC_ALIASES = {
    "inode_usage": "inode_usage_percentage",
    "inode_used_percentage": "inode_usage_percentage",
    "inode_used_pct": "inode_usage_percentage",
    "cpu_iowait": "cpu_iowait_percentage",
    "cpu_iowait_pct": "cpu_iowait_percentage",
    "iowait": "cpu_iowait_percentage",
}

_SAFE_COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ALLOWED_DB_TYPES = {"DOUBLE PRECISION", "INTEGER", "BIGINT", "TEXT", "BOOLEAN"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify_metric_name(metric_name):
    """Normalise a user-provided metric name into a safe DB column key."""
    key = metric_name.strip().lower()
    key = key.replace("%", "percentage")
    key = re.sub(r"[^a-z0-9]+", "_", key)
    key = re.sub(r"_+", "_", key).strip("_")

    if not key:
        raise ValueError("Metric name became empty after normalization")
    if not _SAFE_COLUMN_RE.match(key):
        raise ValueError(f"Unsafe metric key generated: {key}")

    return key[:63]


def db_connect():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    return psycopg2.connect(**DB_DSN)


# ---------------------------------------------------------------------------
# Machine / SSH helpers
# ---------------------------------------------------------------------------


def get_all_machines_from_db(conn):
    """Return a list of SSH target dicts for every enabled machine."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                alias,
                COALESCE(host(ip_address), hostname) AS connection_host,
                COALESCE(ssh_port, 22) AS ssh_port,
                ssh_user,
                ssh_key_path
            FROM machines
            WHERE COALESCE(monitoring_enabled, true) = true
            ORDER BY updated_at DESC NULLS LAST, alias;
        """)
        rows = cur.fetchall()

    machines = []
    for row in rows:
        alias, host, ssh_port, ssh_user, ssh_key_path = row
        if not host or not ssh_user or not ssh_key_path:
            continue
        machines.append({
            "alias": alias,
            "host": host,
            "port": ssh_port or 22,
            "user": ssh_user,
            "ssh_key": os.path.expanduser(ssh_key_path),
        })
    return machines


def ssh_run(machine, command, timeout=15):
    ssh_cmd = [
        "ssh",
        "-i", machine["ssh_key"],
        "-p", str(machine["port"]),
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{machine['user']}@{machine['host']}",
        command,
    ]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "ssh timeout"
    except Exception as e:
        return False, "", str(e)


def parse_first_float(text):
    match = re.search(r"[-+]?\d*\.?\d+", text or "")
    return float(match.group()) if match else None


def validate_command_on_machines(metric_key, command, machines):
    """
    Try to validate `command` against every machine in the list.
    Return the first successful validation result.
    If all fail, raise with a summary.
    """
    errors = []
    for machine in machines:
        ok, stdout, stderr = ssh_run(machine, command, timeout=15)

        if not ok:
            errors.append(f"{machine['alias']}: SSH returned non-zero or failed ({stderr[:100]})")
            continue

        value = parse_first_float(stdout)

        if value is None:
            errors.append(f"{machine['alias']}: Could not parse numeric value from: {stdout[:100]}")
            continue

        # Successfully validated on this machine
        return {
            "status": "ok",
            "metric_key": metric_key,
            "value": value,
            "raw_output": stdout,
            "machine": machine["alias"],
        }

    # All machines failed
    summary = "; ".join(errors[:5])
    if len(errors) > 5:
        summary += f" (... and {len(errors) - 5} more)"
    raise RuntimeError(
        f"Command validation failed on all {len(machines)} machine(s): {summary}"
    )


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------


def write_metric_script(metric_key, meta):
    """
    Create a small standalone plugin script in new-metrics/<key>.py.

    The script includes both METADATA (the METRIC dict) and a collect()
    function that executes the shell command and returns
    {"value": <number>, "status": "ok", "raw_output": "..."}.
    """
    NEW_METRICS_DIR.mkdir(parents=True, exist_ok=True)
    script_path = NEW_METRICS_DIR / f"{metric_key}.py"

    display_name_json = json.dumps(meta["display_name"])
    unit_json = json.dumps(meta.get("unit", ""))
    # Escape the command for embedding in Python source: wrap in triple-quotes
    # to handle any embedded quotes or special chars safely.
    command_raw = meta["command"]
    # Use repr() for a safe Python string literal representation
    command_repr = repr(command_raw)

    content = f'''#!/usr/bin/env python3
\"\"\"
{meta["display_name"]} - custom metric plugin.

When executed directly, runs the monitoring command and returns the actual
metric value as JSON: {{"value": <number>, "status": "ok", "raw_output": "..."}}.
\"\"\"

import json
import re
import subprocess
import sys

METRIC = {{
    "metric_key": "{metric_key}",
    "display_name": {display_name_json},
    "column_name": "{metric_key}",
    "db_type": "{meta.get("db_type", "DOUBLE PRECISION")}",
    "unit": {unit_json},
    "command": {command_repr},
    "parser": "first_float",
    "enabled": True,
}}


def collect():
    \"\"\"Execute the monitoring command and return the parsed value.\"\"\"
    try:
        proc = subprocess.run(
            METRIC["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {{"value": None, "status": "error", "raw_output": "command timed out"}}
    except Exception as e:
        return {{"value": None, "status": "error", "raw_output": str(e)}}

    raw = (proc.stdout or "").strip()

    if proc.returncode != 0:
        return {{
            "value": None,
            "status": "error",
            "raw_output": (raw or proc.stderr.strip())[:500],
        }}

    match = re.search(r"[-+]?[0-9]*[.]?[0-9]+", raw)
    if match:
        return {{"value": float(match.group()), "status": "ok", "raw_output": raw[:500]}}

    return {{"value": None, "status": "error", "raw_output": "No numeric value found in: " + str(raw[:500])}}


if __name__ == "__main__":
    result = collect()
    print(json.dumps(result))
'''

    # Write to a .pending file first, compile-validate, then atomically rename
    pending_path = script_path.with_suffix(".py.pending")
    pending_path.write_text(content)

    try:
        compile(pending_path.read_text(), str(pending_path), "exec")
    except SyntaxError as e:
        pending_path.unlink(missing_ok=True)
        raise RuntimeError(f"Generated script has a syntax error: {e}")

    pending_path.replace(script_path)
    script_path.chmod(0o755)
    return script_path


# ---------------------------------------------------------------------------
# Registry update
# ---------------------------------------------------------------------------


def _yaml_escape(value):
    """Escape a string for safe embedding in YAML."""
    if value is None:
        return "null"
    s = str(value)
    # If the value contains special YAML chars, wrap in double quotes
    # and escape internal backslashes, quotes, and newlines.
    if any(c in s for c in ('"', "'", ":", "#", "{", "}", "[", "]", ",")):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def update_registry(metric_key, meta, script_path):
    """Append the new metric entry to metric_registry.yaml if not already present."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not REGISTRY_FILE.exists() or REGISTRY_FILE.read_text().strip() == "":
        REGISTRY_FILE.write_text("metrics:\n")

    existing = REGISTRY_FILE.read_text()
    marker = f"  {metric_key}:"
    if marker in existing:
        return

    rel_script = script_path.relative_to(ROOT)
    command_str = _yaml_escape(meta["command"])
    display_name_str = _yaml_escape(meta["display_name"])
    unit_str = _yaml_escape(meta.get("unit", ""))
    db_type_str = _yaml_escape(meta.get("db_type", "DOUBLE PRECISION"))

    block = (
        f"\n  {metric_key}:\n"
        f"    display_name: {display_name_str}\n"
        f"    column_name: \"{metric_key}\"\n"
        f"    db_type: \"{meta.get('db_type', 'DOUBLE PRECISION')}\"\n"
        f"    unit: {unit_str}\n"
        f"    script: \"{rel_script}\"\n"
        f"    command: {command_str}\n"
        f"    parser: \"first_float\"\n"
        f"    timeout_seconds: 15\n"
        f"    enabled: true\n"
    )

    with REGISTRY_FILE.open("a") as f:
        f.write(block)


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------


def ensure_db_column(conn, column_name, db_type):
    """Add a dynamic column to metric_samples if it doesn't exist."""
    if not _SAFE_COLUMN_RE.match(column_name):
        raise ValueError(f"Unsafe DB column name: {column_name}")

    db_type = db_type.upper().strip()
    if db_type not in _ALLOWED_DB_TYPES:
        raise ValueError(f"Unsafe or unsupported DB type: {db_type}")

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER TABLE metric_samples ADD COLUMN IF NOT EXISTS {} {}")
            .format(sql.Identifier(column_name), sql.SQL(db_type))
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Metric resolution
# ---------------------------------------------------------------------------


def resolve_metric_from_cli(metric_name, command, display_name, db_type, unit):
    """
    Resolve metric metadata from CLI args or the hardcoded catalog.

    Priority:
      1. If --command is provided, use it directly (Hermes-driven flow).
      2. If the slugified name is in METRIC_COMMANDS, use that.
      3. If it matches a known alias, use the canonical entry.
      4. Otherwise, error — Hermes must provide --command.
    """
    metric_key = slugify_metric_name(metric_name)

    if command:
        # CLI-driven: build metadata from arguments
        meta = {
            "display_name": display_name or metric_name.strip(),
            "unit": unit or "",
            "db_type": db_type or "DOUBLE PRECISION",
            "command": command,
        }
        return metric_key, meta

    # Fallback: hardcoded catalog
    if metric_key in METRIC_COMMANDS:
        return metric_key, METRIC_COMMANDS[metric_key]

    # Try aliases
    if metric_key in METRIC_ALIASES:
        real_key = METRIC_ALIASES[metric_key]
        return real_key, METRIC_COMMANDS[real_key]

    raise RuntimeError(
        f"Metric '{metric_name}' is not in the predefined catalog and no --command was "
        f"provided. Hermes must generate the shell command and pass it via --command."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Add a new monitoring metric: validate, generate script, register, add DB column."
    )
    parser.add_argument(
        "--metric", required=True,
        help="Human-readable metric name (e.g. 'Established Connections')."
    )
    parser.add_argument(
        "--command", default=None,
        help="Shell command to collect the metric. Required for metrics not in the hardcoded catalog."
    )
    parser.add_argument(
        "--display-name", default=None,
        help="Friendly display name (defaults to --metric value)."
    )
    parser.add_argument(
        "--db-type", default=None,
        help="SQL column type: DOUBLE PRECISION, INTEGER, BIGINT, TEXT, BOOLEAN (default: DOUBLE PRECISION)."
    )
    parser.add_argument(
        "--unit", default=None,
        help="Unit of measurement (e.g. 'count', '%', 'ms')."
    )
    args = parser.parse_args()

    try:
        metric_key, meta = resolve_metric_from_cli(
            args.metric, args.command, args.display_name, args.db_type, args.unit
        )

        conn = db_connect()
        machines = get_all_machines_from_db(conn)

        if not machines:
            raise RuntimeError(
                "No enabled machines found in the `machines` table. "
                "Cannot validate the command. Register a machine first."
            )

        # Try all machines, succeed on first working one
        validation = validate_command_on_machines(metric_key, meta["command"], machines)

        script_path = write_metric_script(metric_key, meta)
        update_registry(metric_key, meta, script_path)
        ensure_db_column(conn, metric_key, meta.get("db_type", "DOUBLE PRECISION"))

        conn.close()

        print(json.dumps({
            "status": "ok",
            "metric_key": metric_key,
            "display_name": meta["display_name"],
            "script": str(script_path),
            "registry": str(REGISTRY_FILE),
            "db_column": metric_key,
            "validated_on": validation["machine"],
            "validated_value": validation["value"],
            "raw_output": validation["raw_output"],
        }, indent=2))

    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
