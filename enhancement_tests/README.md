# Hermes Enhancement End-to-End Test Harness

This harness tests the three completed enhancements as one dependent workflow:

1. Add a reachable machine.
2. Run `p1_fixed.py` and verify a new `metric_samples` row for that machine.
3. Add a metric and verify its `metric_registry` row.
4. Execute the registered metric command over SSH.
5. Run `p1_fixed.py` and verify the new `metric_samples` column and a non-null value.
6. Add an application and verify its monitoring command in JSON.
7. Execute the stored application command over SSH.
8. Run `p1_fixed.py` and verify a new `app_metric_samples` row.

Every assertion is recorded in both:

- `test-results/run-.../report.json` and `summary.md`
- `enhancement_test_runs` and `enhancement_test_steps` in the test database

Command stdout and stderr are stored as separate log files under the run directory.

## Why a separate test database is required

A metric onboarding test creates a real column in `metric_samples`. Running repeated
unique tests in the production database would permanently pollute its schema. The
runner therefore refuses databases whose name does not contain `test`, `e2e`, `qa`,
or `staging`, unless `--allow-non-test-db` is deliberately supplied.

The cleanest setup is to clone the schema into a disposable database and make the
three skills plus `p1_fixed.py` read their DSN from the same environment variable.

Example:

```bash
createdb lab_monitoring_e2e
psql lab_monitoring_e2e < /path/to/db_schema.sql
export LAB_MONITORING_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:5432/lab_monitoring_e2e'
```

## Configure

Copy the example:

```bash
cp test_config.example.json test_config.json
```

Replace the four command placeholders with the actual deterministic entrypoints for:

- machine onboarding
- metric onboarding
- application onboarding
- `p1_fixed.py`

Commands are arrays, not shell strings, so paths and arguments are passed safely.
Each command may use these placeholders:

- `{machine_fixture_path}`
- `{metric_fixture_path}`
- `{app_fixture_path}`
- `{run_id}`
- `{run_short}`
- `{database_dsn}`

Set the actual apps JSON layout through:

```json
"apps_json": {
  "path": "/absolute/path/apps.json",
  "list_path": "applications",
  "name_key": "app_name",
  "command_key": "command"
}
```

For a top-level JSON array, set `list_path` to an empty string.

## Run

```bash
python3 e2e_runner.py --config test_config.json
```

A successful run exits `0`; assertion failures exit `1`; fatal configuration or
runner errors exit `2`.

## Recommended one-line Hermes TUI prompt

```text
Run the enhancement-e2e-test skill with config /absolute/path/test_config.json and return the run ID, status, summary path, and failed steps.
```

This tests the real deterministic skill entrypoints and all resulting side effects.
To test natural-language parsing separately, keep one small contract test per skill
that checks whether a TUI prompt produces the correct fixture/spec before invoking
its backend.

## What the report captures

Each step stores:

- start/end timestamps and duration
- expected versus actual result
- invoked command and return code
- stdout/stderr file paths and output tails
- relevant database rows
- generated column metadata
- SSH reachability and remote command result
- traceback for any failure

Passwords, DSNs, tokens, and private-key-like fields are redacted from the report.
