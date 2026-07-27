# p1_helper.py — how it works, what it reads/writes, and how to onboard a machine

This documents the current, as-built behavior of `p1_helper.py` (verified by
running it against a real target on 2026-07-27). No architecture changes were
made — this is a reference, plus one bug fix (see "Bug found & fixed" below).

## Files it reads

| File | Purpose | Required? |
|---|---|---|
| `.env` (repo root) | `P1_DB_HOST`, `P1_DB_PORT`, `P1_DB_NAME`, `P1_DB_USER`, `P1_DB_PASSWORD` — the Postgres connection. | Required. Without it, DB_DSN silently falls back to hardcoded defaults (`release_user`/`release_password`/`lab_monitoring_db`), which will fail auth against a real DB. |
| `monitor_config.yaml` (repo root) | Per-machine **monitoring policy**: `thresholds` (ram/disk/cpu/error_count/warning_count), `package_checks` (systemd services to check), `apps` (name+pgrep pattern for app-level metrics), `packages` (OS package install/version tracking), `network_checks` (ping/dns targets), `top_n`, `disk_device`, `network_interface`. Also top-level `settings.consecutive_threshold_breaches` and `service_aliases`. | Optional per machine — a machine with no entry still gets basic stats collected, just no thresholds/services/apps/packages/network checks. |
| `system_state.json` (repo root) | Crash-safe bookkeeping: per-machine breach counters, `install_state` (NORMAL/INSTALLING), last log-check watermark. This script reads it but never writes it to disk — it prints a `new_system_state` object that **the calling agent** is responsible for writing back. If you run `p1_helper.py` standalone (as we did for this test), this file is never updated on disk. | Optional (defaults to `{}` if missing). |
| `metric_registry.yaml` (repo root) | Optional custom metrics (e.g. inode usage, cpu iowait) run via SSH, parsed by `metric_runtime.py`. | Optional — only used if `metric_runtime.py` imports successfully and the file exists. |
| **Postgres `machines` table** | The **sole source of truth for SSH targets**: `alias`, `ip_address`, `ssh_port`, `ssh_user`, `ssh_key_path`, `monitoring_enabled`. `monitor_config.yaml` has no host/port/user/key fields at all — it only ever keys off the same `alias` string used in this table. | Required — a machine with no row here (or `monitoring_enabled=false`) is never touched, regardless of what's in `monitor_config.yaml`. |

So the split you noticed is real and intentional as currently built: **which
machines to monitor and how to SSH to them lives only in Postgres**;
**what to check on them** (thresholds/services/apps/packages/network checks)
lives only in `monitor_config.yaml`. There's no admin UI for either yet —
today, adding a `machines` row is done with a direct SQL `INSERT` (that's how
every existing row got there), same as editing `monitor_config.yaml` is a
direct file edit. (We discussed moving `apps`/monitoring policy into Postgres
too, for easier customer self-service — holding off on that per your
request; flag it again whenever you want to revisit.)

## Files it writes

- **Never writes any file itself.** It prints one JSON blob to stdout containing `new_system_state`, which the calling agent is expected to write to `system_state.json` via its own `write_file` step.
- **Writes directly to Postgres** (autocommit, no cross-machine transaction): `machines` (upsert hardware/OS inventory), `machine_state`, `metric_samples`, `service_status`, `events` (alerts), `app_metric_samples`, `package_state`, `network_check_samples`, `network_summaries`, `log_summaries`, `top_processes`, plus `metric_samples` custom-metric columns if `metric_registry.yaml` is in use.

## CLI usage

```bash
python3 p1_helper.py run --mode standard    # processes all machines.install_state == NORMAL (or missing)
python3 p1_helper.py run --mode highfreq    # processes all machines.install_state == INSTALLING; no-op if none
python3 p1_helper.py validate --machine <alias>   # single-machine sanity check, no DB writes
```

`run` has **no per-alias filter** — it loops over every row in `machines`
where `monitoring_enabled = true`. Current DB has ~54 such rows, many of
which are mock/legacy test entries (private `10.x` addresses, `mock-*`
aliases) that are unreachable from here — each one costs SSH-timeout time
(up to ~20s+ per collector) on every `run` invocation. Worth pruning
`monitoring_enabled` on stale rows before this runs on a real cron cadence,
or a real `run --mode standard` could take a long time / not finish within
its cron interval. Not touched in this session — flagging it since it
affects whether the script "works as intended" in production.

## Onboarding a new machine (what needs to be done)

1. **Add a `machines` row** (direct SQL — no script/UI does this today):
   ```sql
   INSERT INTO machines (alias, ip_address, ssh_port, ssh_user, ssh_key_path, monitoring_enabled)
   VALUES ('my-alias', '1.2.3.4', 22, 'someuser', '~/.ssh/some_key', true);
   ```
   `ssh_key_path` is `os.path.expanduser()`'d at connect time, so `~/...`
   paths are fine as long as they resolve correctly for whatever user account
   actually runs `p1_helper.py`.
2. **Ensure passwordless SSH works**: the script always connects with
   `BatchMode=yes` (no password prompts) and `StrictHostKeyChecking=no`. The
   given key must already be authorized on the target (`~/.ssh/authorized_keys`).
3. *(Optional)* **Add a `monitor_config.yaml` block** for the same alias if
   you want thresholds, service checks, per-app metrics, package tracking,
   network checks, or disk/network I/O rate collection. Without one, the
   machine still gets basic stats (ram/disk/cpu/swap/load/uptime/process
   count/failed units) with no alerting.
4. Run `python3 p1_helper.py run --mode standard` (or wait for the cron job).

## Bug found & fixed

`p1_helper.py` computed two different notions of "ROOT":
- `ROOT = os.getcwd()` for `monitor_config.yaml` / `system_state.json` / `metric_registry.yaml`.
- Then re-assigned `ROOT = Path(__file__).resolve().parent.parent` just for loading `.env`.

Since `p1_helper.py` lives directly in the repo root (next to `.env`),
`parent.parent` climbed one directory too far (looked for `.env` at the
parent of the repo, which doesn't exist). `load_dotenv()` failed silently,
so `DB_DSN` used its hardcoded fallback credentials
(`release_user`/`release_password`/`lab_monitoring_db`) instead of the real
`.env` values — meaning **every DB write was failing before this fix**,
authenticating as the wrong user against the wrong database name.

Fix: load `.env` from the same `os.getcwd()`-based `ROOT` already used for
the other config files, removing the now-unused `pathlib` import. Confirmed
this actually was breaking things by running `p1_helper.py run --mode
standard` before the fix (`password authentication failed for user
"release_user"`) and after (connects and writes successfully).

## Test performed

Target: `ssh -i ~/.ssh/hermes_p1_test advaithbalaji24@34.61.245.177`
(Debian 13 GCP instance, hostname `instance-20260619-035117`).

1. SSH'd in first to see what's actually running (per your answer) — found:
   `mariadb` (listening on 3306), `ssh`, `exim4`, `cron`, standard Debian/GCP
   agent services. No `monitor_config.yaml` entry was added for this
   machine (out of scope per your "no changes" answer) — it's being
   monitored with default/no thresholds for now.
2. Added a `machines` row: alias `hermes_p1_test`, the given IP/user/key,
   `monitoring_enabled = true`.
3. Ran the actual collection + DB-write functions (`evaluate_machine` +
   `persist_to_db`, the same functions `cmd_run` calls per-machine)
   scoped to just this one alias, to avoid waiting on the ~54 unreachable
   legacy rows. Result: `status: "ok"`, `db_write_ok: True`, no errors.
4. Verified rows actually landed in Postgres: `machines` (hostname, OS,
   CPU, RAM, disk correctly populated), `machine_state` (`install_state:
   NORMAL`, `last_checked` timestamp), `metric_samples` (cpu 4.8%, ram
   16.2%, disk 34%, status `ok`).
5. Also ran the real CLI entrypoint `python3 p1_helper.py validate --machine
   hermes_p1_test` directly (this one only touches a single alias, so it's
   safe/fast to run as-is) — returned `"passed": true` with matching stats.

Both the full pipeline and the CLI `validate` path work correctly against a
real target after the `.env` fix. The `hermes_p1_test` row is left in the
`machines` table with `monitoring_enabled = true` — say the word if you'd
rather I disable or remove it.
