#!/usr/bin/env python3
"""End-to-end validator for Hermes machine, metric, and app onboarding skills.

The runner invokes the configured skill entrypoints, runs p1_fixed.py after each
skill, verifies the expected database/filesystem/SSH side effects, and writes a
complete JSON + Markdown evidence bundle for the run.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import traceback
import uuid
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import Json, RealDictCursor
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "psycopg2 is required. Install it in the same environment used by p1_fixed.py "
        "(for example: pip install psycopg2-binary)."
    ) from exc


PASS = "passed"
FAIL = "failed"
SKIP = "skipped"
APP_PARSER_SAFE_VALUES = {"running", "stopped", "ok", "error", "restarting"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return value


def redact(value: Any, key: str = "") -> Any:
    sensitive = ("password", "passwd", "secret", "token", "dsn", "private_key")
    if any(part in key.lower() for part in sensitive):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


def recursive_format(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        # Only replace {known_placeholders}, leave literal {curly braces} alone.
        # This prevents shell commands like awk '{print $1}' from crashing.
        def _replacer(m: re.Match) -> str:
            key = m.group(1)
            return str(context[key]) if key in context else m.group(0)
        return re.sub(r"\{(\w+)\}", _replacer, value)
    if isinstance(value, list):
        return [recursive_format(v, context) for v in value]
    if isinstance(value, Mapping):
        return {k: recursive_format(v, context) for k, v in value.items()}
    return value


def nested_get(data: Any, dotted_path: str) -> Any:
    current = data
    if not dotted_path:
        return current
    for part in dotted_path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def nested_set(data: Any, dotted_path: str, value: Any) -> Any:
    if not dotted_path:
        return value
    current = data
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value
    return data


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    duration_ms: int
    stdout_path: str
    stderr_path: str
    stdout_tail: str
    stderr_tail: str


class Runner:
    def __init__(self, config_path: Path, allow_non_test_db: bool = False):
        self.config_path = config_path.resolve()
        self.raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.run_uuid = uuid.uuid4()
        self.run_id = str(self.run_uuid)
        self.run_short = self.run_uuid.hex[:8]
        self.started_at = utc_now()
        self.allow_non_test_db = allow_non_test_db
        self.sequence = 0
        self.steps: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {
            "run_id": self.run_id,
            "run_short": self.run_short,
        }

        db_cfg = self.raw_config.get("database", {})
        dsn_env = db_cfg.get("dsn_env", "LAB_MONITORING_TEST_DSN")
        self.database_dsn = os.environ.get(dsn_env) or db_cfg.get("dsn")
        if not self.database_dsn:
            raise RuntimeError(
                f"Database DSN is missing. Set {dsn_env} or database.dsn in the config."
            )
        self.context["database_dsn"] = self.database_dsn

        execution = self.raw_config.get("execution", {})
        base_results = Path(execution.get("results_directory", "./test-results"))
        if not base_results.is_absolute():
            base_results = (self.config_path.parent / base_results).resolve()
        self.run_dir = base_results / f"run-{self.started_at:%Y%m%dT%H%M%SZ}-{self.run_short}"
        self.logs_dir = self.run_dir / "logs"
        self.fixtures_dir = self.run_dir / "fixtures"
        self.logs_dir.mkdir(parents=True, exist_ok=False)
        self.fixtures_dir.mkdir(parents=True, exist_ok=False)

        self.context.update(
            {
                "run_directory": str(self.run_dir),
                "machine_fixture_path": str(self.fixtures_dir / "machine.json"),
                "metric_fixture_path": str(self.fixtures_dir / "metric.json"),
                "app_fixture_path": str(self.fixtures_dir / "app.json"),
            }
        )
        self.config = recursive_format(self.raw_config, self.context)
        self.execution = self.config.get("execution", {})
        self.continue_after_failure = bool(
            self.execution.get("continue_after_failure", True)
        )
        cleanup_cfg = self.config.get("cleanup", {})
        self.cleanup_enabled = bool(cleanup_cfg.get("enabled", False))
        self.cleanup_before_run = self.cleanup_enabled and bool(
            cleanup_cfg.get("before_run", False)
        )
        self.cleanup_after_run = self.cleanup_enabled and bool(
            cleanup_cfg.get("after_run", False)
        )
        validation_cfg = self.config.get("validation", {})
        self.clock_skew_tolerance_seconds = max(
            0, int(validation_cfg.get("clock_skew_tolerance_seconds", 0))
        )
        self.clock_skew_tolerance = timedelta(
            seconds=self.clock_skew_tolerance_seconds
        )
        self.runtime_state: dict[str, Any] = {}
        self.audit_in_database = bool(db_cfg.get("audit_in_database", True))
        self.conn = psycopg2.connect(self.database_dsn)
        self.conn.autocommit = True
        self.current_database = self.scalar("SELECT current_database()")
        self._enforce_database_safety()
        self._write_fixtures()
        if self.audit_in_database:
            self._install_audit_schema()
            self._insert_run_row()

    def _enforce_database_safety(self) -> None:
        require_test = bool(
            self.raw_config.get("database", {}).get("require_test_database", True)
        )
        looks_test = bool(re.search(r"(?:test|e2e|qa|staging)", self.current_database, re.I))
        if require_test and not looks_test and not self.allow_non_test_db:
            raise RuntimeError(
                f"Refusing to run destructive onboarding tests against database "
                f"'{self.current_database}'. Use a disposable test database, or pass "
                f"--allow-non-test-db deliberately."
            )

    def _write_fixtures(self) -> None:
        fixtures = self.config.get("fixtures", {})
        for name in ("machine", "metric", "app"):
            path = Path(self.context[f"{name}_fixture_path"])
            path.write_text(
                json.dumps(fixtures.get(name, {}), indent=2, sort_keys=True),
                encoding="utf-8",
            )

    def _install_audit_schema(self) -> None:
        schema_path = Path(__file__).with_name("audit_schema.sql")
        with self.conn.cursor() as cur:
            cur.execute(schema_path.read_text(encoding="utf-8"))

    def _insert_run_row(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.enhancement_test_runs
                    (run_id, status, started_at, config_snapshot, report_path)
                VALUES (%s, 'running', %s, %s, %s)
                """,
                (
                    self.run_id,
                    self.started_at,
                    Json(redact(self.config)),
                    str(self.run_dir / "report.json"),
                ),
            )

    def query(self, statement: Any, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(statement, params)
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]

    def scalar(self, statement: Any, params: Sequence[Any] | None = None) -> Any:
        rows = self.query(statement, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def machine_alias(self) -> str:
        return str(self.config["fixtures"]["machine"]["alias"])

    def metric_key(self) -> str:
        return str(self.config["fixtures"]["metric"]["metric_key"])

    def metric_column_name(self) -> str:
        metric = self.config["fixtures"]["metric"]
        return str(metric.get("db_column") or metric.get("column_name"))

    def app_name(self) -> str:
        return str(self.config["fixtures"]["app"]["app_name"])

    def effective_since(self, since: datetime) -> datetime:
        return since - self.clock_skew_tolerance

    def metric_column_rows(self, column_name: str | None = None) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT column_name, data_type, udt_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'metric_samples'
               AND column_name = %s
            """,
            (column_name or self.metric_column_name(),),
        )

    def app_samples_for_name(
        self,
        app_name: str | None = None,
        *,
        since: datetime | None = None,
        server_id: Any = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        clauses = ["app_name = %s"]
        params: list[Any] = [app_name or self.app_name()]
        if server_id is not None:
            clauses.append("server_id = %s")
            params.append(server_id)
        if since is not None:
            clauses.append("ts >= %s")
            params.append(self.effective_since(since))
        params.append(limit)
        return self.query(
            f"""
            SELECT id, server_id, ts, app_name, display_name, status,
                   cpu_pct, rss_memory_mb, process_count, thread_count,
                   listening_sockets
              FROM public.app_metric_samples
             WHERE {' AND '.join(clauses)}
             ORDER BY ts DESC
             LIMIT %s
            """,
            params,
        )

    def load_apps_json_payload(self) -> tuple[Path, Any, Any]:
        app_cfg = self.config.get("apps_json", {})
        path = Path(app_cfg["path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        records = nested_get(data, app_cfg.get("list_path", ""))
        if not isinstance(records, (list, Mapping)):
            raise TypeError("apps_json.list_path must resolve to a list or object")
        return path, data, records

    def app_json_snapshot(self, expected_name: str | None = None) -> dict[str, Any]:
        app_cfg = self.config.get("apps_json", {})
        path = Path(app_cfg["path"])
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        records = nested_get(data, app_cfg.get("list_path", ""))
        if not isinstance(records, (list, Mapping)):
            raise TypeError("apps_json.list_path must resolve to a list or object")
        expected_name = str(expected_name or self.app_name())
        name_key = app_cfg.get("name_key", "app_name")
        command_key = app_cfg.get("command_key", "command")
        match = None
        if isinstance(records, Mapping):
            for key, record in records.items():
                if str(key) == expected_name:
                    match = record
                    break
                if isinstance(record, Mapping) and str(record.get(name_key)) == expected_name:
                    match = record
                    break
            record_count = len(records)
            container_kind = "mapping"
        else:
            for record in records:
                if isinstance(record, Mapping) and str(record.get(name_key)) == expected_name:
                    match = record
                    break
            record_count = len(records)
            container_kind = "list"
        record = dict(match) if isinstance(match, Mapping) else None
        command = record.get(command_key) if record else None
        return {
            "json_path": str(path),
            "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            "expected_app_name": expected_name,
            "exists": bool(record),
            "record_count": record_count,
            "container_kind": container_kind,
            "record": record,
            "command": command,
        }

    def app_json_record(self) -> tuple[dict[str, Any] | None, str | None, Any]:
        snapshot = self.app_json_snapshot()
        path = Path(snapshot["json_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        return snapshot["record"], snapshot["command"], data

    def is_parser_safe_app_output(self, output: str) -> tuple[bool, str]:
        normalized = output.strip()
        if not normalized:
            return False, "empty"
        if normalized.lower() in APP_PARSER_SAFE_VALUES:
            return True, "status_token"
        try:
            json.loads(normalized)
            return True, "json"
        except json.JSONDecodeError:
            pass
        if re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
            return True, "numeric"
        return False, "unrecognized"

    def cleanup_step(self, when: str) -> bool:
        def perform() -> tuple[Any, Any, dict[str, Any]]:
            if when == "before" and not self.cleanup_before_run:
                actual = {
                    "enabled": self.cleanup_enabled,
                    "before_run": self.cleanup_before_run,
                    "after_run": self.cleanup_after_run,
                    "performed": False,
                }
                return actual, True, {"reason": "cleanup.before_run disabled"}
            if when == "after" and not self.cleanup_after_run:
                actual = {
                    "enabled": self.cleanup_enabled,
                    "before_run": self.cleanup_before_run,
                    "after_run": self.cleanup_after_run,
                    "performed": False,
                }
                return actual, True, {"reason": "cleanup.after_run disabled"}

            removed: dict[str, Any] = {
                "machine_rows_deleted": 0,
                "metric_samples_deleted_for_e2e_machines": 0,
                "app_metric_samples_deleted_for_e2e_machines": 0,
                "app_metric_samples_deleted_for_e2e_apps": 0,
                "custom_metric_samples_deleted": 0,
                "metric_registry_deleted": 0,
                "metric_columns_dropped": [],
                "apps_removed_from_json": [],
                "apps_json_hash_before": None,
                "apps_json_hash_after": None,
            }

            machine_rows = self.query(
                "SELECT server_id, alias FROM public.machines WHERE alias LIKE %s",
                ("e2e_%",),
            )
            server_ids = [row["server_id"] for row in machine_rows if row.get("server_id")]
            if server_ids:
                with self.conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM public.metric_samples WHERE server_id = ANY(%s)",
                        (server_ids,),
                    )
                    removed["metric_samples_deleted_for_e2e_machines"] = cur.rowcount
                    cur.execute(
                        "DELETE FROM public.app_metric_samples WHERE server_id = ANY(%s)",
                        (server_ids,),
                    )
                    removed["app_metric_samples_deleted_for_e2e_machines"] = cur.rowcount
            with self.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.app_metric_samples WHERE app_name LIKE %s",
                    ("e2e_%",),
                )
                removed["app_metric_samples_deleted_for_e2e_apps"] = cur.rowcount
                cur.execute(
                    "DELETE FROM public.custom_metric_samples WHERE metric_key LIKE %s",
                    ("e2e_%",),
                )
                removed["custom_metric_samples_deleted"] = cur.rowcount
                cur.execute(
                    "DELETE FROM public.metric_registry WHERE metric_key LIKE %s",
                    ("e2e_%",),
                )
                removed["metric_registry_deleted"] = cur.rowcount
                cur.execute(
                    "DELETE FROM public.machines WHERE alias LIKE %s",
                    ("e2e_%",),
                )
                removed["machine_rows_deleted"] = cur.rowcount

            metric_columns = self.query(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'metric_samples'
                   AND column_name LIKE %s
                 ORDER BY column_name
                """,
                ("e2e_%",),
            )
            if metric_columns:
                with self.conn.cursor() as cur:
                    for row in metric_columns:
                        column_name = row["column_name"]
                        cur.execute(
                            sql.SQL(
                                "ALTER TABLE public.metric_samples DROP COLUMN IF EXISTS {}"
                            ).format(sql.Identifier(column_name))
                        )
                        removed["metric_columns_dropped"].append(column_name)

            app_cfg = self.config.get("apps_json", {})
            path = Path(app_cfg["path"])
            if path.exists():
                before_snapshot = self.app_json_snapshot(expected_name="e2e_")
                removed["apps_json_hash_before"] = before_snapshot["sha256"]
                _, data, records = self.load_apps_json_payload()
                name_key = app_cfg.get("name_key", "app_name")
                if isinstance(records, Mapping):
                    keys_to_remove = []
                    for key, record in records.items():
                        record_name = (
                            str(record.get(name_key))
                            if isinstance(record, Mapping) and record.get(name_key) is not None
                            else str(key)
                        )
                        if str(key).startswith("e2e_") or record_name.startswith("e2e_"):
                            keys_to_remove.append(key)
                    for key in keys_to_remove:
                        record = records.pop(key)
                        record_name = (
                            str(record.get(name_key))
                            if isinstance(record, Mapping) and record.get(name_key) is not None
                            else str(key)
                        )
                        removed["apps_removed_from_json"].append(record_name)
                else:
                    kept_records = []
                    for record in records:
                        record_name = (
                            str(record.get(name_key))
                            if isinstance(record, Mapping) and record.get(name_key) is not None
                            else ""
                        )
                        if record_name.startswith("e2e_"):
                            removed["apps_removed_from_json"].append(record_name)
                            continue
                        kept_records.append(record)
                    data = nested_set(data, app_cfg.get("list_path", ""), kept_records)
                path.write_text(
                    json.dumps(data, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                removed["apps_json_hash_after"] = hashlib.sha256(
                    path.read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest()

            actual = {
                "performed": True,
                "when": when,
                "removed": removed,
            }
            evidence = {
                "database": self.current_database,
                "requires_test_database": self.raw_config.get("database", {}).get(
                    "require_test_database", True
                ),
                "safe_prefixes": {
                    "machine_alias": "e2e_",
                    "metric_key": "e2e_",
                    "metric_column": "e2e_",
                    "app_name": "e2e_",
                },
            }
            return actual, True, evidence

        return self.record_step(
            "preflight" if when == "before" else "cleanup",
            f"Cleanup test-only E2E artifacts {when} run",
            perform,
            expected={
                "enabled": self.cleanup_enabled,
                "before_run": self.cleanup_before_run,
                "after_run": self.cleanup_after_run,
                "safe_prefixes": ["e2e_"],
            },
        )

    def record_step(
        self,
        enhancement: str,
        step_name: str,
        fn: Callable[[], tuple[Any, Any, dict[str, Any]]],
        expected: Any = None,
        skip_reason: str | None = None,
    ) -> bool:
        self.sequence += 1
        started = utc_now()
        step: dict[str, Any] = {
            "sequence_no": self.sequence,
            "enhancement": enhancement,
            "step_name": step_name,
            "status": "running",
            "started_at": iso(started),
            "expected": json_safe(expected),
            "actual": None,
            "evidence": {},
            "error_message": None,
        }
        if self.audit_in_database:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.enhancement_test_steps
                        (run_id, sequence_no, enhancement, step_name, status,
                         started_at, expected)
                    VALUES (%s, %s, %s, %s, 'running', %s, %s)
                    """,
                    (
                        self.run_id,
                        self.sequence,
                        enhancement,
                        step_name,
                        started,
                        Json(json_safe(expected)),
                    ),
                )

        if skip_reason:
            passed = False
            status = SKIP
            actual = {"reason": skip_reason}
            evidence: dict[str, Any] = {}
            error_message = None
        else:
            try:
                actual, assertion, evidence = fn()
                passed = bool(assertion)
                status = PASS if passed else FAIL
                error_message = None if passed else "Assertion returned false"
            except Exception as exc:  # capture evidence instead of aborting the run
                passed = False
                status = FAIL
                actual = {"exception_type": type(exc).__name__}
                evidence = {"traceback": traceback.format_exc()}
                error_message = str(exc)

        finished = utc_now()
        duration_ms = int((finished - started).total_seconds() * 1000)
        step.update(
            {
                "status": status,
                "finished_at": iso(finished),
                "duration_ms": duration_ms,
                "actual": json_safe(actual),
                "evidence": json_safe(evidence),
                "error_message": error_message,
            }
        )
        self.steps.append(step)

        if self.audit_in_database:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.enhancement_test_steps
                       SET status = %s,
                           finished_at = %s,
                           duration_ms = %s,
                           actual = %s,
                           evidence = %s,
                           error_message = %s
                     WHERE run_id = %s AND sequence_no = %s
                    """,
                    (
                        status,
                        finished,
                        duration_ms,
                        Json(json_safe(actual)),
                        Json(json_safe(evidence)),
                        error_message,
                        self.run_id,
                        self.sequence,
                    ),
                )
        self._write_report(final=False)
        return passed

    def run_command(
        self,
        label: str,
        command: Sequence[str],
        timeout_seconds: int,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        argv = [str(token) for token in command]
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.config.get("environment", {}).items()})
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        stdout_path = self.logs_dir / f"{self.sequence:03d}-{label}.stdout.log"
        stderr_path = self.logs_dir / f"{self.sequence:03d}-{label}.stderr.log"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env=env,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\nTimed out after {timeout_seconds}s"
            returncode = 124
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
        return CommandResult(
            argv=argv,
            returncode=returncode,
            duration_ms=duration_ms,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            stdout_tail=stdout[-4000:],
            stderr_tail=stderr[-4000:],
        )

    def command_step(self, enhancement: str, name: str, command_key: str) -> bool:
        command = self.config.get("commands", {}).get(command_key)
        if not command:
            return self.record_step(
                enhancement,
                name,
                lambda: ({}, False, {}),
                expected={"configured_command": command_key},
                skip_reason=f"commands.{command_key} is not configured",
            )
        if any(str(token).startswith("<") for token in command):
            return self.record_step(
                enhancement,
                name,
                lambda: ({}, False, {}),
                expected={"configured_command": command_key},
                skip_reason=f"commands.{command_key} still contains a placeholder",
            )
        timeout = int(self.execution.get("command_timeout_seconds", 180))
        if command_key == "p1":
            timeout = int(self.execution.get("p1_timeout_seconds", 300))

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            result = self.run_command(command_key, command, timeout)
            actual = {
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
            }
            evidence = {
                "argv": result.argv,
                "stdout_path": result.stdout_path,
                "stderr_path": result.stderr_path,
                "stdout_tail": result.stdout_tail,
                "stderr_tail": result.stderr_tail,
            }
            return actual, result.returncode == 0, evidence

        return self.record_step(
            enhancement,
            name,
            perform,
            expected={"returncode": 0, "command_key": command_key},
        )

    def machine_row(self) -> dict[str, Any] | None:
        alias = self.machine_alias()
        rows = self.query(
            "SELECT * FROM public.machines WHERE alias = %s",
            (alias,),
        )
        return rows[0] if rows else None

    def ensure_machine_absent(self) -> bool:
        alias = self.machine_alias()

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            row = self.machine_row()
            actual = {"alias": alias, "exists": bool(row), "row": row}
            return actual, row is None, {"query": "SELECT * FROM public.machines WHERE alias = %s"}

        return self.record_step(
            "machine",
            "Machine alias is absent before onboarding",
            perform,
            expected={"alias": alias, "exists": False},
        )

    def validate_machine_db(self) -> bool:
        expected_machine = self.config["fixtures"]["machine"]

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            row = self.machine_row()
            if not row:
                return None, False, {"query": "machines by alias"}
            compared_fields = [
                "alias",
                "hostname",
                "ip_address",
                "ssh_port",
                "ssh_user",
                "ssh_key_path",
                "monitoring_enabled",
            ]
            mismatches: dict[str, Any] = {}
            for field in compared_fields:
                if field not in expected_machine or expected_machine[field] in ("", None):
                    continue
                expected = str(expected_machine[field])
                actual = str(row.get(field))
                if expected != actual:
                    mismatches[field] = {"expected": expected, "actual": actual}
            return row, not mismatches, {"mismatches": mismatches}

        return self.record_step(
            "machine",
            "Machine exists in machines table with expected values",
            perform,
            expected=expected_machine,
        )

    def validate_tcp(self) -> bool:
        machine = self.machine_row() or self.config["fixtures"]["machine"]
        host = str(machine.get("ip_address"))
        port = int(machine.get("ssh_port") or 22)
        timeout = int(self.execution.get("ssh_timeout_seconds", 10))

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            started = time.monotonic()
            with socket.create_connection((host, port), timeout=timeout):
                pass
            latency_ms = int((time.monotonic() - started) * 1000)
            return {"host": host, "port": port, "latency_ms": latency_ms}, True, {}

        return self.record_step(
            "machine",
            "Machine SSH port is reachable",
            perform,
            expected={"host": host, "port": port, "reachable": True},
        )

    def ssh_base(self, machine: Mapping[str, Any]) -> list[str]:
        args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(self.execution.get('ssh_timeout_seconds', 10))}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(machine.get("ssh_port") or 22),
        ]
        key_path = machine.get("ssh_key_path")
        if key_path:
            args.extend(["-i", str(key_path)])
        args.append(f"{machine['ssh_user']}@{machine['ip_address']}")
        return args

    def validate_ssh(self) -> bool:
        machine = self.machine_row()
        if not machine:
            return self.record_step(
                "machine",
                "Machine accepts non-interactive SSH",
                lambda: ({}, False, {}),
                skip_reason="Machine row was not created",
            )

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            command = self.ssh_base(machine) + ["hostname"]
            result = self.run_command(
                "ssh-hostname",
                command,
                int(self.execution.get("ssh_timeout_seconds", 10)) + 5,
            )
            hostname = result.stdout_tail.strip().splitlines()[-1] if result.stdout_tail.strip() else ""
            expected_hostname = str(machine.get("hostname") or "").strip()
            hostname_matches = not expected_hostname or hostname == expected_hostname
            actual = {
                "returncode": result.returncode,
                "hostname": hostname,
                "hostname_matches_database": hostname_matches,
            }
            evidence = {
                "argv": result.argv,
                "stdout_path": result.stdout_path,
                "stderr_path": result.stderr_path,
                "stderr_tail": result.stderr_tail,
            }
            return actual, result.returncode == 0 and hostname_matches, evidence

        return self.record_step(
            "machine",
            "Machine accepts non-interactive SSH",
            perform,
            expected={"returncode": 0, "hostname_matches_database": True},
        )

    def run_p1_and_capture_start(self, enhancement: str, label: str) -> tuple[bool, datetime]:
        start = utc_now()
        passed = self.command_step(enhancement, label, "p1")
        return passed, start

    def validate_metric_sample_for_machine(self, since: datetime) -> bool:
        machine = self.machine_row()
        if not machine:
            return self.record_step(
                "machine",
                "p1 inserted metric_samples for new machine",
                lambda: ({}, False, {}),
                skip_reason="Machine row was not created",
            )

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            effective_since = self.effective_since(since)
            rows = self.query(
                """
                SELECT id, server_id, ts, source_mode, status,
                       cpu_pct, ram_pct, disk_pct
                  FROM public.metric_samples
                 WHERE server_id = %s AND ts >= %s
                 ORDER BY ts DESC
                 LIMIT 5
                """,
                (machine["server_id"], effective_since),
            )
            return rows, bool(rows), {
                "captured_before_p1": iso(since),
                "effective_since": iso(effective_since),
                "clock_skew_tolerance_seconds": self.clock_skew_tolerance_seconds,
            }

        return self.record_step(
            "machine",
            "p1 inserted metric_samples for new machine",
            perform,
            expected={"server_id": machine["server_id"], "row_count_min": 1},
        )

    def registry_row(self) -> dict[str, Any] | None:
        metric_key = self.metric_key()
        rows = self.query(
            "SELECT * FROM public.metric_registry WHERE metric_key = %s",
            (metric_key,),
        )
        return rows[0] if rows else None

    def ensure_metric_absent(self) -> bool:
        metric_key = self.metric_key()
        column_name = self.metric_column_name()

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            registry = self.registry_row()
            column_rows = self.metric_column_rows(column_name)
            actual = {
                "metric_key": metric_key,
                "registry_exists": bool(registry),
                "registry_row": registry,
                "column_name": column_name,
                "column_exists": bool(column_rows),
                "column_rows": column_rows,
            }
            return actual, not registry and not column_rows, {}

        return self.record_step(
            "metric",
            "Metric key and metric_samples column are absent before onboarding",
            perform,
            expected={
                "metric_key": metric_key,
                "registry_exists": False,
                "column_name": column_name,
                "column_exists": False,
            },
        )

    def validate_metric_registry(self) -> bool:
        expected_metric = self.config["fixtures"]["metric"]

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            row = self.registry_row()
            if not row:
                return None, False, {}
            expected_column = expected_metric.get("db_column") or expected_metric.get("column_name")
            actual_column = row.get("db_column") or row.get("column_name")
            checks = {
                "metric_key": row.get("metric_key") == expected_metric.get("metric_key"),
                "column": actual_column == expected_column,
                "enabled": bool(row.get("enabled")),
                "has_command": bool(row.get("collection_command") or row.get("command_text")),
            }
            return row, all(checks.values()), {"checks": checks}

        return self.record_step(
            "metric",
            "Metric exists in metric_registry",
            perform,
            expected=expected_metric,
        )

    def validate_registry_command(self) -> bool:
        machine = self.machine_row()
        registry = self.registry_row()
        if not machine or not registry:
            return self.record_step(
                "metric",
                "Metric command executes successfully on test machine",
                lambda: ({}, False, {}),
                skip_reason="Machine or metric_registry row is unavailable",
            )
        command_text = registry.get("collection_command") or registry.get("command_text")

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            ssh_command = self.ssh_base(machine) + ["bash -lc " + shlex.quote(command_text)]
            result = self.run_command(
                "metric-command",
                ssh_command,
                int(registry.get("timeout_seconds") or 10) + 5,
            )
            output = result.stdout_tail.strip()
            numeric = False
            try:
                float(output.splitlines()[-1])
                numeric = True
            except (ValueError, IndexError):
                pass
            actual = {
                "returncode": result.returncode,
                "output": output,
                "numeric_output": numeric,
            }
            evidence = {
                "command": command_text,
                "stdout_path": result.stdout_path,
                "stderr_path": result.stderr_path,
                "stderr_tail": result.stderr_tail,
            }
            return actual, result.returncode == 0 and numeric, evidence

        return self.record_step(
            "metric",
            "Metric command executes successfully on test machine",
            perform,
            expected={"returncode": 0, "numeric_output": True},
        )

    def validate_metric_column_and_value(self, since: datetime) -> bool:
        machine = self.machine_row()
        registry = self.registry_row()
        if not machine or not registry:
            return self.record_step(
                "metric",
                "p1 created metric_samples column and inserted a value",
                lambda: ({}, False, {}),
                skip_reason="Machine or metric_registry row is unavailable",
            )
        column_name = registry.get("db_column") or registry.get("column_name")

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            effective_since = self.effective_since(since)
            column_rows = self.metric_column_rows(column_name)
            if not column_rows:
                return {"column_exists": False, "samples": []}, False, {}
            statement = sql.SQL(
                """
                SELECT id, server_id, ts, status, {metric_column} AS metric_value
                  FROM public.metric_samples
                 WHERE server_id = %s AND ts >= %s
                 ORDER BY ts DESC
                 LIMIT 5
                """
            ).format(metric_column=sql.Identifier(column_name))
            samples = self.query(statement, (machine["server_id"], effective_since))
            has_non_null = any(row.get("metric_value") is not None for row in samples)
            actual = {
                "column_exists": True,
                "column": column_rows[0],
                "samples": samples,
                "has_non_null_value": has_non_null,
            }
            return actual, bool(samples) and has_non_null, {
                "captured_before_p1": iso(since),
                "effective_since": iso(effective_since),
                "clock_skew_tolerance_seconds": self.clock_skew_tolerance_seconds,
            }

        return self.record_step(
            "metric",
            "p1 created metric_samples column and inserted a value",
            perform,
            expected={
                "column_name": column_name,
                "sample_count_min": 1,
                "non_null_value": True,
            },
        )

    def ensure_app_absent(self) -> bool:
        app_name = self.app_name()
        app_cfg = self.config.get("apps_json", {})
        path = Path(app_cfg.get("path", ""))
        if not path.exists():
            return self.record_step(
                "app",
                "Application name is absent before onboarding",
                lambda: ({}, False, {}),
                expected={"app_name": app_name, "json_path": str(path)},
                skip_reason=f"Apps JSON does not exist: {path}",
            )

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            snapshot = self.app_json_snapshot(app_name)
            existing_rows = self.app_samples_for_name(app_name, limit=5)
            self.runtime_state["app_json_before_onboarding"] = snapshot
            actual = {
                "app_name": app_name,
                "json_path": snapshot["json_path"],
                "json_sha256": snapshot["sha256"],
                "app_exists_in_json": snapshot["exists"],
                "existing_json_record": snapshot["record"],
                "existing_app_metric_sample_count": len(existing_rows),
                "existing_app_metric_samples": existing_rows,
            }
            evidence = {"json_snapshot": snapshot}
            return actual, not snapshot["exists"] and not existing_rows, evidence

        return self.record_step(
            "app",
            "Application name is absent before onboarding",
            perform,
            expected={
                "app_name": app_name,
                "app_exists_in_json": False,
                "existing_app_metric_sample_count": 0,
            },
        )

    def validate_app_json(self) -> bool:
        app_cfg = self.config.get("apps_json", {})
        path = Path(app_cfg.get("path", ""))
        if not path.exists():
            return self.record_step(
                "app",
                "Application command is stored in monitored apps JSON",
                lambda: ({}, False, {}),
                expected={"json_path": str(path)},
                skip_reason=f"Apps JSON does not exist: {path}",
            )

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            before = self.runtime_state.get("app_json_before_onboarding")
            after = self.app_json_snapshot(self.app_name())
            actual = {
                "before": before,
                "after": after,
                "json_path": str(path),
            }
            changed_during_run = bool(
                before
                and not before.get("exists")
                and after.get("exists")
                and before.get("sha256") != after.get("sha256")
            )
            evidence = {
                "new_record": after.get("record"),
                "command": after.get("command"),
                "json_hash_changed": None if not before else before.get("sha256") != after.get("sha256"),
            }
            return actual, bool(after.get("record") and after.get("command") and changed_during_run), evidence

        return self.record_step(
            "app",
            "Application command is stored in monitored apps JSON",
            perform,
            expected={
                "app_name": self.config["fixtures"]["app"]["app_name"],
                "has_command": True,
                "created_or_changed_during_run": True,
            },
        )

    def validate_app_command(self) -> bool:
        machine = self.machine_row()
        try:
            record, command_text, _ = self.app_json_record()
        except Exception:
            record, command_text = None, None
        if not machine or not record or not command_text:
            return self.record_step(
                "app",
                "Stored application command executes on test machine",
                lambda: ({}, False, {}),
                skip_reason="Machine or application JSON record is unavailable",
            )

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            ssh_command = self.ssh_base(machine) + ["bash -lc " + shlex.quote(command_text)]
            result = self.run_command(
                "app-command",
                ssh_command,
                int(self.execution.get("ssh_timeout_seconds", 10)) + 10,
            )
            output = result.stdout_tail.strip()
            parser_safe, parser_mode = self.is_parser_safe_app_output(output)
            actual = {
                "returncode": result.returncode,
                "output": output,
                "output_non_empty": bool(output),
                "parser_safe_output": parser_safe,
                "parser_safe_mode": parser_mode,
            }
            evidence = {
                "command": command_text,
                "record": record,
                "stdout_path": result.stdout_path,
                "stderr_path": result.stderr_path,
                "stderr_tail": result.stderr_tail,
            }
            return actual, result.returncode == 0 and bool(output) and parser_safe, evidence

        return self.record_step(
            "app",
            "Stored application command executes on test machine",
            perform,
            expected={"returncode": 0, "output_non_empty": True, "parser_safe_output": True},
        )

    def validate_app_samples(self, since: datetime) -> bool:
        machine = self.machine_row()
        app_name = self.config["fixtures"]["app"]["app_name"]
        if not machine:
            return self.record_step(
                "app",
                "p1 inserted app_metric_samples for new application",
                lambda: ({}, False, {}),
                skip_reason="Machine row was not created",
            )

        def perform() -> tuple[Any, Any, dict[str, Any]]:
            rows = self.app_samples_for_name(app_name, since=since, server_id=machine["server_id"])
            return rows, bool(rows), {
                "captured_before_p1": iso(since),
                "effective_since": iso(self.effective_since(since)),
                "clock_skew_tolerance_seconds": self.clock_skew_tolerance_seconds,
            }

        return self.record_step(
            "app",
            "p1 inserted app_metric_samples for new application",
            perform,
            expected={
                "server_id": machine["server_id"],
                "app_name": app_name,
                "row_count_min": 1,
            },
        )

    def preflight(self) -> bool:
        ok = True
        ok &= self.record_step(
            "preflight",
            "Connected to an isolated test database",
            lambda: (
                {"database": self.current_database},
                True,
                {"config_path": str(self.config_path)},
            ),
            expected={"test_database": True},
        )

        def fixture_check() -> tuple[Any, Any, dict[str, Any]]:
            paths = {
                name: self.context[f"{name}_fixture_path"]
                for name in ("machine", "metric", "app")
            }
            valid = all(Path(path).is_file() for path in paths.values())
            return paths, valid, {}

        ok &= self.record_step(
            "preflight",
            "Generated machine, metric, and app test fixtures",
            fixture_check,
            expected={"fixture_count": 3},
        )

        def app_target_alignment() -> tuple[Any, Any, dict[str, Any]]:
            machine_alias = self.machine_alias()
            target_alias = self.config["fixtures"]["app"].get("target_machine_alias")
            actual = {
                "machine_alias": machine_alias,
                "target_machine_alias": target_alias,
            }
            return actual, target_alias == machine_alias, {
                "note": "The app fixture field is kept aligned even though the app skill does not consume it directly."
            }

        ok &= self.record_step(
            "preflight",
            "App fixture target machine alias matches machine fixture alias",
            app_target_alignment,
            expected={"target_machine_alias_matches_machine_alias": True},
        )
        return ok

    def run(self) -> int:
        preflight_ok = self.preflight()
        self.cleanup_step("before")

        self.ensure_machine_absent()
        machine_command_ok = self.command_step(
            "machine", "Run machine onboarding skill", "add_machine"
        )
        machine_db_ok = self.validate_machine_db()
        self.validate_tcp()
        self.validate_ssh()
        p1_machine_ok, p1_machine_start = self.run_p1_and_capture_start(
            "machine", "Run p1_fixed.py after machine onboarding"
        )
        self.validate_metric_sample_for_machine(p1_machine_start)

        self.ensure_metric_absent()
        metric_command_ok = self.command_step(
            "metric", "Run metric onboarding skill", "add_metric"
        )
        self.validate_metric_registry()
        self.validate_registry_command()
        p1_metric_ok, p1_metric_start = self.run_p1_and_capture_start(
            "metric", "Run p1_fixed.py after metric onboarding"
        )
        self.validate_metric_column_and_value(p1_metric_start)

        self.ensure_app_absent()
        app_command_ok = self.command_step(
            "app", "Run application onboarding skill", "add_app"
        )
        self.validate_app_json()
        self.validate_app_command()
        p1_app_ok, p1_app_start = self.run_p1_and_capture_start(
            "app", "Run p1_fixed.py after application onboarding"
        )
        self.validate_app_samples(p1_app_start)
        if self.cleanup_after_run:
            self.cleanup_step("after")

        return self.finalize()

    def finalize(self) -> int:
        passed = sum(step["status"] == PASS for step in self.steps)
        failed = sum(step["status"] == FAIL for step in self.steps)
        skipped = sum(step["status"] == SKIP for step in self.steps)
        if failed == 0 and skipped == 0:
            status = PASS
        else:
            status = FAIL
        finished = utc_now()
        self.final_status = status
        self.finished_at = finished
        self._write_report(final=True)
        if self.audit_in_database:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.enhancement_test_runs
                       SET status = %s,
                           finished_at = %s,
                           passed_steps = %s,
                           failed_steps = %s,
                           skipped_steps = %s
                     WHERE run_id = %s
                    """,
                    (status, finished, passed, failed, skipped, self.run_id),
                )
        self.conn.close()
        print(f"Run ID:   {self.run_id}")
        print(f"Status:   {status}")
        print(f"Report:   {self.run_dir / 'summary.md'}")
        print(f"Evidence: {self.run_dir / 'report.json'}")
        return 0 if status == PASS else 1

    def _write_report(self, final: bool) -> None:
        report = {
            "run_id": self.run_id,
            "status": getattr(self, "final_status", "running"),
            "started_at": iso(self.started_at),
            "finished_at": iso(getattr(self, "finished_at", None)) if final else None,
            "database": self.current_database,
            "config_path": str(self.config_path),
            "run_directory": str(self.run_dir),
            "cleanup": redact(self.config.get("cleanup", {})),
            "validation": redact(self.config.get("validation", {})),
            "fixtures": redact(self.config.get("fixtures", {})),
            "steps": self.steps,
        }
        (self.run_dir / "report.json").write_text(
            json.dumps(json_safe(report), indent=2, sort_keys=False),
            encoding="utf-8",
        )
        lines = [
            f"# Enhancement E2E Test — `{self.run_id}`",
            "",
            f"- Status: **{report['status'].upper()}**",
            f"- Started: `{report['started_at']}`",
            f"- Database: `{self.current_database}`",
            f"- Evidence directory: `{self.run_dir}`",
            "",
            "## Steps",
            "",
            "| # | Enhancement | Step | Status | Duration |",
            "|---:|---|---|---|---:|",
        ]
        for step in self.steps:
            lines.append(
                f"| {step['sequence_no']} | {step['enhancement']} | "
                f"{step['step_name']} | **{step['status'].upper()}** | "
                f"{step.get('duration_ms', 0)} ms |"
            )
        failures = [s for s in self.steps if s["status"] == FAIL]
        if failures:
            lines.extend(["", "## Failures", ""])
            for step in failures:
                lines.append(
                    f"- **#{step['sequence_no']} {step['step_name']}** — "
                    f"{step.get('error_message') or 'assertion failed'}"
                )
        lines.extend(
            [
                "",
                "Full expected/actual values, SQL evidence, command output paths, and tracebacks are in `report.json`.",
            ]
        )
        (self.run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--allow-non-test-db",
        action="store_true",
        help="Deliberately allow the runner to modify a database whose name does not look like a test database.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner: Runner | None = None
    try:
        runner = Runner(args.config, allow_non_test_db=args.allow_non_test_db)
        return runner.run()
    except Exception as exc:
        print(f"Fatal test-runner error: {exc}", file=sys.stderr)
        traceback.print_exc()
        if runner is not None:
            try:
                runner.final_status = FAIL
                runner.finished_at = utc_now()
                runner._write_report(final=True)
            except Exception:
                pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
