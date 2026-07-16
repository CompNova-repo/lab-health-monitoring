# E2E Test Harness — Changes Documentation

All modifications made to get the E2E test passing end-to-end (18/18 steps).

---

## 1. `02_ingest_machine.py` — Machine database ingest script

**Location:** `/home/vagrant/.hermes/skills/add-new-machine/02_ingest_machine.py`

### Change A: Environment-aware database configuration

**Before:** Hardcoded database user and name.
```python
DB_USER = "release_user"
DB_NAME = "lab_monitoring_db"
```

**After:** Reads from `P1_DB_*` environment variables (with the same defaults), plus added `DB_HOST` and `DB_PORT`.
```python
import os

DB_USER = os.environ.get("P1_DB_USER", "release_user")
DB_NAME = os.environ.get("P1_DB_NAME", "lab_monitoring_db")
DB_HOST = os.environ.get("P1_DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("P1_DB_PORT", "5432")
```

This is consistent with how every other skill (`main.py`, `headless_add_app.py`, `headless_onboard.py`, `metric_onboard.py`) reads database configuration. Without this change, the machine ingest always wrote to `lab_monitoring_db` regardless of the test database configured by the runner.

### Change B: PGPASSWORD bridging for psql

**Before:** `psql` subprocess relied on environment inheritance for `PGPASSWORD`.
```python
def run_psql(sql: str) -> subprocess.CompletedProcess:
    cmd = [
        "psql",
        "-U", DB_USER,
        "-d", DB_NAME,
        ...
    ]
    return subprocess.run(cmd, ...)
```

**After:** Explicitly bridges `P1_DB_PASSWORD` to `PGPASSWORD` before spawning `psql`.
```python
def run_psql(sql: str) -> subprocess.CompletedProcess:
    cmd = [
        "psql",
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        ...
    ]
    env = os.environ.copy()
    if "P1_DB_PASSWORD" in env and "PGPASSWORD" not in env:
        env["PGPASSWORD"] = env["P1_DB_PASSWORD"]
    return subprocess.run(cmd, ..., env=env)
```

Also added `-h` and `-p` flags to use the host/port from environment variables. The `psql` CLI tool uses `PGPASSWORD` (not `P1_DB_PASSWORD`), so without this bridge, it would prompt for a password interactively and hang.

---

## 2. `e2e_runner.py` — E2E test runner (starter package)

**Location:** `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/e2e_runner.py`

### Change A: Safe `recursive_format` — handle literal curly braces

**Before:** Used `str.format_map()` which interprets all `{...}` as format placeholders.
```python
def recursive_format(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(context)
```

**After:** Uses regex to only replace known `{placeholders}` from the context dict.
```python
def recursive_format(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        def _replacer(m: re.Match) -> str:
            key = m.group(1)
            return str(context[key]) if key in context else m.group(0)
        return re.sub(r"\{(\w+)\}", _replacer, value)
```

**Why:** The metric command `awk '{print $1}' /proc/loadavg` contains `{print $1}` which `format_map` interpreted as a placeholder key `print $1`. This caused a `KeyError` before any test steps ran. The regex `\{(\w+)\}` matches `{run_short}`, `{machine_fixture_path}`, etc. but NOT `{print $1}` (because `$` and spaces aren't word characters).

### Change B: Decimal JSON serialization support

**Before:** `json_safe` didn't handle PostgreSQL `NUMERIC` type values.
```python
def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    ...
```

**After:** Added explicit `Decimal` handling.
```python
from decimal import Decimal

def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    ...
```

**Why:** The `machines` table uses `NUMERIC` columns (e.g., `ram_gb`, `disk_total_gb`). When the runner read a machine row and tried to serialize it as JSON (for the audit table and report), PostgreSQL returned `Decimal` objects which Python's `json.dumps` can't serialize. This crashed during `validate_machine_db` with `TypeError: Object of type Decimal is not JSON serializable`.

---

## 3. `app_commands.json` symlink — Cross-skill file path alignment

**Created:** `/home/vagrant/.hermes/skills/new-metric-addition/app_commands.json`

```
new-metric-addition/app_commands.json → ../../../add-new-apps/app_commands.json
```

**Why:** The `add-new-apps` skill's `headless_add_app.py` writes app command registrations to:
```
/home/vagrant/.hermes/skills/add-new-apps/app_commands.json
```

But the monitoring script (`new-metric-addition/p1_fixed.py`) reads from:
```
/home/vagrant/.hermes/skills/new-metric-addition/app_commands.json
```

These are different paths. So when the E2E test adds an application via `headless_add_app.py`, `p1_fixed.py` can't find it and therefore never collects app metrics (no rows in `app_metric_samples`). The symlink makes both locations point to the same file.

---

## 4. `interactive_setup.py` — Interactive E2E configuration script (new file)

**Location:** `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/interactive_setup.py`

A new interactive setup script that walks the user through collecting every needed detail and generates `test_config.json`. It:

- Asks for database credentials and creates the test DB
- Collects machine details (alias, IP, SSH user, SSH key, port)
- Collects metric details (command, name, DB type)
- Collects app details (app name, service name, process pattern)
- Generates `test_config.json` with correct paths and CLI arguments for the actual skill entrypoints (not `--spec` placeholders)
- Optionally runs the E2E test immediately

---

## 5. `agent_prompt.md` — Updated documentation

**Location:** `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/agent_prompt.md`

Corrected throughout to match the actual project structure:

- **Skill directory names**: "machine-addition" → `add-new-machine`, "metric-addition" → `new-metric-addition`, "application-addition" → `add-new-apps`
- **Entrypoints**: Documented actual CLI arguments (no `--spec` flags)
- **Second metric skill**: Added documentation for `metric-onboarding` skill which uses `metric_onboard.py apply --spec <path>`
- **apps_json.list_path**: Corrected from `"applications"` to `"apps"` (matching actual `app_commands.json` structure)
- **Registry column names**: Noted that the table uses `column_name` (not `db_column`) and `command_text` (not `collection_command`)
- **p1_fixed.py path**: Corrected to `/home/vagrant/.hermes/skills/new-metric-addition/p1_fixed.py`
- **DB env vars**: Added note that this p1_fixed.py uses `require_env()` (must be set, no defaults)

---

## Summary of all files changed

| File | Type | Location |
|------|------|----------|
| `02_ingest_machine.py` | Modified skill | `/home/vagrant/.hermes/skills/add-new-machine/02_ingest_machine.py` |
| `e2e_runner.py` | Modified harness | `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/e2e_runner.py` |
| `app_commands.json` | Symlink created | `/home/vagrant/.hermes/skills/new-metric-addition/app_commands.json` → `../add-new-apps/app_commands.json` |
| `interactive_setup.py` | New file | `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/interactive_setup.py` |
| `agent_prompt.md` | Updated | `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/agent_prompt.md` |
