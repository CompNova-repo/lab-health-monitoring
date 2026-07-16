# Intelligence Insights

## Description
Generates cohesive, weekly Markdown reports of machine health and intelligence insights using native database aggregations with advanced statistical analysis.

## Commands

### `/generate-insights`
Triggers the analysis report generation. Can run across the entire database or target a specific machine.

**Usage:**
`/generate-insights` (Runs all machines)
`/generate-insights --machine [alias_or_server_id]` (Runs a specific machine)
`/generate-insights --machine [alias_or_server_id] --days 14` (Custom lookback period)

**Execution Instructions for Hermes:**
1. Parse the user's command. If a `--machine` flag is provided, execute: 
   `python ~/.hermes/skills/intelligence-insights/scripts/db_aggregator.py --machine "<target>"`
   If no flag is provided, execute without arguments to fetch all machines.
2. Read the resulting JSON output from the terminal. The JSON now includes richer statistical fields:
   - `daily_rollups`: time-bucketed daily avg/max per metric
   - `trends`: linear regression slope (units/day) + R² per metric
   - `temporal_patterns`: hourly-bucketed avg/max per metric for rhythm detection
   - `fleet_context`: per-metric z-scores comparing this machine to the fleet average
   - `correlations`: pairwise Pearson coefficients between key metric pairs
3. Read the historical context from `~/.hermes/skills/intelligence-insights/scripts/historical_insights.json` (if it exists).
4. Act as an expert infrastructure analyst. For EVERY machine in the JSON array, synthesize the data using the ADVANCED INSIGHT DIRECTIVES below.
5. Generate a cleanly formatted, Streamlit-compatible Markdown report comparing current health to the historical context. Do not use complex HTML.
6. Save the final compiled Markdown report to `~/.hermes/skills/intelligence-insights/scripts/weekly_report.md`.
7. Generate a new 2-3 sentence summary state of your findings and overwrite `historical_insights.json` with it to serve as next week's baseline.

---

## ADVANCED INSIGHT DIRECTIVES (STRICT OMISSION RULES)

You have access to advanced statistical fields (trend slopes, R² values, fleet z-scores, hourly temporal buckets, and Pearson correlations). You MUST adhere to the following rules to prevent report bloat:

**GOLDEN RULE:** Brevity is a feature. A short, 3-paragraph report with zero advanced insights is preferred over a long report filled with forced observations. If the data is mundane, keep the report brief.

**OMISSION RULE:** If a threshold is NOT met for any insight type below, that section MUST NOT EXIST in the markdown. Do not write "No correlations found" or "Trends are stable." Simply omit the header and text entirely.

**RECOMMENDATION RULE:** NEVER provide a recommendation (e.g., "You should downsize") unless the corresponding threshold is met. If the machine is healthy but heavily utilized, offer no recommendations.

---

### 1. Predictive Capacity Runway

**Purpose:** Tells the operator *when* a resource will hit critical thresholds based on current consumption velocity, enabling proactive action before alerts fire.

**Trigger (ALL must be true):**
- `trends[metric].r_squared > 0.7` (statistically significant trend)
- `trends[metric].slope_per_day > 0` (upward trajectory)
- Projected days until breach = (threshold - current_avg) / slope_per_day < 90

**When triggered, include a section like:**
> 📈 **Capacity Runway:** Memory usage is growing at `{slope_per_day}% per day` (R²={r_squared}). At this velocity, the 85% warning threshold will be breached in **~{projected_days} days**.

**Metrics to check:** `ram_pct`, `disk_pct` (growth-prone), `cpu_pct` (if consistently trending up).

---

### 2. Cross-Metric Correlation

**Purpose:** Reveals hidden systemic bottlenecks by mathematically linking seemingly unrelated resources (e.g., network traffic causing disk I/O wait).

**Trigger:**
- `correlations[].strength == "strong"` (|Pearson| > 0.7)

**When triggered, include a section like:**
> 🔗 **Correlation Detected:** `{pair}` has a Pearson coefficient of `{pearson}`. High `{metric_a}` strongly correlates with `{metric_b}`, suggesting a shared bottleneck.

**Interpretation guidance for specific pairs:**
- `CPU ↔ Network Latency`: High CPU causing queuing delays on network packets
- `Network Rx ↔ Disk Latency`: Incoming data saturating the storage write path
- `Disk Write ↔ CPU`: Write-heavy workload consuming CPU cycles on I/O wait
- `Packet Loss ↔ Network Latency`: Congestion leading to dropped packets (usually a network-layer issue)
- `Network Rx ↔ Tx`: Symmetrical traffic suggests balanced workload; asymmetry suggests a backup/mirror task

---

### 3. Fleet Cohort Deviation

**Purpose:** Instantly isolates whether a metric anomaly is local to this machine or a fleet-wide phenomenon. A `|z-score| > 2` means the machine is a significant outlier.

**Trigger:**
- Any metric in `fleet_context` has `|z_score| > 2.0`

**When triggered, include a section like:**
> 👥 **Fleet Context:** `{metric}` averages `{this_machine_avg}` vs. fleet average of `{fleet_avg}` (z-score: {z_score}). This is a significant deviation — the rest of the fleet is operating normally, isolating the issue to this specific host.

**When NOT triggered:** Omit entirely. A machine performing at fleet baseline is unremarkable and should not be called out.

#### 🚨 The "Ghost Town" Rule (Zero-State Isolation)

**Override for Fleet Context:** If ALL of the following are true, do NOT frame this as merely a "deviation" or "low baseline":
- Mesh ping success rate < 5% (machine is unreachable from all sources)
- AND `daily_rollups[].avg_net_rx_bytes_sec` < 1024 (sustained throughput < 1 KB/s)
- AND `daily_rollups[].avg_cpu_pct` < 5 (negligible compute)

**Action:** Explicitly flag this as a potential **Isolation, Application Crash, or Agent Failure.**

> 👻 **Ghost Town Detected:** `{machine}` is unreachable from all mesh sources (0% ping success) with near-zero CPU and network activity. This machine is not simply "underutilized" — it is likely isolated, crashed, or its agent has failed. Investigation required.

**Rationale:** A healthy, active machine rarely has absolute zero network receptiveness combined with negligible CPU and throughput. When every activity metric is near-zero, the machine is probably dead, not quiet.

---

### 4. Temporal Rhythm / "Cron Blame"

**Purpose:** Detects rhythmic anomalies caused by scheduled tasks (backups, cron jobs, log rotation) by checking if spikes cluster within a recurring time window.

**Trigger (ALL must be true):**
- In `temporal_patterns`, identify the 2-hour rolling window with the highest `avg_{metric}` for CPU or disk-write metrics
- If that window's values are >80% of all spike-level readings AND the window repeats daily

**When triggered, include a section like:**
> ⏱️ **Temporal Pattern:** CPU spikes to `{max_cpu}%` exclusively between `{start_hour}:00 and {end_hour}:00 UTC` daily. This rhythm strongly suggests a scheduled nightly backup, snapshot, or log-rotation cron job rather than organic user traffic.

**Metrics to check:** `cpu_pct`, `disk_write_iops`, `disk_latency_ms` (most cron-sensitive).

---

### 5. FinOps / Right-Sizing Headroom

**Purpose:** Identifies severely over-provisioned machines where downsizing would save costs without risking stability. Only flag when the evidence overwhelmingly supports a downgrade.

**Trigger (ALL must be true for the entire 7-day period):**
- `max_cpu` never exceeded 30%
- `max_ram` never exceeded 40%
- Zero critical spikes (no samples where cpu_pct > 90 or ram_pct > 90)

**When triggered, include a section like:**
> 💰 **Efficiency Insight:** This machine is over-provisioned. Peak usage over 7 days never exceeded `{max_cpu}%` CPU / `{max_ram}%` RAM. Downsizing the instance class by one tier would save resources without risking stability.

**When NOT triggered:** Omit entirely. Do not suggest scaling up or make any recommendation.

#### 🚨 The FinOps Sanity Check (Right-Sizing Veto)

**Veto override:** Even if the FinOps thresholds are met (CPU < 30%, RAM < 40%, zero spikes), you MUST check activity metrics before recommending downsizing.

**Veto trigger:** Core activity metrics are near zero:
- Mesh ping success rate < 5% (machine is unreachable)
- `established_connections` ≈ 0
- Network throughput (`net_rx_bytes_sec`, `net_tx_bytes_sec`) < 1 KB/s sustained

**If veto triggers:** Do NOT recommend downsizing. Instead, flag the inactivity.

> ⚠️ **Inactivity Detected:** While CPU/RAM appear over-provisioned, activity metrics show near-zero utilization — this machine may be idle, disconnected, or broken. Resolve connectivity before considering downsizing.

**If veto does NOT trigger (activity metrics are normal AND FinOps thresholds met):** Proceed with the standard Efficiency Insight recommendation.

---

### Report Structure Template

When writing the report, follow this structure in order. Omit any section whose trigger conditions are not met:

```markdown
# {Machine Name} - Weekly Health Report
**Period:** {start_date} to {end_date}

## Executive Summary
{2-3 sentences comparing to historical context, noting key changes}

## Core Metrics
{Standard rollup table + brief analysis}

[Optional] ## Capacity Runway
[Optional] ## Correlation Insights
[Optional] ## Fleet Context
[Optional] ## Temporal Patterns
[Optional] ## Efficiency

## Health Verdict
{overall status badge + brief justification}
```

**Final output rule:** Save the complete Markdown to `weekly_report.md`. Generate a 2-3 sentence `<summary_state>` of this week's key findings and overwrite `historical_insights.json` with it (same format as before). The summary should be compressed — just the signal, no fluff.
