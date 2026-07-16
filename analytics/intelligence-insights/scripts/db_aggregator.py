import json
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "dbname": "lab_monitoring_db",
    "user": "release_user",
    "password": "release_password",
    "host": "127.0.0.1",
    "port": 5432
}

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

def discover_telemetry_metrics(cursor):
    """
    Queries PostgreSQL system catalogs to introspect the metric_samples table.
    Filters out metadata columns, keeping only valid numeric metric fields.
    """
    # Columns that represent string fields, metadata, identifiers or audit tracking
    METADATA_COLUMNS = {
        'id', 'server_id', 'ts', 'source_mode', 
        'raw_extra', 'status', 'created_at'
    }
    
    query = """
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'public' 
          AND table_name = 'metric_samples';
    """
    cursor.execute(query)
    columns = cursor.fetchall()
    
    metric_fields = []
    for col in columns:
        col_name = col['column_name']
        data_type = col['data_type']
        
        # Keep only numeric fields and skip operational metadata fields
        if col_name not in METADATA_COLUMNS and data_type in ('numeric', 'bigint', 'integer', 'double precision'):
            metric_fields.append(col_name)
            
    return metric_fields

def generate_dynamic_aggregation_data(server_id, lookback_days=7):
    """
    Autodetects existing table columns and constructs a time-bucketed 
    SQL query on the fly, packaging all parameters dynamically for the LLM.
    """
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        # Use RealDictCursor to preserve column mappings cleanly
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Step 1: Introspect the DB Table structure dynamically
        metrics = discover_telemetry_metrics(cursor)
        if not metrics:
            print("Error: No numeric telemetry metrics found in schema.")
            return None
            
        print(f"Dynamic Schema Engine discovered {len(metrics)} active metrics: {metrics}")
        
        # Step 2: On-the-fly SQL construct generation
        # We programmatically build daily average and maximum clauses for each discovered metric
        select_clauses = ["date_trunc('day', ts) AS metric_day"]
        for metric in metrics:
            select_clauses.append(f"ROUND(AVG({metric})::numeric, 2) AS avg_{metric}")
            select_clauses.append(f"ROUND(MAX({metric})::numeric, 2) AS max_{metric}")
            
        sql_query = f"""
            SELECT {', '.join(select_clauses)}
            FROM public.metric_samples
            WHERE server_id = %s
              AND ts >= NOW() - INTERVAL %s
            GROUP BY metric_day
            ORDER BY metric_day ASC;
        """
        
        # Step 3: Extract dataset
        lookback_string = f"{lookback_days} days"
        cursor.execute(sql_query, (server_id, lookback_string))
        rows = cursor.fetchall()
        
        # Step 4: Serialize timestamps into clean string frames for JSON payload stability
        serialized_data = []
        for row in rows:
            row_dict = dict(row)
            if row_dict.get('metric_day'):
                row_dict['metric_day'] = row_dict['metric_day'].strftime('%Y-%m-%d')
            serialized_data.append(row_dict)
            
        return serialized_data
        
    except Exception as e:
        print(f"Database Introspection Error: {e}")
        return None
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

if __name__ == "__main__":
    import sys
    target = "3c33c5e6-d886-46b3-9791-551fd68febc3"
    if len(sys.argv) > 2 and sys.argv[1] == "--machine":
        target = sys.argv[2]
    
    aggregated_payload = generate_dynamic_aggregation_data(target)
    
    if aggregated_payload:
        print("\n--- Summary Payload Prepared For Gemma 4 Ingestion ---")
        print(json.dumps(aggregated_payload, cls=DecimalEncoder, indent=2))
