import datetime
import random
import uuid
import psycopg2
from psycopg2.extras import execute_values

DB_CONFIG = {"dbname": "lab_monitoring_db", "user": "release_user", "password": "release_password", "host": "127.0.0.1", "port": 5432}

def seed_cron_spikes():
    server_id = "2b22c5e6-d886-46b3-9791-551fd68feba2"
    machine_data = (server_id, "mock-cron-target-02", "prod-cron-02.internal", "192.168.1.106", 22, "Ubuntu", "22.04 LTS", "Intel Xeon", 8, 32.0, 500.0)
    
    end_time = datetime.datetime.now(datetime.timezone.utc)
    start_time = end_time - datetime.timedelta(days=7)
    interval = datetime.timedelta(minutes=5)
    current_time = start_time
    metrics = []

    while current_time < end_time:
        # Check if the time falls within a nightly 1-hour window (e.g., between 02:00 and 03:00 UTC)
        is_backup_window = (2 <= current_time.hour < 3)

        if is_backup_window:
            cpu = random.uniform(92.0, 97.5)
            ram = random.uniform(75.0, 80.0)  # High but bounded
            swap = 0.0
            disk_write = random.uniform(250.0, 400.0)  # Massive write heavy load
            net_latency = random.uniform(15.0, 30.0)   # Latency tax
            cpu_temp = random.uniform(72.0, 78.0)
        else:
            cpu = random.uniform(12.0, 25.0)
            ram = random.uniform(38.0, 42.0)
            swap = 0.0
            disk_write = random.uniform(5.0, 15.0)
            net_latency = random.uniform(1.2, 2.5)
            cpu_temp = random.uniform(44.0, 48.0)

        metrics.append((
            server_id, current_time, "standard", round(cpu, 2), round(ram, 2), round(swap, 2),
            35.0, 10.0, round(disk_write, 2), 2.0, 100000, 100000, round(net_latency, 2),
            0.0, round(cpu / 15.0, 2), round(cpu / 15.0, 2), round(cpu / 15.0, 2),
            85 if not is_backup_window else 140, 3600, "ok", round(cpu_temp, 2)
        ))
        current_time += interval

    return machine_data, metrics

def insert_data(machine, metrics):
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO public.machines VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING;", machine)
    execute_values(cursor, "INSERT INTO public.metric_samples (server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct, disk_pct, disk_read_iops, disk_write_iops, disk_latency_ms, net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms, packet_loss_pct, load_avg_1m, load_avg_5m, load_avg_15m, process_count, uptime_seconds, status, cpu_temp_c) VALUES %s;", metrics)
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Successfully seeded Cron Spikes Target under Server ID: {machine[0]}")

if __name__ == "__main__":
    m, d = seed_cron_spikes()
    insert_data(m, d)
