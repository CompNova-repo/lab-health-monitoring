#!/usr/bin/env python3
"""
TIME_WAIT Connections - custom metric plugin.

When executed directly, runs the monitoring command and returns the actual
metric value as JSON: {"value": <number>, "status": "ok", "raw_output": "..."}.
"""

import json
import re
import subprocess
import sys

METRIC = {
    "metric_key": "time_wait_connections",
    "display_name": "TIME_WAIT Connections",
    "column_name": "time_wait_connections",
    "db_type": "INTEGER",
    "unit": "count",
    "command": 'ss -t state time-wait | wc -l',
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

    match = re.search(r"[-+]?[0-9]*[.]?[0-9]+", raw)
    if match:
        return {"value": float(match.group()), "status": "ok", "raw_output": raw[:500]}

    return {"value": None, "status": "error", "raw_output": "No numeric value found in: " + str(raw[:500])}


if __name__ == "__main__":
    result = collect()
    print(json.dumps(result))
