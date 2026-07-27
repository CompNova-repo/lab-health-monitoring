---
name: new-metric-addition
category: new-metric-addition
description: Orchestrates the addition of new system monitoring metrics including validation, script generation, and DB schema updates.
---
# Skill: New Metric Addition
Trigger: \"add metric\", \"create a new metric\", \"monitor a new KPI\"

## System Instructions

You are the entry point for custom metric onboarding.

**Your job:**

1. Understand the exact metric the user wants to add (what KPI, shell command, data type, unit).
2. **Generate the shell command** yourself — do NOT rely on a hardcoded catalog. Think about how the metric would be collected (e.g. `ss`, `df`, `top`, `/proc`, `curl`, etc.) and write the correct shell pipeline.
3. **Do NOT modify `main.py`** — never edit the orchestrator file. The `--command` flag exists so you can pass your generated command directly.
4. Run the orchestrator with the generated command. Hermes must not run `ssh`, `scp`, or target-machine shell commands directly. All target-machine validation must happen by invoking the deterministic Python orchestrator below.
5. The orchestrator must load validation targets from the PostgreSQL `machines` table and use exactly `machines.ip_address` as the SSH host, along with `ssh_port`, `ssh_user`, and `ssh_key_path`. Do not use `hostname`, `ssh_targets.yaml`, hardcoded IPs, or fallback/default SSH targets.
6. Read the orchestrator JSON result. A successful run means:
   * the raw shell command succeeded on at least one enabled target machine
   * the generated `new-metrics/<metric_key>.py` script was executed on that same target machine
   * the generated script printed valid JSON containing `status`, `value`, and `raw_output`
   * the generated script's parser extracted a numeric value without returning `status: "error"`
7. If the orchestrator returns an error with `stage: "command_validation"` or `stage: "generated_script_validation"`, generate a corrected command and rerun the orchestrator. Try at most 5 total command attempts for the metric.
8. If the orchestrator fails because validation hosts are unreachable, the database is unavailable, SSH credentials are missing, or another environment dependency is blocking validation, do not burn all 5 attempts on the same command. Report the blocker and stop.

## Command Quality Rules

Generated commands must measure the requested KPI directly. Do not hide missing data or unsupported systems behind synthetic success values.

* Do not use fallbacks like `|| echo 0`, `|| true`, `; echo 0`, or similar default literals.
* If the metric source is unavailable, the command must exit non-zero and print an explanatory error to stderr.
* Do not collect CPU temperature from `/proc/cpuinfo`; it normally does not expose CPU temperature.
* For CPU temperature on Linux, use thermal or hwmon sensor files and validate the value is in Celsius, greater than 0, and plausibly below 130.

## Execution Hook

```bash
python3 ~/.hermes/skills/new-metric-addition/main.py \\
    --metric "<user_provided_metric_name>" \\
    --command "<shell_command_you_generated>" \\
    --display-name "<friendly_name>" \\
    --db-type "<DOUBLE_PRECISION | INTEGER | BIGINT | TEXT | BOOLEAN>" \\
    --unit "<unit>"
```

### Examples

| User says | Hermes generates |
|---|---|
| "add metric to track established connections" | `--metric "Established Connections" --command "ss -t state established | wc -l" --display-name "Established Connections" --db-type INTEGER --unit count` |
| "monitor inode usage" | `--metric "Inode Usage Percentage" --command "df -Pi / | awk 'NR==2 {gsub(/%/, \"\", $5); print $5}'" --display-name "Inode Usage Percentage" --db-type "DOUBLE PRECISION" --unit "%"` |
| "add cpu iowait percentage" | `--metric "CPU I/O Wait Percentage" --command "top -bn1 | awk '/Cpu\\(s\\)/ {gsub(/,/, \"\"); for(i=1;i<=NF;i++) if ($i ~ /wa/) print $(i-1)}'" --display-name "CPU I/O Wait Percentage" --db-type "DOUBLE PRECISION" --unit "%"` |
| "add cpu temperature" | `--metric "CPU Temperature" --command "for f in /sys/class/thermal/thermal_zone*/temp /sys/class/hwmon/hwmon*/temp*_input; do [ -r \"$f\" ] || continue; v=$(cat \"$f\") || continue; case \"$v\" in ''|*[!0-9.-]*) continue;; esac; awk -v v=\"$v\" 'BEGIN { if (v > 1000) v = v / 1000; if (v > 0 && v <= 130) { print v; exit 0 } exit 1 }' && exit 0; done; echo 'cpu temperature sensor not found' >&2; exit 1" --display-name "CPU Temperature" --db-type "DOUBLE PRECISION" --unit "C"` |

## Contract

The generated metric script must:

* be saved inside `new-metrics/`
* print only valid JSON
* include `status`, `value`, and `raw_output`
* return `status: "ok"` with a numeric `value` when run on the validation target

The orchestrator must validate both layers before registry or schema changes:

* raw command execution on an enabled target machine
* generated Python script execution on that same target machine, including its JSON output and regex/parser result

The registry decides:

* metric key
* script path
* DB column
* DB type
* enabled status

## Important: Do NOT edit main.py

The `main.py` file is the orchestrator, not a catalog. It should never be modified to add new metrics. Always use the `--command` flag to pass your generated shell commands.
