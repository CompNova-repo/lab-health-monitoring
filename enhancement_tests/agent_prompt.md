You are setting up an end-to-end test harness for three completed Hermes TUI enhancements:

1. Add a new machine.
2. Add a new metric.
3. Add a new monitored application.

A starter package is available at:

/home/vagrant/.hermes/skills/hermes-enhancement-e2e/

The existing project contains:

- The completed `add-new-machine` Hermes skill (directory: `/home/vagrant/.hermes/skills/add-new-machine/`).
- The completed `new-metric-addition` Hermes skill (directory: `/home/vagrant/.hermes/skills/new-metric-addition/`).
- The completed `add-new-apps` Hermes skill (directory: `/home/vagrant/.hermes/skills/add-new-apps/`).
- A second metric skill, `metric-onboarding` (`/home/vagrant/.hermes/skills/metric-onboarding/`), which uses a spec-file interface instead of CLI arguments.
- The main monitoring script, `p1_fixed.py` (the authoritative one is at `/home/vagrant/.hermes/skills/new-metric-addition/p1_fixed.py`).
- A PostgreSQL database used by the monitoring system.
- A JSON file (`app_commands.json`) used by `p1_fixed.py` to discover monitored applications (located at `/home/vagrant/.hermes/skills/add-new-apps/app_commands.json`).

Your job is to inspect the existing implementation and fully integrate the starter test harness. Do not merely fill placeholders based on guesses.

Primary objective

Make this command execute one complete, evidence-backed integration test:

python e2e_runner.py --config test_config.json

The test must:

1. Add a reachable test machine using the real machine-onboarding backend.
2. Verify the machine row in machines.
3. Verify TCP and non-interactive SSH connectivity.
4. Run p1_fixed.py.
5. Verify that a new row for that machine appears in metric_samples.
6. Add a harmless test metric using the real metric-onboarding backend.
7. Verify the metric row in metric_registry.
8. Run the metric command directly over SSH and confirm numeric output.
9. Run p1_fixed.py.
10. Verify that the metric column exists in metric_samples.
11. Verify that the new metric has a non-null value for the test machine.
12. Add a harmless monitored application using the real application-onboarding backend.
13. Verify that its monitoring command is stored in the actual `app_commands.json` file.
14. Execute the exact stored command over SSH and confirm it succeeds.
15. Run p1_fixed.py.
16. Verify that a corresponding row appears in app_metric_samples.
17. Store all commands, database assertions, expected and actual values, stdout, stderr, failures, and timestamps in the generated test report.

Safety requirements

Do not run this against the production database.

Create or use a disposable PostgreSQL database whose name contains one of:

- test
- e2e
- qa
- staging

Import the current project schema into that database.

All four components must use the same test database:

- Machine-onboarding backend
- Metric-onboarding backend
- Application-onboarding backend
- p1_fixed.py

Prefer injecting the test DSN through environment variables rather than modifying hardcoded production credentials.

Do not use --allow-non-test-db.

Do not modify production data.

Do not permanently alter the production apps JSON.

If the application skill only supports one fixed JSON path, create an isolated test copy and configure both the application skill and p1_fixed.py to use that same test JSON during the test run.

Do not rewrite the three completed skills unless an integration defect prevents deterministic invocation. Any necessary adjustment must be minimal, backward-compatible, and documented.

Required discovery before configuration

Inspect the project and determine:

1. The deterministic Python entrypoint for the `add-new-machine` skill (`headless_onboard.py`) and its actual CLI arguments:

   ```
   python3 /home/vagrant/.hermes/skills/add-new-machine/headless_onboard.py \
       --alias "<alias>" --ip "<ip_address>" --user "<ssh_user>" \
       --key "<ssh_key_path>" --tags "<tags>" --monitor "<yes|no>"
   ```

2. The deterministic Python entrypoint for the `new-metric-addition` skill (`main.py`) and its actual CLI arguments:

   ```
   python3 /home/vagrant/.hermes/skills/new-metric-addition/main.py \
       --metric "<metric_name>" --command "<shell_command>" \
       --display-name "<display_name>" --db-type "<DB_TYPE>" --unit "<unit>"
   ```

3. The alternative metric entrypoint (`metric-onboarding` skill's `metric_onboard.py`) which uses a spec-file interface:

   ```
   python3 /home/vagrant/.hermes/skills/metric-onboarding/metric_onboard.py apply --spec <spec_file_path>
   ```

4. The deterministic Python entrypoint for the `add-new-apps` skill (`headless_add_app.py`) and its actual CLI arguments:

   ```
   python3 /home/vagrant/.hermes/skills/add-new-apps/headless_add_app.py \
       --app-name "<app_name>" --service-name "<service_name>" \
       --process-pattern "<process_pattern>" [--overwrite] [--local]
   ```

5. The exact command required to run p1_fixed.py once:

   ```
   python3 /home/vagrant/.hermes/skills/new-metric-addition/p1_fixed.py run --mode standard
   ```

   This version of p1_fixed.py differs from other copies in key ways:
   - It uses `require_env()` which **requires** `P1_DB_HOST`, `P1_DB_PORT`, `P1_DB_NAME`, `P1_DB_USER`, `P1_DB_PASSWORD` environment variables (no fallback defaults).
   - It also loads credentials from `/home/vagrant/.hermes/skills/new-metric-addition/.env` via `load_dotenv()`.
   - It supports these subcommands: `run --mode standard|highfreq`, `validate --machine <alias>`, and `collect-apps`.
   - It has mesh ping collection support via a `mesh_ping_results` table.

6. The environment variables or configuration files used by each component for its PostgreSQL connection.

   For most skills (headless_onboard.py, main.py, headless_add_app.py, metric_onboard.py), these env vars are used with defaults:

   - `P1_DB_HOST` (default: `127.0.0.1`)
   - `P1_DB_PORT` (default: `5432`)
   - `P1_DB_NAME` (default: `lab_monitoring_db`)
   - `P1_DB_USER` (default: `release_user`)
   - `P1_DB_PASSWORD` (default: `release_password`)

   **Important**: The authoritative `p1_fixed.py` at `/home/vagrant/.hermes/skills/new-metric-addition/p1_fixed.py` uses `require_env()` which means all four P1_DB_* variables **must** be set in the environment (no fallback defaults). It also loads from a `.env` file at `/home/vagrant/.hermes/skills/new-metric-addition/.env` via `load_dotenv()`. When running through the E2E harness, ensure these env vars are set via the test runner's `environment` config.

7. The actual path and structure of the monitored-apps JSON (`app_commands.json` at `/home/vagrant/.hermes/skills/add-new-apps/app_commands.json`):

   ```json
   {
     "version": 1,
     "apps": {
       "<app_name>": {
         "app_name": "...",
         "display_name": "...",
         "service_name": "...",
         "process_pattern": "...",
         "enabled": true,
         "commands": { ... },
         ...
       }
     }
   }
   ```

   The `list_path` in the runner config must be `"apps"` (not `"applications"`).

8. The actual columns in:

   - `machines` — server_id, alias, hostname, ip_address, ssh_port, ssh_user, ssh_key_path, monitoring_enabled, tags, os_name, os_version, cpu_model, cpu_cores, ram_gb, disk_total_gb, created_at, updated_at
   - `metric_registry` — metric_key, display_name, column_name (not `db_column`), unit, command_strategy, command_text, parser_name, timeout_seconds, chart_group, preferred_viz, threshold_warning, threshold_critical, enabled, created_at, updated_at
   - `metric_samples` — id, server_id, ts, source_mode, status, cpu_pct, ram_pct, swap_pct, disk_pct, disk_read_iops, disk_write_iops, disk_latency_ms, net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms, packet_loss_pct, load_avg_1m, load_avg_5m, load_avg_15m, process_count, uptime_seconds, systemd_failed_units_count, raw_extra, plus dynamically added columns
   - `app_metric_samples` — id, server_id, ts, app_name, display_name, status, cpu_pct, rss_memory_mb, process_count, thread_count, listening_sockets

9. Whether `metric_samples.id` exists — YES, it does (SERIAL PRIMARY KEY).

10. Whether `metric_samples.source_mode` and `metric_samples.status` exist — YES, both exist.

11. Whether `app_metric_samples.display_name` exists — YES.

12. Whether the registry uses `db_column` or `column_name` — it uses **`column_name`** (the table column is named `column_name`). `db_column` is used in spec files as an alias but maps to `column_name` in the DB.

13. Whether the registry uses `collection_command` or `command_text` — it uses **`command_text`** (the table column is `command_text`; `collection_command` is NOT a column in metric_registry).

14. How `p1_fixed.py` selects enabled machines — it queries `machines` WHERE `COALESCE(monitoring_enabled, true) = true`.

15. Whether it monitors every machine or requires an explicit flag — it requires `monitoring_enabled` to be true (or not set, since COALESCE defaults to true).

16. How application definitions are associated with machines — applications defined in `monitor_config.yaml` use a machine alias as a key. Applications registered via `app_commands.json` are collected locally or via SSH running the stored commands on each monitored machine.

Do not assume the starter package schema queries exactly match the current database. Update the harness queries where necessary while preserving the intended assertions.

Starter package setup

Extract the package into the Hermes skills directory or an appropriate project test directory.

The package contains:

- e2e_runner.py
- test_config.example.json
- audit_schema.sql
- requirements.txt
- README.md
- SKILL.md

Install the dependency in the Python environment used for testing:

python -m pip install -r requirements.txt

Copy:

cp test_config.example.json test_config.json

Replace all placeholders in test_config.json with verified values from the project.

The configured commands must be argument arrays, not shell command strings.

**Important**: The actual skill entrypoints do NOT use `--spec` flags. They use their own specific CLI arguments (documented above). You must adapt the command configuration to match the real interface of each skill. For skills that take inline arguments (like `headless_onboard.py` and `headless_add_app.py`), you will need to pass the fixture values inline in the command arguments, or create a thin wrapper script that reads the fixture JSON and forwards the parameters.

Example structure (note: actual args differ per skill — see required discovery above):

```json
{
  "commands": {
    "add_machine": [
      "python3",
      "/home/vagrant/.hermes/skills/add-new-machine/headless_onboard.py",
      "--alias", "{run_short}-test-machine",
      "--ip", "10.0.0.1",
      "--user", "testuser",
      "--key", "/home/testuser/.ssh/id_ed25519",
      "--tags", "e2e",
      "--monitor", "yes"
    ],
    "add_metric": [
      "python3",
      "/home/vagrant/.hermes/skills/new-metric-addition/main.py",
      "--metric", "e2e_load_1m_{run_short}",
      "--command", "awk '{print $1}' /proc/loadavg",
      "--display-name", "E2E Load Average 1m",
      "--db-type", "DOUBLE PRECISION",
      "--unit", "load"
    ],
    "add_app": [
      "python3",
      "/home/vagrant/.hermes/skills/add-new-apps/headless_add_app.py",
      "--app-name", "sshd",
      "--service-name", "ssh",
      "--process-pattern", "sshd",
      "--overwrite"
    ],
    "p1": [
      "python3",
      "/home/vagrant/.hermes/skills/new-metric-addition/p1_fixed.py",
      "run",
      "--mode",
      "standard"
    ]
  }
}
```

The exact arguments must come from the real implementation, not this example.

Test fixtures

Use a real reachable Linux test machine.

Machine fixture requirements:

- Unique alias containing {run_short}.
- Reachable IP address.
- Correct SSH user.
- Absolute private-key path.
- Correct SSH port.
- Non-interactive SSH authentication.
- Monitoring enabled.
- Hostname should either match the machine or be left empty if the onboarding process discovers it automatically.

Use a harmless numeric metric such as:

awk '{print $1}' /proc/loadavg

The metric must:

- Produce one numeric value.
- Require no privileged access.
- Complete quickly.
- Have a unique metric_key and column name containing {run_short}.
- Use a PostgreSQL-compatible numeric type.

Use an application that is already installed on the test machine.

Prefer a stable process such as sshd, but first verify its actual process name on the target OS.

Example command:

pgrep -x sshd >/dev/null && echo running || echo stopped

The application command must:

- Be harmless.
- Be non-interactive.
- Complete quickly.
- Match the format expected by p1_fixed.py.
- Be stored by the real application-onboarding backend.

Required harness adaptations

Review e2e_runner.py carefully and correct any mismatch with the real schema.

In particular, inspect and adapt these queries:

Machine sample verification

The starter query currently expects fields such as:

id, server_id, ts, source_mode, status, cpu_pct, ram_pct, disk_pct

Only select columns that actually exist.

The required assertion is simply:

- A metric_samples row exists.
- It belongs to the newly added machine.
- Its timestamp is after the relevant p1_fixed.py run started.

Metric sample verification

Only assume id or status exist if confirmed — in the current schema, both `id` and `status` exist.

The required assertions are:

- The dynamically registered column exists.
- A row for the test machine was inserted after the relevant P1 start time.
- The new metric value is not null.

Application sample verification

Only select confirmed columns.

The required assertions are:

- A row exists in app_metric_samples.
- It has the newly added machine's server_id.
- It has the expected app_name.
- Its timestamp is after the relevant P1 start time.

metric_registry column verification

The registry table uses **`column_name`** (not `db_column`) and **`command_text`** (not `collection_command`). Update the runner queries accordingly.

apps_json configuration

The `app_commands.json` file uses this structure:

```json
{
  "version": 1,
  "apps": {
    "<app_name>": {
      "app_name": "...",
      "display_name": "...",
      "service_name": "...",
      "commands": {
        "cpu_pct": "...",
        "rss_memory_mb": "...",
        ...
      }
    }
  }
}
```

The `list_path` in the runner's `apps_json` config must be `"apps"` (not `"applications"`).

Timestamp handling

Capture the timestamp immediately before each p1_fixed.py invocation.

Use that timestamp when checking the database, so old rows cannot make a failed run appear successful.

Allow a small clock-skew tolerance if the database server and runner machine do not have perfectly synchronized clocks.

A tolerance of approximately 5–10 seconds is acceptable and should be documented.

Failure behaviour

Continue after a failed step wherever later steps can still provide useful diagnostic evidence.

However:

- If the machine was not inserted, machine-dependent SSH checks should be marked skipped.
- If SSH is unavailable, remote metric and application command checks should be marked skipped or failed with a clear dependency reason.
- If the metric was not registered, dynamic metric verification should be skipped.
- If the application record was not written, application command execution should be skipped.

Do not report an overall pass when any required assertion failed or was skipped.

Evidence requirements

Every run must create a unique directory under test-results, containing:

- Generated machine fixture
- Generated metric fixture
- Generated application fixture
- Command stdout files
- Command stderr files
- summary.md
- report.json

The report must include:

- Run ID
- Overall status
- Test database name
- Start and end time
- Duration of every step
- Expected result
- Actual result
- Database rows used as evidence
- Commands invoked
- Return codes
- Output file paths
- Relevant output tails
- Exception type
- Traceback
- Failure reason

Sensitive values must be redacted:

- Passwords
- Database DSNs
- Tokens
- Secrets
- Private-key contents

Storing the private-key path is acceptable; never store or print private-key contents.

The optional audit tables should also be created in the disposable test database:

- enhancement_test_runs
- enhancement_test_steps

Natural-language Hermes testing

The primary harness should test deterministic backend entrypoints.

Separately add one lightweight contract test per Hermes skill to verify that a one-line TUI prompt is correctly converted into the backend input.

Do not make the main integration test dependent on LLM interpretation because that would make failures harder to diagnose.

The full validation should therefore have two layers:

Layer 1: TUI contract tests

For each skill:

- Provide one known prompt.
- Capture the generated JSON/spec.
- Confirm required fields are correct.
- Do not run the entire monitoring integration here unless the backend is deliberately invoked.

Layer 2: Deterministic E2E integration test

Run e2e_runner.py using known fixtures and real backend entrypoints.

This layer is the authoritative integration test.

Final verification

Run the harness once.

Inspect:

- Exit code
- summary.md
- report.json
- Audit database rows
- Machine row
- Metric registry row
- Dynamic metric column
- Metric sample value
- Application JSON record
- Application metric sample

Fix all configuration or schema mismatches found during the run.

The setup is complete only when:

python e2e_runner.py --config test_config.json

returns exit code 0 and every required step is marked passed.

Deliverables

At completion, provide:

1. The final path of the installed test harness.
2. The final test_config.json, with secrets redacted in the response.
3. A list of any files changed outside the starter package.
4. The exact command used to run the test.
5. The run ID.
6. The overall status.
7. The paths to summary.md and report.json.
8. A concise explanation of any harness changes made to match the real schema.
9. A concise explanation of how the test database and application JSON were isolated.
10. Any remaining limitations.

Do not claim success based only on command exit codes. Success must be proven through the database, JSON, SSH, and generated report assertions.
