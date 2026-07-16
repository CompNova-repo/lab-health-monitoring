import datetime
import random
import uuid
import psycopg2
from psycopg2.extras import execute_values

DB_CONFIG = {"dbname": "lab_monitoring_db", "user": "release_user", "password": "release_password", "host": "127.0.0.1", "port": 5432}

def seed_network_degradation():
    server_id = "3c33c5e6-d886-46b3-9791-551fd68febc3"
    machine_data = (server_id, "mock-network-target-03", "prod-net-03.internal", "192.168.1.107", 22, "Ubuntu", "22.04 LTS", "Intel Xeon", 4, 16.0, 200.0)
    
    end_time = datetime.datetime.now(datetime.timezone.utc)
    start_time = end_time - datetime.timedelta(days=7)
    interval = datetime.timedelta(minutes=5)
    current_time = start_time
    metrics = []

    while current_time < end_time:
        # Simulate bad network flapping segments every few days or hours
        is_flapping = (random.choice([0, 1, 2, 3, 4, 5]) == 3)

        cpu = random.uniform(4.0, 12.0)
        ram = random.uniform(28.0, 31.0)
        swap = 0.0
        cpu_temp = random.uniform(38.0, 42.0)
        
        if is_flapping:
            net_latency = random.uniform(180.0, 350.0) # Dangerous response bounds
            packet_loss = random.uniform(4.0, 12.5)    # Dropping frames
            status = "partial"
        else:
            net_latency = random.uniform(4.0, 8.5)
            packet_loss = 0.0
            status = "ok"

        metrics.append((
            server_id, current_time, "standard", round(cpu, 2), round(ram, 2), round(swap, 2),
            20.0, 1.0, 2.0, 1.0, 45000, 40000, round(net_latency, 2), round(packet_loss, 2),
            round(cpu / 20.0, 2), round(cpu / 20.0, 2), round(cpu / 20.0, 2),
            42, 7200, status, round(cpu_temp, 2)
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
    print(f"Successfully seeded Network Flapping Target under Server ID: {machine[0]}")

if __name__ == "__main__":
    m, d = seed_network_degradation()
    insert_data(m, d)
