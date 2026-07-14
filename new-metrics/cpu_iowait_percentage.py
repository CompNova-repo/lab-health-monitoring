#!/usr/bin/env python3
"""
CPU I/O Wait Percentage - custom metric plugin.

When executed directly, runs the monitoring command and returns the actual
metric value as JSON: {"value": <number>, "status": "ok", "raw_output": "..."}.
"""

import json
import re
import subprocess
import sys

METRIC = {
    "metric_key": "cpu_iowait_percentage",
    "display_name": "CPU I/O Wait Percentage",
    "column_name": "cpu_iowait_percentage",
    "db_type": "DOUBLE PRECISION",
    "unit": "%",
    "command": "top -bn1 | awk '/Cpu\\(s\\)/ {gsub(/,/, \"\"); for(i=1;i<=NF;i++) if ($i ~ /wa/) print $(i-1)}'",
    "parser": "first_float",
    "enabled": True,
}


def collect():
    """Execute the monitoring command and return the parsed value."""
    try:
        proc = subprocess.run(
            METRIC["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"value": None, "status": "error", "raw_output": "command timed out"}
    except Exception as e:
        return {"value": None, "status": "error", "raw_output": str(e)}

    raw = (proc.stdout or "").strip()

    if proc.returncode != 0:
        return {
            "value": None,
            "status": "error",
            "raw_output": (raw or proc.stderr.strip())[:500],
        }

    match = re.search(r"[-+]?\d*\.?\d+", raw)
    if match:
        return {"value": float(match.group()), "status": "ok", "raw_output": raw[:500]}

    return {"value": None, "status": "error", "raw_output": f"No numeric value found in: {raw[:500]}"}


if __name__ == "__main__":
    result = collect()
    print(json.dumps(result))
