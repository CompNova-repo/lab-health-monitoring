import os
import json
import psycopg2
import psycopg2.extras
from datetime import datetime

# ---------------------------------------------------------
# 1. Configuration & Paths
# ---------------------------------------------------------
# Hardcode absolute paths to keep state inside the skill directory
SKILL_DIR = os.path.expanduser("~/.hermes/skills/analytics/intelligence-insights/scripts")
STATE_FILE = os.path.join(SKILL_DIR, "historical_insights.json")
REPORT_FILE = os.path.join(SKILL_DIR, "weekly_report.md")

DB_NAME = "lab_monitoring_db"
DB_USER = "release_user"

# ---------------------------------------------------------
# 2. Database Aggregation (Offloading work from the LLM)
# ---------------------------------------------------------
def get_weekly_metrics():
    """Queries the last 7 days of data and returns a statistical rollup per machine."""
    # Connecting without a host bypasses TCP and forces the local Unix socket
    conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER)
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    query = """
    SELECT 
        m.alias,
        COUNT(s.id) as total_samples,
        ROUND(AVG(s.cpu_pct)::numeric, 2) as avg_cpu,
        MAX(s.cpu_pct) as max_cpu,
        ROUND(AVG(s.ram_pct)::numeric, 2) as avg_ram,
        MAX(s.ram_pct) as max_ram,
        ROUND(MAX(s.disk_pct)::numeric, 2) as max_disk,
        SUM(CASE WHEN s.cpu_pct > 90 THEN 1 ELSE 0 END) as cpu_spikes,
        SUM(CASE WHEN s.ram_pct > 90 THEN 1 ELSE 0 END) as ram_spikes
    FROM metric_samples s
    JOIN machines m ON m.server_id = s.server_id
    WHERE s.ts >= NOW() - INTERVAL '7 days'
    GROUP BY m.alias
    ORDER BY max_cpu DESC;
    """
    
    cursor.execute(query)
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return results

# ---------------------------------------------------------
# 3. State Management
# ---------------------------------------------------------
def load_historical_context():
    """Loads the brief summary of last week's insights."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"previous_insights": "No historical data available. This is the first run."}

def save_historical_context(new_summary):
    """Saves the compressed LLM summary for next week's baseline."""
    with open(STATE_FILE, "w") as f:
        json.dump({"previous_insights": new_summary, "last_run": str(datetime.now())}, f, indent=2)

def write_markdown_report(report_content):
    """Saves the formatted Markdown intended for the Streamlit dashboard."""
    with open(REPORT_FILE, "w") as f:
        f.write(report_content)
    print(f"[SUCCESS] Report written to {REPORT_FILE}")

# ---------------------------------------------------------
# 4. LLM Integration
# ---------------------------------------------------------
def generate_llm_report(current_metrics, historical_context):
    """
    Passes the aggregated SQL data and previous JSON memory to the LLM.
    Replace the stub below with your standard Hermes LLM invocation interface.
    """
    
    prompt = f"""
    You are an expert infrastructure analyst generating a weekly health report for a Streamlit dashboard. 
    
    Historical Context from last week:
    {json.dumps(historical_context, indent=2)}
    
    Current 7-Day Aggregated Metrics:
    {json.dumps(current_metrics, indent=2)}
    
    Your goal is to output a Streamlit-compatible Markdown document containing:
    1. A brief executive summary comparing current health to last week's context.
    2. A neatly formatted Markdown table summarizing the metrics.
    3. Actionable Recommendations (ONLY flag issues if resources consistently spike or disk is nearing exhaustion. Do not suggest scaling if avg CPU is low despite a single max spike).
    4. Provide a JSON-parsable block at the very end wrapped in <summary_state>...</summary_state> tags containing a 2-3 sentence summary of this week's findings. This will be stored for next week.
    
    Do not use complex HTML. Stick to standard Markdown headers (##), bold text, bullet points, and tables.
    """

    # --- HERMES LLM CALL BOUNDARY ---
    # Because Hermes manages LLM context natively, if you are wrapping this in a standard 
    # Python runtime, use the OpenAI/Anthropic client here. Assuming standard Hermes SDK fallback:
    
    import google.generativeai as genai
    # Ensure GEMINI_API_KEY is available in the environment variables
    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-2.5-flash")
    
    response = model.generate_content(prompt)
    output = response.text
    # --------------------------------
    
    return output

# ---------------------------------------------------------
# 5. Execution Pipeline
# ---------------------------------------------------------
def main():
    print("[INFO] Initiating intelligence-insights pipeline...")
    
    print("[INFO] Querying current weekly rollups via local socket...")
    current_data = get_weekly_metrics()
    
    if not current_data:
        print("[WARNING] No data found in the last 7 days. Exiting.")
        return
        
    print("[INFO] Loading historical context...")
    history = load_historical_context()
    
    print("[INFO] Requesting analytical synthesis from LLM...")
    llm_output = generate_llm_report(current_data, history)
    
    # Parse out the state string from the LLM response
    report_body = llm_output
    new_state_summary = "No summary generated."
    
    if "<summary_state>" in llm_output and "</summary_state>" in llm_output:
        parts = llm_output.split("<summary_state>")
        report_body = parts[0].strip()
        new_state_summary = parts[1].split("</summary_state>")[0].strip()

    print("[INFO] Writing artifacts to disk...")
    write_markdown_report(report_body)
    save_historical_context(new_state_summary)
    
    print("[SUCCESS] Weekly intelligence insight run completed successfully.")

if __name__ == "__main__":
    main()
