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
4. Run the orchestrator with the generated command.
5. If the orchestrator fails during the validation phase (e.g., SSH timeouts), do not repeatedly retry the same command. Investigate the connectivity to the validation host or inform the user that the live-check is blocking the installation.

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

## Contract

The generated metric script must:

* be saved inside `new-metrics/`
* print only valid JSON
* include `status`, `value`, and `raw_output`

The registry decides:

* metric key
* script path
* DB column
* DB type
* enabled status

## Important: Do NOT edit main.py

The `main.py` file is the orchestrator, not a catalog. It should never be modified to add new metrics. Always use the `--command` flag to pass your generated shell commands.