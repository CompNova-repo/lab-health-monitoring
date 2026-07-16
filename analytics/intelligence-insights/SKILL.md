# Intelligence Insights

## Description
Generates cohesive, weekly Markdown reports of machine health and intelligence insights using native database aggregations.

## Commands

### `/generate-insights`
Triggers the analysis report generation. Can run across the entire database or target a specific machine.

**Usage:**
`/generate-insights` (Runs all machines)
`/generate-insights --machine [alias_or_server_id]` (Runs a specific machine)

**Execution Instructions for Hermes:**
1. Parse the user's command. If a `--machine` flag is provided, execute: 
   `python ~/.hermes/skills/intelligence-insights/scripts/db_aggregator.py --machine "<target>"`
   If no flag is provided, execute without arguments to fetch all machines.
2. Read the resulting JSON output from the terminal.
3. Read the historical context from `~/.hermes/skills/intelligence-insights/scripts/historical_insights.json` (if it exists).
4. Act as an expert infrastructure analyst. For EVERY machine in the JSON array, synthesize the data.
5. Generate a cleanly formatted, Streamlit-compatible Markdown report comparing current health to the historical context. Do not use complex HTML.
6. Save the final compiled Markdown report to `~/.hermes/skills/intelligence-insights/scripts/weekly_report.md`.
7. Generate a new 2-3 sentence summary state of your findings and overwrite `historical_insights.json` with it to serve as next week's baseline.
