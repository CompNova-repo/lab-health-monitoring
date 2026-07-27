# p1_helper.py — full feature test report

Date: 2026-07-27
Target: `hermes_p1_test` (`34.61.245.177`, Debian 13, GCP instance
`instance-20260619-035117`, running `mariadb`, `ssh`, `exim4`, `cron`)

Goal: exercise every collector/write path the script offers against a real
machine, confirm rows land in Postgres correctly, and report what actually
happened — including two real bugs this surfaced.

**Update (same day): the CPU % bug found below has been fixed and
re-verified live against `hermes_p1_test`.** See "Fixes applied & verified"
at the end. The `cpu_i_o_wait_percentage` custom-metric bug is still open.

No architecture changes were made. `monitor_config.yaml` got one new,
realistic, permanent block for `hermes_p1_test` (thresholds 90/85/95/50/200,
matching every other machine in the file). Everything else in this report —
the artificially low thresholds and the fake service name used to force
alerts — only ever existed in an in-memory config dict inside the test
script; `monitor_config.yaml` was never touched with those values.

## Result summary

| Feature | Exercised? | Result |
|---|---|---|
| `.env` / DB connection | yes | OK (fixed earlier this session) |
| `monitor_config.yaml` parser (`load_monitor_config`) | yes | OK — nested `apps:`/`network_checks:` lists, `disk_device`, `network_interface`, `settings` all parsed correctly |
| Resource stats (ram/disk/swap/load/uptime/process count) | yes | OK, values correct |
| **CPU % stat** | yes | **BUG, fixed & re-verified.** Was reporting the exact inverse of reality under a common condition |
| systemd failed-units count | yes | OK (0) |
| Hardware/OS inventory → `machines` upsert | yes | OK |
| `package_checks` → `service_status` | yes | OK — cron/ssh/mariadb/exim4 all correctly "active"; a deliberately fake service correctly came back "inactive" |
| `apps` → `app_metric_samples` | yes | OK — mariadb cpu/rss/process/thread counts captured |
| `packages` → `package_state` | yes | OK — mariadb-server/openssh-server/exim4-base with correct installed versions |
| `network_checks` (ping + dns) → `network_check_samples` | yes | OK — both `status: ok` with real latency |
| Connection/listening-port summary → `network_summaries` | yes | OK |
| journalctl error/warning counts → `log_summaries` | yes | OK |
| Top-N processes by cpu/mem → `top_processes` | yes | OK |
| Disk I/O rate (`disk_device: sda`) | yes | OK — read/write IOPS + latency populated |
| Network throughput (`network_interface: ens4`) | yes | OK — rx/tx bytes/sec populated |
| Gateway ping/TCP-RTT fallback | yes | OK |
| Custom metric: `inode_usage_percentage` | yes | OK, value 15.0 landed in DB |
| Custom metric: `established_connections` | yes | OK, value landed in DB |
| Custom metric: `time_wait_connections` | yes | OK, value landed in DB |
| Custom metric: `cpu_i_o_wait_percentage` | yes | **BUG — see below.** Never reaches the DB, for two separate reasons |
| Consecutive-breach counting (`ram`/`disk`/`cpu`/`error_count`/`warning_count`) | yes | OK — counter went 0→1→2 across two runs, alert fired only once ≥ threshold (2) |
| Immediate service-down alert (no counter) | yes | OK — fired on both runs, correct message |
| Alert persistence → `events` | yes | OK — severity, metric, value, threshold, consecutive_breaches, message all correct |
| CLI `run --mode standard` (per-machine logic) | yes, via direct function calls* | OK |
| CLI `validate --machine <alias>` | yes, real CLI invocation | OK (also how bug #1 was first noticed) |

\* *`run --mode standard` itself loops over all ~54 `machines` rows with no
per-alias filter (most unreachable mock/legacy entries) — as flagged last
time, actually invoking it end-to-end takes a very long time, so I called
the exact same `evaluate_machine`/`persist_to_db` functions it uses
internally, scoped to just this one alias.*

## Row counts, `hermes_p1_test` (`server_id 609e95c0-0e0f-449f-ac49-3516a347bffc`)

| table | before this session | after |
|---|---|---|
| `machines` | 1 | 1 (upsert, correctly no duplicate) |
| `machine_state` | 1 | 1 (upsert, correctly no duplicate) |
| `metric_samples` | 1 | 3 |
| `service_status` | 0 | 5 (cron, ssh, mariadb, exim4, +1 fake) |
| `app_metric_samples` | 0 | 2 |
| `package_state` | 0 | 3 |
| `network_check_samples` | 0 | 4 |
| `network_summaries` | 1 | 3 |
| `log_summaries` | 1 | 3 |
| `top_processes` | 10 | 30 |
| `events` | 0 | 4 |

## Bug 1 (high severity, FIXED): CPU % can report the exact opposite of reality

`REMOTE_STATS_CMD` computes CPU usage with:
```
top -bn1 | awk '/Cpu\(s\)/ {print 100 - $8}'
```
This assumes `$8` is always the idle percentage. `top` right-justifies each
field to a fixed width. When idle is a 3-digit value (`100.0`, which happens
whenever the box is briefly fully idle — very common on a quiet lab/test
machine), the preceding comma loses its separating space and two fields
merge into one:

```
%Cpu(s):  0.0 us,  0.0 sy,  0.0 ni,100.0 id,  0.0 wa, ...
                              ^^^^^^^^^^ "ni," and "100.0" become ONE awk field
```

That shifts every subsequent field left by one, so `$8` becomes the literal
string `"id,"` instead of the idle number. Awk coerces a non-numeric string
to `0` in arithmetic, so `100 - $8` evaluates to `100 - 0 = 100`. Verified
by replaying the exact captured line through `awk` directly — confirmed
`100 - $8 = 100`.

**Effect**: a fully-idle machine (true CPU usage ≈ 0%) gets reported as
`cpu_pct: 100.0`. This isn't a rare edge case for a monitoring product — any
lightly-loaded box will hit exactly 100.0% idle periodically. I hit this
live: `validate --machine hermes_p1_test` returned `"passed": false` with
`"cpu": {"value": 100.0, "threshold": 95.0}` on a machine that was actually
sitting at ~4-5% CPU. That's a false CPU alert generated by the parser, not
the machine — the worst kind of monitoring bug (fires exactly when things
are fine). **Fixed** — see below.

## Bug 2 (medium severity): `cpu_i_o_wait_percentage` custom metric never lands in the DB

Two independent problems stack here:

1. **Collector command assumes the wrong `top` output format.** The command
   in `metric_registry.yaml` is:
   ```
   top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\([0-9.]*\)% wa.*/\1/'
   ```
   This expects a field glued as `N.N%wa` (older/RHEL-style `top`). This
   Debian 13 box's `top` (procps-ng) prints `N.N wa,` — number, space,
   label, no attached `%`. The `sed` pattern never matches, so it falls
   through and returns the entire raw `top` line unparsed, which fails
   float parsing → no value collected. Verified by running the exact
   generated remote command manually over SSH.
2. **Even if #1 is fixed, a naming mismatch would still drop it.**
   `metric_registry.yaml` declares this metric's `column_name` as
   `cpu_i_o_wait_percentage`, so `collect_custom_metrics_via_ssh` would
   store any successfully-parsed value under `stats["cpu_i_o_wait_percentage"]`.
   But `db_write_metric_sample()` in `p1_helper.py` hardcodes reading
   `stats.get("cpu_iowait_percentage")` — a different string (no `_i_o_`).
   That key will never be present, so the INSERT always sends `NULL` for
   this column regardless of what was collected.

Confirmed via direct DB inspection: the row where this metric was collected
has correct values for the other three custom metrics
(`inode_usage_percentage: 15.0`, `established_connections: 14.0`,
`time_wait_connections: 3.0`) but `cpu_iowait_percentage: NULL`.

Not fixed yet — flagging for you to confirm the intended column name
before I touch `metric_registry.yaml` again.

Side note, not a bug, just clutter: `metric_samples` also has two unused
orphan columns (`cpu_iowait_pct`, `inode_used_pct`) left over from an
earlier naming convention — harmless, nothing writes to them, but worth
knowing they're dead weight if you ever prune the schema.

## Housekeeping from this test

- `service_status` and `events` now have permanent rows for a fake service
  name (`hermes-fake-svc-xyz`) used only to prove the immediate
  service-down alert path works. `monitor_config.yaml` does **not**
  reference this service — it only ever existed in the test script's
  in-memory config. Say the word if you'd like me to delete those rows;
  otherwise they'll just sit there as an inert "inactive" entry.
- The two forced-breach test rows (thresholds `ram/disk/cpu: 1`) are real
  `metric_samples`/`events` rows, clearly identifiable by their thresholds
  and timestamps in the tables above, if you want them pruned later.
- `monitor_config.yaml`'s new `hermes_p1_test` block uses real, sane
  thresholds (90/85/95/50/200) — nothing artificial was left in the
  committed config.

## Fixes applied & verified

Bug 1 predates this session (not something introduced earlier) and is now
fixed. Bug 2 is still open.

**Bug 1 fix** — [p1_helper.py](p1_helper.py)'s `REMOTE_STATS_CMD` CPU
extraction no longer indexes a fixed awk field. It now pipes through
`grep 'Cpu(s)' | sed -E 's/.*[^0-9]([0-9]+\.[0-9]+)%? *id.*/\1/' | awk
'{print 100 - $1}'` — this anchors on the text immediately *after* the idle
number (`id`, with or without an attached `%`), which stays intact
regardless of whether the preceding field glued onto it. A non-match now
falls through to `to_float()` returning `null` (safe) instead of a
plausible-looking wrong number.

Verified:
- Replayed the exact previously-broken line (`...ni,100.0 id,...`) through
  the new command directly: now correctly yields `0` (previously `100`).
- Confirmed the same command still parses both the normal Debian/procps-ng
  format (`95.2 id,`) and a synthetic RHEL-style format (`99.7%id,`)
  correctly (`4.8` and `0.3` busy%, respectively).
- Re-ran the real CLI: `python3 p1_helper.py validate --machine
  hermes_p1_test` now returns `"passed": true` with `"cpu_pct": 4.8` — the
  false `100.0 > 95` alert is gone.

Bug 2 (the `cpu_i_o_wait_percentage` custom metric) has not been touched —
it needs a decision on the intended column name (`cpu_i_o_wait_percentage`
vs. the existing `cpu_iowait_percentage` column) before fixing the naming
side of it.

## Bottom line

Every collector and DB write path works correctly except Bug 2 above, which
is still open. Bug 1 (the CPU % inversion, the more serious of the two
since it produced live false alerts) is fixed and re-verified against the
same real machine. Outstanding items, unchanged from before (not addressed
this round, still your call):
- Bug 2 itself (`cpu_i_o_wait_percentage` never reaching the DB).
- The ~54-row `machines` table still has many unreachable mock/legacy
  entries slowing down a full `run --mode standard`.
- The test-only rows noted in "Housekeeping from this test" above (fake
  service, forced-breach samples) are still sitting in the DB if you want
  them pruned.
