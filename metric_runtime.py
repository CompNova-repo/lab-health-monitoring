#!/usr/bin/env python3
"""Shared registry loading, metric execution, and PostgreSQL schema helpers."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

SKILL_DIR = Path(__file__).resolve().parent
REGISTRY_FILE = Path(os.environ.get("P1_METRIC_REGISTRY_FILE", SKILL_DIR / "metric_registry.yaml"))
SAFE_COLUMN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ALLOWED_DB_TYPES = {"DOUBLE PRECISION", "BIGINT", "INTEGER", "NUMERIC"}


def is_safe_metric_column(name):
    return bool(SAFE_COLUMN.fullmatch(name or ""))


def load_metric_registry(path=None):
    registry_path = Path(path or REGISTRY_FILE)
    if not registry_path.exists():
        return {}
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    metrics = raw.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("metric registry 'metrics' value must be a mapping")
    result = {}
    for key, cfg in metrics.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        column = cfg.get("column_name", key)
        if not is_safe_metric_column(key) or not is_safe_metric_column(column):
            raise ValueError(f"unsafe metric key or column: {key!r}/{column!r}")
        result[key] = cfg
    return result


def run_metric_script(metric_key, cfg, registry_file=None):
    registry_path = Path(registry_file or REGISTRY_FILE)
    script = cfg.get("script")
    base = registry_path.resolve().parent
    script_path = (base / script).resolve() if script else None
    result = {"metric_key": metric_key, "column_name": cfg.get("column_name", metric_key),
              "value": None, "status": "error", "raw_output": ""}
    if not script_path or base not in script_path.parents or not script_path.is_file():
        result["raw_output"] = "Metric script is missing or outside the skill directory"
        return result
    try:
        proc = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True,
                              timeout=int(cfg.get("timeout_seconds", 15)))
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["raw_output"] = str(exc)[:500]
        return result
    raw = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        result["raw_output"] = raw[:500]
        return result
    try:
        data = json.loads(proc.stdout)
        value = data.get("value")
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError("value is not numeric")
        result.update(value=value, status=data.get("status", "ok"),
                      raw_output=str(data.get("raw_output", raw))[:500])
    except (ValueError, TypeError, AttributeError) as exc:
        result["raw_output"] = f"Invalid JSON output: {raw[:400]} ({exc})"
    return result


def collect_custom_metrics(metric_registry, registry_file=None):
    return [run_metric_script(key, cfg, registry_file) for key, cfg in metric_registry.items()]


def db_ensure_custom_metric_columns(conn, metric_registry):
    from psycopg2 import sql
    with conn.cursor() as cur:
        for key, cfg in metric_registry.items():
            column = cfg.get("column_name", key)
            db_type = str(cfg.get("db_type", "DOUBLE PRECISION")).upper()
            if not is_safe_metric_column(column) or db_type not in ALLOWED_DB_TYPES:
                raise ValueError(f"unsafe metric column or database type: {column!r}/{db_type!r}")
            cur.execute(sql.SQL("ALTER TABLE metric_samples ADD COLUMN IF NOT EXISTS {} {}").format(
                sql.Identifier(column), sql.SQL(db_type)))
