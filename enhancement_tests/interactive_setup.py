#!/usr/bin/env python3
"""Interactive setup for the Hermes enhancement E2E test harness.

Walks you through collecting every detail needed for test_config.json,
generates the file, and optionally launches the test runner.

Usage:
    python interactive_setup.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SKILLS_DIR = Path(os.path.expanduser("~/.hermes/skills"))
E2E_DIR = SKILLS_DIR / "hermes-enhancement-e2e"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    """Ask for a value, optionally with a default shown in brackets."""
    if default is not None:
        msg = f"  {label} [{default}]: "
    else:
        msg = f"  {label}: "
    try:
        val = input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not val and default is not None:
        return default
    return val


def confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {question} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def heading(text: str):
    print(f"\n{BOLD}{CYAN}{'=' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{CYAN}{'=' * 60}{RESET}\n")


def success(text: str):
    print(f"  {GREEN}✓ {text}{RESET}")


def warn(text: str):
    print(f"  {YELLOW}⚠ {text}{RESET}")


def error(text: str):
    print(f"  {RED}✗ {text}{RESET}")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def step_database() -> dict[str, Any]:
    heading("Step 1 — Database")

    print("  The E2E test needs a disposable PostgreSQL database.")
    print("  The runner will refuse to run against a production database.\n")

    # Detect current env or .env defaults
    default_host = os.environ.get("P1_DB_HOST", "127.0.0.1")
    default_port = os.environ.get("P1_DB_PORT", "5432")
    default_user = os.environ.get("P1_DB_USER", "release_user")
    default_password = os.environ.get("P1_DB_PASSWORD", "release_password")

    # Try to suggest a unique test DB name
    default_db = os.environ.get("P1_DB_NAME", "lab_monitoring_db")
    if "test" not in default_db and "e2e" not in default_db:
        default_db = "e2e_enhancement_test"

    host = prompt("Host", default_host)
    port = prompt("Port", default_port)
    user = prompt("User", default_user)
    password = prompt("Password", default_password, secret=True)
    dbname = prompt("Database name", default_db)

    create = confirm(f"Create database '{dbname}' if it doesn't exist?", True)
    if create:
        try:
            result = subprocess.run(
                ["psql", "-h", host, "-p", port, "-U", user, "-c", f"CREATE DATABASE {dbname}"],
                capture_output=True, text=True, timeout=10,
                env={"PGPASSWORD": password} | os.environ,
            )
            if result.returncode == 0:
                success(f"Database '{dbname}' is ready.")
            elif "already exists" in (result.stderr or ""):
                success(f"Database '{dbname}' already exists — using it.")
            else:
                warn(f"Could not create database: {result.stderr.strip()}")
                warn("Make sure the database exists before running the test.")
        except FileNotFoundError:
            warn("psql not found — make sure the database exists manually.")
        except Exception as e:
            warn(f"Could not create database: {e}")

    env_section = {
        "P1_DB_HOST": host,
        "P1_DB_PORT": port,
        "P1_DB_NAME": dbname,
        "P1_DB_USER": user,
        "P1_DB_PASSWORD": password,
    }

    dsn = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    success(f"DSN: postgresql://{user}:<redacted>@{host}:{port}/{dbname}")

    return {
        "dsn_env": "LAB_MONITORING_TEST_DSN",
        "dsn": dsn,
        "require_test_database": True,
        "audit_in_database": True,
        "env": env_section,
        "current_database": dbname,
    }


def step_machine() -> dict[str, Any]:
    heading("Step 2 — Test Machine")

    print("  The E2E test needs a Linux machine reachable via SSH.")
    print("  The machine-entrypoint will validate SSH and register it in the DB.\n")

    alias = prompt("Machine alias", "e2e_machine_{run_short}")
    hostname = prompt("Expected hostname (leave empty if unknown)", "")
    ip = prompt("IP address or hostname", "")
    if not ip:
        error("IP address is required.")
        ip = prompt("IP address", "")

    ssh_user = prompt("SSH username", "patchuser")
    default_key = os.path.expanduser("~/.ssh/id_ed25519")
    if not Path(default_key).exists():
        default_key = os.path.expanduser("~/.ssh/id_rsa")
    ssh_key = prompt("SSH private key path", default_key)
    ssh_port = prompt("SSH port", "22")
    monitoring = prompt("Enable monitoring?", "yes")

    # Build the machine fixture with real values that will be embedded in the command
    fixture = {
        "alias": alias,
        "hostname": hostname,
        "ip_address": ip,
        "ssh_port": int(ssh_port),
        "ssh_user": ssh_user,
        "ssh_key_path": str(Path(os.path.expanduser(ssh_key)).resolve()),
        "monitoring_enabled": monitoring.lower() in ("yes", "y", "true", "1"),
    }

    # The headless_onboard.py takes inline CLI args (not a fixture file)
    command = [
        sys.executable or "python3",
        str(SKILLS_DIR / "add-new-machine" / "headless_onboard.py"),
        "--alias", alias,
        "--ip", ip,
        "--user", ssh_user,
        "--key", str(Path(os.path.expanduser(ssh_key)).resolve()),
        "--tags", "e2e",
        "--monitor", monitoring,
    ]

    return {
        "fixture": fixture,
        "command": command,
    }


def step_metric() -> dict[str, Any]:
    heading("Step 3 — Test Metric")

    print("  The E2E test will register a harmless numeric metric.")
    print("  Default: load average (1-minute) via /proc/loadavg")
    print("  This requires no special privileges and completes quickly.\n")

    default_cmd = "awk '{print $1}' /proc/loadavg"
    default_key = "e2e_load_1m_{run_short}"
    default_display = "E2E Load Average 1m"

    metric_key = prompt("Metric key (snake_case)", default_key)
    command = prompt("Shell command to collect the value", default_cmd)
    display_name = prompt("Display name", default_display)
    db_type = prompt("DB column type", "DOUBLE PRECISION")
    unit = prompt("Unit", "load")

    fixture = {
        "metric_key": metric_key,
        "display_name": display_name,
        "column_name": metric_key,
        "db_column": metric_key,
        "db_type": db_type.lower(),
        "unit": unit,
        "command_strategy": "linux_shell_first_float",
        "command_text": command,
        "collection_command": command,
        "parser": "first_float",
        "parser_name": "first_float",
        "timeout_seconds": 10,
        "enabled": True,
    }

    command_argv = [
        sys.executable or "python3",
        str(SKILLS_DIR / "new-metric-addition" / "main.py"),
        "--metric", metric_key,
        "--command", command,
        "--display-name", display_name,
        "--db-type", db_type,
        "--unit", unit,
    ]

    return {
        "fixture": fixture,
        "command": command_argv,
    }


def step_app(machine_alias: str) -> dict[str, Any]:
    heading("Step 4 — Application to Monitor")

    print("  The E2E test will register a harmless application.")
    print("  Default: sshd (present on almost every Linux server).\n")

    default_name = "e2e_sshd_{run_short}"
    default_service = "ssh"
    default_pattern = "sshd"
    default_display = "E2E SSHD {run_short}"

    app_name = prompt("Application name", default_name)
    service_name = prompt("systemd service name", default_service)
    process_pattern = prompt("Process pattern (regex)", default_pattern)
    display_name = prompt("Display name", default_display)

    fixture = {
        "app_name": app_name,
        "display_name": display_name,
        "command": f"pgrep -x {process_pattern} >/dev/null && echo running || echo stopped",
        "target_machine_alias": machine_alias,
    }

    # headless_add_app.py takes inline CLI args
    add_command = [
        sys.executable or "python3",
        str(SKILLS_DIR / "add-new-apps" / "headless_add_app.py"),
        "--app-name", app_name,
        "--service-name", service_name,
        "--process-pattern", process_pattern,
        "--display-name", display_name,
        "--overwrite",
        "--local",
    ]

    return {
        "fixture": fixture,
        "add_command": add_command,
    }


def generate_config(
    db: dict[str, Any],
    machine: dict[str, Any],
    metric: dict[str, Any],
    app: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete test_config.json dict."""

    return {
        "database": {
            "dsn_env": db["dsn_env"],
            "dsn": db["dsn"],
            "require_test_database": db["require_test_database"],
            "audit_in_database": db["audit_in_database"],
        },
        "execution": {
            "continue_after_failure": True,
            "command_timeout_seconds": 180,
            "p1_timeout_seconds": 300,
            "ssh_timeout_seconds": 10,
            "results_directory": "./test-results",
        },
        "cleanup": {
            "enabled": True,
            "before_run": True,
            "after_run": False,
        },
        "commands": {
            "add_machine": machine["command"],
            "add_metric": metric["command"],
            "add_app": app["add_command"],
            "p1": [
                sys.executable or "python3",
                str(SKILLS_DIR / "new-metric-addition" / "p1_fixed.py"),
                "run",
                "--mode",
                "standard",
            ],
        },
        "environment": {
            "LAB_MONITORING_DSN": db["dsn"],
            "P1_DB_HOST": db["env"]["P1_DB_HOST"],
            "P1_DB_PORT": db["env"]["P1_DB_PORT"],
            "P1_DB_NAME": db["env"]["P1_DB_NAME"],
            "P1_DB_USER": db["env"]["P1_DB_USER"],
            "P1_DB_PASSWORD": db["env"]["P1_DB_PASSWORD"],
        },
        "apps_json": {
            "path": str(SKILLS_DIR / "add-new-apps" / "app_commands.json"),
            "list_path": "apps",
            "name_key": "app_name",
            "command_key": "command",
        },
        "fixtures": {
            "machine": machine["fixture"],
            "metric": metric["fixture"],
            "app": app["fixture"],
        },
    }


def print_summary(config: dict[str, Any]):
    heading("Configuration Summary")
    print(f"  Database:  {config['database']['dsn']}")
    print(f"  Machine:   {config['fixtures']['machine']['alias']}")
    print(f"             {config['fixtures']['machine']['ip_address']}")
    print(f"  Metric:    {config['fixtures']['metric']['metric_key']}")
    print(f"  App:       {config['fixtures']['app']['app_name']}")
    print()
    print(f"  Config will be written to: {BOLD}test_config.json{RESET}")
    print()


def write_config(config: dict[str, Any]) -> Path:
    path = E2E_DIR / "test_config.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print(f"\n  {BOLD}{GREEN}Hermes Enhancement E2E — Interactive Setup{RESET}")
    print(f"  {BOLD}{'─' * 50}{RESET}")
    print()
    print(f"  This script will help you configure and optionally run the")
    print(f"  end-to-end test for machine / metric / app onboarding.")
    print(f"  Skills directory: {SKILLS_DIR}")
    print()

    if not E2E_DIR.exists():
        error(f"E2E package not found at {E2E_DIR}")
        error("Make sure the hermes-enhancement-e2e package is installed.")
        sys.exit(1)

    db = step_database()
    machine = step_machine()
    metric = step_metric()
    app = step_app(machine["fixture"]["alias"])

    config = generate_config(db, machine, metric, app)
    print_summary(config)

    if confirm("Write test_config.json and proceed?", True):
        path = write_config(config)
        success(f"Config written to {path}")
    else:
        print("  Config not written.")
        return

    # Show known limitations
    heading("Known Limitations")
    print(f"  {YELLOW}1. apps_json.command_key mismatch{RESET}")
    print("     headless_add_app.py stores commands as a dict keyed by metric")
    print("     name (cpu_pct, rss_memory_mb, etc.), not as a single 'command'")
    print("     string. The runner's validate_app_command step expects a flat")
    print("     'command' field and may fail. Adaptation of either the runner")
    print("     or skill may be needed.")
    print()
    print(f"  {YELLOW}2. p1_fixed.py app_commands.json path{RESET}")
    print("     headless_add_app.py writes to")
    print(f"     {SKILLS_DIR / 'add-new-apps' / 'app_commands.json'}")
    print("     but p1_fixed.py (new-metric-addition version) reads from")
    print(f"     {SKILLS_DIR / 'new-metric-addition' / 'app_commands.json'}")
    print("     These are different paths. Consider symlinking or adjusting.")
    print()

    if confirm(f"Run the E2E test now? (python e2e_runner.py --config test_config.json)", True):
        heading("Running E2E Test")
        cmd = [sys.executable, str(E2E_DIR / "e2e_runner.py"), "--config", str(path)]
        print(f"  $ {' '.join(cmd)}\n")
        result = subprocess.run(cmd, cwd=E2E_DIR)
        if result.returncode == 0:
            success("All tests passed!")
        else:
            warn(f"Tests finished with exit code {result.returncode}")
            warn("Check the report in test-results/ for details.")
    else:
        print()
        print(f"  To run later:")
        print(f"    cd {E2E_DIR}")
        print(f"    python e2e_runner.py --config test_config.json")


if __name__ == "__main__":
    main()
