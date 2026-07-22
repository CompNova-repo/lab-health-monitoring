import pytest
import json
import os
import sys
import time
from unittest.mock import patch, MagicMock
import psycopg2
import psycopg2.extras

# Import the module to test
import p1_fixed

# ==============================================================================
# MOCK DATA (Simulating SSH outputs from remote Linux machines)
# ==============================================================================

MOCK_STATS_OUTPUT = """---RAM---
45.2
---SWAP---
0.0
---DISK---
32
---CPU---
12.5
---LOAD---
0.50 0.60 0.70
---PROCS---
150
---UPTIME---
up 2 days
---UPTIMESEC---
172800
---FAILEDUNITS---
0
"""

MOCK_INVENTORY_OUTPUT = """---HOSTNAME---
test-vm-01
---OSNAME---
Ubuntu
---OSVERSION---
22.04
---CPUMODEL---
Intel Core i7
---CPUCORES---
8
---RAMGB---
16
---DISKTOTALGB---
100
---IPADDR---
10.0.0.5
"""

MOCK_NETWORK_SUMMARY_OUTPUT = """---TOTALCONN---
42
---LISTENPORTS---
22
80
443
---TOPREMOTE---
3 10.0.0.1
2 10.0.0.2
"""

MOCK_LOG_SUMMARY_OUTPUT = """---ERRCOUNT---
5
---WARNCOUNT---
12
---TOPERR---
3 Connection refused
2 Timeout
"""

MOCK_TOP_PROCESSES_OUTPUT = """---TOPCPU---
1234 python3 15.5 2.1 45000
5678 nginx 5.2 1.0 20000
---TOPMEM---
5678 nginx 5.2 10.5 120000
1234 python3 15.5 2.1 45000
"""

def mock_ssh_run(target, remote_cmd, timeout=20):
    """Mock ssh_run to return predefined outputs based on command markers."""
    if "echo '---RAM---'" in remote_cmd: return True, MOCK_STATS_OUTPUT, ""
    if "echo '---HOSTNAME---'" in remote_cmd: return True, MOCK_INVENTORY_OUTPUT, ""
    if "echo '---TOTALCONN---'" in remote_cmd: return True, MOCK_NETWORK_SUMMARY_OUTPUT, ""
    if "echo '---ERRCOUNT---'" in remote_cmd: return True, MOCK_LOG_SUMMARY_OUTPUT, ""
    if "echo '---TOPCPU---'" in remote_cmd: return True, MOCK_TOP_PROCESSES_OUTPUT, ""
    if "systemctl is-active" in remote_cmd: return True, "ssh:active\nnginx:active\n", ""
    if "pgrep -f" in remote_cmd: return True, "NOPIDS", ""
    if "dpkg-query" in remote_cmd or "rpm -q" in remote_cmd: return True, "curl:installed:7.68.0\n", ""
    if "ping -c 3" in remote_cmd: return True, "3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = 1.1/1.2/1.3/0.1 ms", ""
    if "getent hosts" in remote_cmd: return True, "0 15", ""
    if "ip route show default" in remote_cmd: return True, "GW:10.0.0.1\n3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = 1.0/1.1/1.2/0.1 ms", ""
    if "/proc/diskstats" in remote_cmd: return True, "---T0---\n100 200 300 400 500 600\n---T1---\n110 220 330 440 550 660", ""
    if "/proc/net/dev" in remote_cmd: return True, "---T0---\n1000 2000\n---T1---\n1500 2500", ""
    return True, "", ""

# ==============================================================================
# FIXTURES
# ==============================================================================

@pytest.fixture(scope="module")
def db_conn():
    """Connect to the real database for integration tests."""
    conn, err = p1_fixed.db_connect()
    if conn is None:
        pytest.skip(f"Database connection failed: {err}. Ensure DB is running and credentials are correct.")
    
    # Ensure schema exists
    p1_fixed.db_ensure_schema(conn)
    
    yield conn
    
    # Teardown: Clean up test data aggressively
    with conn.cursor() as cur:
        cur.execute("DELETE FROM top_processes WHERE server_id IN (SELECT server_id FROM machines WHERE alias LIKE 'test-%')")
        cur.execute("DELETE FROM network_check_samples WHERE server_id IN (SELECT server_id FROM machines WHERE alias LIKE 'test-%')")
        cur.execute("DELETE FROM service_status WHERE server_id IN (SELECT server_id FROM machines WHERE alias LIKE 'test-%')")
        cur.execute("DELETE FROM metric_samples WHERE server_id IN (SELECT server_id FROM machines WHERE alias LIKE 'test-%')")
        cur.execute("DELETE FROM machines WHERE alias LIKE 'test-%'")
    conn.close()

@pytest.fixture
def mock_ssh():
    with patch('p1_fixed.ssh_run', side_effect=mock_ssh_run):
        yield

@pytest.fixture
def test_machine_config():
    return {
        "thresholds": {"cpu": 90.0, "ram": 80.0, "disk": 90.0},
        "package_checks": ["ssh", "nginx"],
        "apps": [{"name": "myapp", "pattern": "myapp.bin"}],
        "packages": ["curl"],
        "network_checks": [{"target": "8.8.8.8", "type": "ping"}],
        "top_n": 2,
        "disk_device": "sda",
        "network_interface": "eth0"
    }

@pytest.fixture
def test_ssh_target():
    return {"host": "127.0.0.1", "port": "22", "user": "testuser", "ssh_key": "~/.ssh/id_rsa"}

# ==============================================================================
# UNIT TESTS: Parsing & Utilities
# ==============================================================================

class TestParsingFunctions:
    def test_split_sections(self):
        stdout = "---RAM---\n45.2\n---CPU---\n12.5\n"
        sections = p1_fixed.split_sections(stdout, ["RAM", "CPU", "DISK"])
        assert sections["RAM"] == ["45.2"]
        assert sections["CPU"] == ["12.5"]
        assert sections["DISK"] == []

    def test_to_float(self):
        assert p1_fixed.to_float("45.23") == 45.2
        assert p1_fixed.to_float("invalid") is None
        assert p1_fixed.to_float(" 12.0 ") == 12.0

    def test_to_int(self):
        assert p1_fixed.to_int("150") == 150
        assert p1_fixed.to_int("150.9") == 150
        assert p1_fixed.to_int("bad") is None

# ==============================================================================
# INTEGRATION TESTS: Collectors (Mocked SSH)
# ==============================================================================

class TestCollectors:
    def test_collect_stats(self, mock_ssh):
        stats, err = p1_fixed.collect_stats({})
        assert err is None
        assert stats["ram_pct"] == 45.2
        assert stats["cpu_pct"] == 12.5
        assert stats["load_avg_1m"] == 0.5
        assert stats["process_count"] == 150

    def test_collect_inventory(self, mock_ssh):
        inv, err = p1_fixed.collect_inventory({})
        assert err is None
        assert inv["hostname"] == "test-vm-01"
        assert inv["cpu_cores"] == 8
        assert inv["ram_gb"] == 16.0

    def test_collect_network_summary(self, mock_ssh):
        summary, err = p1_fixed.collect_network_summary({})
        assert err is None
        assert summary["total_connections"] == 42
        assert 80 in summary["listening_ports"]

    def test_collect_disk_io(self, mock_ssh):
        io, err = p1_fixed.collect_disk_io({}, "sda", sleep_seconds=3)
        assert err is None
        assert io is not None
        # Delta reads = 10, elapsed = 3s -> 3.33 IOPS
        assert io["disk_read_iops"] == 3.33

# ==============================================================================
# CORE LOGIC & DB INTEGRATION TESTS
# ==============================================================================

class TestEvaluateAndPersist:
    def test_evaluate_machine_and_db_write(self, db_conn, mock_ssh, test_machine_config, test_ssh_target):
        alias = "test-eval-vm-01"
        prior_state = {"install_state": "NORMAL", "breach_counters": {}, "last_log_check_ts": "1 hour ago"}
        
        # 1. Evaluate Machine
        new_state, alerts, db_record = p1_fixed.evaluate_machine(
            alias, test_machine_config, test_ssh_target, prior_state, 
            consecutive_required=2, mode="standard", custom_metrics_cfg={}
        )
        
        assert db_record["status"] == "ok"
        assert db_record["stats"]["ram_pct"] == 45.2
        assert len(db_record["top_processes"][0]) == 2 # top_n = 2
        assert len(alerts) == 0 # CPU is 12.5, threshold is 90.0 -> No alert
        
        # 2. Persist to DB
        db_ok, db_err, failed = p1_fixed.persist_to_db([db_record], {}, mesh_ping_results=None)
        assert db_ok is True, f"DB Write failed: {db_err}"
        
        # 3. Verify DB State
        with db_conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT server_id FROM machines WHERE alias = %s", (alias,))
            row = cur.fetchone()
            assert row is not None, "Machine was not inserted into DB"
            server_id = row["server_id"]
            
            cur.execute("SELECT cpu_pct, ram_pct FROM metric_samples WHERE server_id = %s ORDER BY ts DESC LIMIT 1", (server_id,))
            sample = cur.fetchone()
            assert sample["cpu_pct"] == 12.5
            assert sample["ram_pct"] == 45.2
            
            cur.execute("SELECT service_name, status FROM service_status WHERE server_id = %s", (server_id,))
            services = cur.fetchall()
            svc_dict = {s["service_name"]: s["status"] for s in services}
            assert svc_dict.get("ssh") == "active"

class TestCmdRun:
    @patch('p1_fixed.load_machines_from_db')
    @patch('p1_fixed.load_custom_metrics')
    @patch('p1_fixed.load_state')
    @patch('p1_fixed.persist_to_db')
    def test_cmd_run_standard(self, mock_persist, mock_state, mock_custom, mock_load_machines, mock_ssh, capsys):
        mock_load_machines.return_value = (
            {"test-vm": {"thresholds": {}, "package_checks": [], "apps": [], "packages": [], "network_checks": [], "top_n": 5, "disk_device": None, "network_interface": None}},
            {"test-vm": {"host": "127.0.0.1", "port": "22", "user": "u", "ssh_key": "k"}}
        )
        mock_custom.return_value = {}
        mock_state.return_value = {"test-vm": {"install_state": "NORMAL", "breach_counters": {}}}
        mock_persist.return_value = (True, None, [])
        
        p1_fixed.cmd_run("standard")
        
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        
        assert output["mode"] == "standard"
        assert "test-vm" in output["processed_machines"]
        assert output["db_write_ok"] is True

    @patch('p1_fixed.load_machines_from_db')
    @patch('p1_fixed.load_custom_metrics')
    @patch('p1_fixed.load_state')
    @patch('p1_fixed.evaluate_machine')
    @patch('p1_fixed.collect_mesh_ping_from')
    @patch('p1_fixed.persist_to_db')
    def test_cmd_run_mesh_uses_only_successfully_evaluated_peers(
        self, mock_persist, mock_mesh, mock_eval, mock_state, mock_custom, mock_load_machines, capsys
    ):
        aliases = ["peer-a", "peer-b", "ssh-failed"]
        mock_load_machines.return_value = (
            {
                alias: {
                    "thresholds": {}, "package_checks": [], "apps": [],
                    "packages": [], "network_checks": [], "top_n": 5,
                    "disk_device": None, "network_interface": None,
                }
                for alias in aliases
            },
            {
                "peer-a": {"host": "10.0.0.1", "port": "22", "user": "u", "ssh_key": "k"},
                "peer-b": {"host": "10.0.0.2", "port": "22", "user": "u", "ssh_key": "k"},
                "ssh-failed": {"host": "10.0.0.3", "port": "22", "user": "u", "ssh_key": "k"},
            },
        )
        mock_custom.return_value = {}
        mock_state.return_value = {alias: {"install_state": "NORMAL", "breach_counters": {}} for alias in aliases}

        def eval_side_effect(alias, *args, **kwargs):
            status = "ssh_error" if alias == "ssh-failed" else "ok"
            record = {
                "alias": alias, "ts": "2026-07-21T00:00:00Z", "mode": "standard",
                "status": status, "inventory": {"ip_address": f"10.0.0.{1 if alias == 'peer-a' else 2 if alias == 'peer-b' else 3}"},
                "stats": {}, "custom_metrics": [], "service_status": {}, "app_metrics": [],
                "packages": [], "network_checks": [], "network_summary": None,
                "log_summary": None, "log_window_seconds": None, "top_processes": ([], []),
                "alerts": [], "machine_state": {"install_state": "NORMAL", "breach_counters": {}},
            }
            return record["machine_state"], [], record

        mock_eval.side_effect = eval_side_effect
        mock_mesh.side_effect = lambda _ssh_target, targets: [
            {"target_alias": t["alias"], "target_ip": t["ip"], "success": True, "latency_ms": 1.2}
            for t in targets
        ]
        mock_persist.return_value = (True, None, [])

        p1_fixed.cmd_run("standard")

        _records, _custom_cfg = mock_persist.call_args.args[:2]
        mesh_results = mock_persist.call_args.kwargs["mesh_ping_results"]
        routes = {(r["source_alias"], r["target_alias"]) for r in mesh_results}
        assert routes == {("peer-a", "peer-b"), ("peer-b", "peer-a")}
        assert len(mesh_results) == 2
