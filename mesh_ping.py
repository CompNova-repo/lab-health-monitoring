import asyncio
import asyncpg
import asyncssh
import logging
import argparse
import json
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

# Hardcoded DB defaults matching the skill's environment.
# Override via environment variables or pass explicit db_dsn to run_sync().
_MESH_DB_HOST = os.environ.get("P1_DB_HOST", "127.0.0.1")
_MESH_DB_PORT = os.environ.get("P1_DB_PORT", "5432")
_MESH_DB_NAME = os.environ.get("P1_DB_NAME", "lab_monitoring_db")
_MESH_DB_USER = os.environ.get("P1_DB_USER", "release_user")
_MESH_DB_PASS = os.environ.get("P1_DB_PASSWORD", "release_password")


def _build_dsn() -> str:
    """Construct a PostgreSQL DSN from hardcoded defaults / env vars."""
    return f"postgresql://{_MESH_DB_USER}:{_MESH_DB_PASS}@{_MESH_DB_HOST}:{_MESH_DB_PORT}/{_MESH_DB_NAME}"

# Initialize logger; configuration is deferred to __main__ to respect parent script logging setups
logger = logging.getLogger(__name__)

@dataclass
class Machine:
    server_id: str
    alias: str
    hostname: Optional[str]
    ip_address: str
    ssh_port: int
    os_name: Optional[str]
    ssh_user: Optional[str]
    ssh_key_path: Optional[str]

async def is_reachable(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a machine is reachable via TCP on its SSH port."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False

def build_remote_script(target_ips: List[str], os_name: Optional[str]) -> str:
    """Generate a POSIX-compliant shell script to run parallel pings on the remote machine."""
    target_ips_str = " ".join(target_ips)
    os_name_lower = (os_name or "").lower()
    
    # Tailor the ping command based on the OS to handle timeout flags correctly
    if "linux" in os_name_lower:
        ping_cmd = "ping -c 2 -W 2"
    elif "darwin" in os_name_lower or "mac" in os_name_lower or "freebsd" in os_name_lower:
        ping_cmd = "ping -c 2 -W 2000"
    elif "windows" in os_name_lower:
        ping_cmd = "ping -n 2 -w 2000"
    else:
        ping_cmd = "ping -c 2"

    # Using 'sh' and background jobs (&) for parallel execution on the remote host.
    # Each ping's output is captured so the average RTT can be parsed and returned.
    script = """#!/bin/sh
export LC_ALL=C
parse_rtt() {
    # Linux/Darwin style: rtt min/avg/max/mdev = 0.05/0.06/0.08/0.01 ms
    rtt=$(echo "$1" | awk -F'[/=]' '/min\\/avg\\/max/ {gsub(/ ms/,""); print $5; exit}')
    if [ -z "$rtt" ]; then
        # Windows style: Average = 5ms
        rtt=$(echo "$1" | awk -F'Average = ' '/Average/ {gsub(/ms/,""); print $2; exit}')
    fi
    echo "$rtt"
}
for ip in __TARGET_IPS__; do
    (
        out=$(__PING_CMD__ "$ip" 2>&1)
        rc=$?
        if [ $rc -eq 0 ]; then
            rtt=$(parse_rtt "$out")
            [ -z "$rtt" ] && rtt="NA"
            echo "SUCCESS $ip $rtt"
        else
            echo "FAIL $ip"
        fi
    ) &
done
wait
"""
    script = script.replace("__TARGET_IPS__", target_ips_str)
    script = script.replace("__PING_CMD__", ping_cmd)
    return script

async def run_mesh_ping(machine: Machine, target_machines: List[Machine], sem: asyncio.Semaphore) -> List[Dict[str, Any]]:
    """SSH into a specific machine and execute the mesh ping script."""
    async with sem:
        results = []
        client_keys = [machine.ssh_key_path] if machine.ssh_key_path else None
        
        try:
            logger.debug(f"Connecting to {machine.alias} ({machine.ip_address})...")
            async with asyncssh.connect(
                machine.ip_address,
                port=machine.ssh_port,
                username=machine.ssh_user,
                client_keys=client_keys,
                known_hosts=False,  # Disable strict host key checking for automated monitoring
                connect_timeout=5,
                keepalive_interval=10
            ) as conn:
                
                # Filter out the source machine from the targets
                target_ips = [m.ip_address for m in target_machines if m.server_id != machine.server_id]
                if not target_ips:
                    return results

                script = build_remote_script(target_ips, machine.os_name)
                
                # Execute via 'sh -s' to ensure POSIX compatibility regardless of user's default shell
                proc = await conn.run("sh -s", input=script)
                
                # Map IPs to machine objects for fast O(1) lookup during result parsing
                ip_to_machine = {m.ip_address: m for m in target_machines}
                
                # Parse stdout results
                for line in proc.stdout.strip().split('\n'):
                    if not line:
                        continue
                    parts = line.split()
                    status = parts[0]
                    ip = parts[1] if len(parts) > 1 else None

                    if status == "SUCCESS" and len(parts) >= 3:
                        success = True
                        rtt_raw = parts[2]
                        try:
                            latency_ms = float(rtt_raw)
                        except ValueError:
                            latency_ms = None
                    elif status == "FAIL":
                        success = False
                        latency_ms = None
                    else:
                        logger.warning(f"Unrecognized result line from {machine.alias}: '{line}'")
                        continue

                    target_m = ip_to_machine.get(ip)
                    if target_m:
                        results.append({
                            "source_server_id": str(machine.server_id),
                            "source_alias": machine.alias,
                            "target_server_id": str(target_m.server_id),
                            "target_alias": target_m.alias,
                            "target_ip": ip,
                            "success": success,
                            "latency_ms": latency_ms
                        })
                    else:
                        logger.warning(f"Could not match IP {ip} to a known target machine in DB.")
                            
        except Exception as e:
            logger.error(f"Failed to process {machine.alias} ({machine.ip_address}): {e}")
            
        return results

async def execute_mesh_ping_test(db_dsn: Optional[str] = None, max_concurrent_ssh: int = 20) -> List[Dict[str, Any]]:
    """Main execution function to orchestrate the mesh ping."""
    if db_dsn is None:
        db_dsn = _build_dsn()
    logger.info("Connecting to database...")
    conn = await asyncpg.connect(db_dsn)
    
    logger.info("Fetching monitored machines from DB...")
    rows = await conn.fetch("""
        SELECT 
            server_id::text, 
            alias, 
            hostname, 
            host(ip_address) AS ip_address, 
            ssh_port, 
            os_name, 
            ssh_user, 
            ssh_key_path
        FROM machines
        WHERE monitoring_enabled = true
    """)
    await conn.close()
    
    if not rows:
        logger.warning("No machines found with monitoring_enabled = true.")
        return []
        
    machines = [Machine(**dict(r)) for r in rows]
    logger.info(f"Found {len(machines)} monitored machines. Verifying reachability...")
    
    # Step 1: Pre-check reachability to avoid hanging on dead machines
    reachability_tasks = [is_reachable(m.ip_address, m.ssh_port) for m in machines]
    reachability_results = await asyncio.gather(*reachability_tasks)
    
    reachable_machines = [m for m, reachable in zip(machines, reachability_results) if reachable]
    unreachable_machines = [m for m, reachable in zip(machines, reachability_results) if not reachable]
    
    if unreachable_machines:
        logger.warning(f"{len(unreachable_machines)} machines are unreachable from controller: {[m.alias for m in unreachable_machines]}")
        
    if not reachable_machines:
        logger.error("No reachable machines found. Aborting execution.")
        return []
        
    logger.info(f"{len(reachable_machines)} machines are reachable. Initiating mesh ping...")
    
    # Step 2: Run pings with a concurrency limit to prevent controller/network saturation
    sem = asyncio.Semaphore(max_concurrent_ssh)
    ping_tasks = [run_mesh_ping(m, reachable_machines, sem) for m in reachable_machines]
    
    all_results_nested = await asyncio.gather(*ping_tasks)
    
    # Flatten the list of results
    all_results = [res for sublist in all_results_nested for res in sublist]
    logger.info(f"Mesh ping completed. Total successful checks: {sum(1 for r in all_results if r['success'])}/{len(all_results)}")
    
    return all_results

def run_sync(db_dsn: Optional[str] = None, max_concurrent_ssh: int = 20) -> List[Dict[str, Any]]:
    """Synchronous wrapper for calling from non-async scripts."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        raise RuntimeError(
            "Cannot run synchronously inside an active asyncio event loop. "
            "Please use `await execute_mesh_ping_test(...)` instead."
        )
    return asyncio.run(execute_mesh_ping_test(db_dsn, max_concurrent_ssh))

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    parser = argparse.ArgumentParser(description="Execute N-to-N mesh ping across monitored machines.")
    parser.add_argument(
        "--db-dsn", 
        default=None, 
        help="PostgreSQL DSN (default: build from env vars or hardcoded values)"
    )
    parser.add_argument(
        "--concurrency", 
        type=int, 
        default=20, 
        help="Max concurrent SSH connections (default: 20)"
    )
    
    args = parser.parse_args()
    
    try:
        results = run_sync(args.db_dsn, args.concurrency)
        print("\n--- MESH PING SUMMARY ---")
        print(f"Total Pings Executed: {len(results)}")
        print(json.dumps(results, indent=2))
        
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.exception(f"An unexpected error occurred: {e}")
