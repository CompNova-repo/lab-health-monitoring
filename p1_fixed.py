


#!/usr/bin/env python3
"""
p1_fixed.py - deterministic helper for the P1 (monitoring) agent.

Invoked via the `terminal` tool by cron-fired P1 agent runs. Two separate
cron jobs call this with different modes:

  - Standard job (every 5 min):       p1_fixed.py run --mode standard
  - High-frequency job (every 30 sec): p1_fixed.py run --mode highfreq

Both modes now collect the FULL set of monitored data (resource metrics,
services, packages, apps, network checks, network/connection summaries,
log summaries, top processes) and write it ALL directly to PostgreSQL.

New command:
  - collect-apps:  Run app commands registered by headless_add_app.py locally
                   and insert results into app_metric_samples.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import sql
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

ROOT = os.getcwd()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MONITOR_CONFIG = os.path.join(ROOT, "monitor_config.yaml")
SSH_TARGETS = os.path.join(ROOT, "ssh_targets.yaml")
STATE_FILE = os.path.join(ROOT, "system_state.json")

def resolve_custom_metrics_file():
    """
    Prefer an explicit path, then the current working directory, then the script
    directory, then the standard Hermes metric-onboarding skill location.
    This prevents P1 from silently reading the wrong custom_metrics.json when
    it is launched from a different directory.
    """
    candidates = [
        os.environ.get("P1_CUSTOM_METRICS_FILE"),
        os.path.join(ROOT, "custom_metrics.json"),
        os.path.join(SCRIPT_DIR, "custom_metrics.json"),
        os.path.expanduser("~/.hermes/skills/metric-onboarding/custom_metrics.json"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return os.path.join(ROOT, "custom_metrics.json")

CUSTOM_METRICS_FILE = resolve_custom_metrics_file()

METRIC_REGISTRY_FILE = os.path.join(SCRIPT_DIR, "metric_registry.yaml")
APP_COMMANDS_FILE = os.path.join(SCRIPT_DIR, "app_commands.json")

DB_DSN = {
    "host": os.environ.get("P1_DB_HOST", "127.0.0.1"),
    "port": os.environ.get("P1_DB_PORT", "5432"),
    "dbname": os.environ.get("P1_DB_NAME", "lab_monitoring_db"),
    "user": os.environ.get("P1_DB_USER", "release_user"),
    "password": os.environ.get("P1_DB_PASSWORD", "release_password"),
    "connect_timeout": 5,
}

# Metrics: ram/disk/cpu/swap/load-avg/process-count + systemd failed units.
REMOTE_STATS_CMD = (
    "echo '---RAM---'; "
    "free | awk '/Mem:/ {printf \"%.1f\\n\", $3/$2*100}'; "
    "echo '---SWAP---'; "
    "free | awk '/Swap:/ {if ($2 > 0) printf \"%.1f\\n\", $3/$2*100; else print \"0.0\"}'; "
    "echo '---DISK---'; "
    "df -P / | awk 'NR==2 {gsub(\"%\",\"\",$5); print $5}'; "
    "echo '---CPU---'; "
    "top -bn1 | awk '/Cpu\\(s\\)/ {print 100 - $8}'; "
    "echo '---LOAD---'; "
    "cat /proc/loadavg | awk '{print $1, $2, $3}'; "
    "echo '---PROCS---'; "
    "ps -e --no-headers | wc -l; "
    "echo '---UPTIME---'; "
    "uptime -p; "
    "echo '---UPTIMESEC---'; "
    "cat /proc/uptime | awk '{print int($1)}'; "
    "echo '---FAILEDUNITS---'; "
    "systemctl list-units --state=failed --no-legend 2>/dev/null | wc -l"
)

REMOTE_INVENTORY_CMD = (
    "echo '---HOSTNAME---'; "
    "hostname; "
    "echo '---OSNAME---'; "
    "(. /etc/os-release 2>/dev/null && echo \"$NAME\") || uname -s; "
    "echo '---OSVERSION---'; "
    "(. /etc/os-release 2>/dev/null && echo \"$VERSION_ID\") || uname -r; "
    "echo '---CPUMODEL---'; "
    "awk -F': ' '/model name/ {print $2; exit}' /proc/cpuinfo; "
    "echo '---CPUCORES---'; "
    "nproc; "
    "echo '---RAMGB---'; "
    "free -g | awk '/Mem:/ {print $2}'; "
    "echo '---DISKTOTALGB---'; "
    "df -BG -P / | awk 'NR==2 {gsub(\"G\",\"\",$2); print $2}'; "
    "echo '---IPADDR---'; "
    "hostname -I 2>/dev/null | awk '{print $1}'"
)

REMOTE_LOG_SUMMARY_CMD_TMPL = (
    "echo '---ERRCOUNT---'; "
    "journalctl --since '__SINCE__' -p err 2>/dev/null | grep -v '^-- ' | wc -l; "
    "echo '---WARNCOUNT---'; "
    "journalctl --since '__SINCE__' -p warning -p err 2>/dev/null | grep -v '^-- ' | wc -l; "
    "echo '---TOPERR---'; "
    "journalctl --since '__SINCE__' -p err 2>/dev/null | grep -v '^-- ' | "
    "awk '{$1=\"\";$2=\"\";$3=\"\";print}' | sort | uniq -c | sort -rn | head -5"
)

REMOTE_NETWORK_SUMMARY_CMD = (
    "echo '---TOTALCONN---'; "
    "ss -tun state established 2>/dev/null | tail -n +2 | wc -l; "
    "echo '---LISTENPORTS---'; "
    "ss -tlnH 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\\1/' | sort -un; "
    "echo '---TOPREMOTE---'; "
    "ss -tun state established 2>/dev/null | tail -n +2 | "
    "awk '{print $6}' | sed -E 's/:[0-9]+$//' | sort | uniq -c | sort -rn | head -5"
)

REMOTE_TOP_PROCESSES_CMD_TMPL = (
    "echo '---TOPCPU---'; "
    "ps -eo pid,comm,pcpu,pmem,rss --no-headers --sort=-pcpu | head -{n}; "
    "echo '---TOPMEM---'; "
    "ps -eo pid,comm,pcpu,pmem,rss --no-headers --sort=-pmem | head -{n}"
)

REMOTE_DISKSTATS_CMD_TMPL = (
    "echo '---T0---'; "
    "awk '$3==\"__DEVICE__\" {print $4,$8,$13}' /proc/diskstats; "
    "sleep __SLEEP__; "
    "echo '---T1---'; "
    "awk '$3==\"__DEVICE__\" {print $4,$8,$13}' /proc/diskstats"
)

REMOTE_NETDEV_CMD_TMPL = (
    "echo '---T0---'; "
    "awk -F'[: ]+' '$2==\"__IFACE__\" {print $3,$11}' /proc/net/dev; "
    "sleep __SLEEP__; "
    "echo '---T1---'; "
    "awk -F'[: ]+' '$2==\"__IFACE__\" {print $3,$11}' /proc/net/dev"
)

REMOTE_GATEWAY_PING_CMD = (
    "GW=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}'); "
    "if [ -z \"$GW\" ]; then echo 'NOGATEWAY'; else "
    "echo \"GW:$GW\"; ping -c 3 -W 2 \"$GW\" 2>&1; fi"
)

# ---------------------------------------------------------------------------
# Minimal YAML readers
# ---------------------------------------------------------------------------

def load_monitor_config():
    machines = {}
    settings = {"consecutive_threshold_breaches": 2}

    if not os.path.exists(MONITOR_CONFIG):
        return machines, settings

    current_machine = None
    section = None
    pending_item = None
    in_settings = False

    def new_machine_entry():
        return {
            "thresholds": {}, "package_checks": [], "apps": [], "packages": [],
            "network_checks": [], "top_n": 5, "disk_device": None, "network_interface": None,
        }

    with open(MONITOR_CONFIG) as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"): continue

            if stripped == "machines:":
                in_settings = False
                continue
            if stripped == "settings:":
                in_settings = True
                current_machine = None
                continue

            if in_settings:
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    k, v = k.strip(), v.strip()
                    try: settings[k] = int(v)
                    except ValueError:
                        try: settings[k] = float(v)
                        except ValueError: settings[k] = v
                continue

            if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":"):
                current_machine = stripped[:-1].strip()
                machines[current_machine] = new_machine_entry()
                section = None
                pending_item = None
                continue

            if current_machine is None: continue
            m = machines[current_machine]

            if stripped == "thresholds:": section = "thresholds"; pending_item = None; continue
            if stripped == "package_checks:": section = "package_checks"; pending_item = None; continue
            if stripped == "apps:": section = "apps"; pending_item = None; continue
            if stripped == "packages:": section = "packages"; pending_item = None; continue
            if stripped == "network_checks:": section = "network_checks"; pending_item = None; continue
            
            if stripped.startswith("top_n:"):
                section = None
                try: m["top_n"] = int(stripped.split(":", 1)[1].strip())
                except ValueError: pass
                continue

            if stripped.startswith("disk_device:"):
                section = None
                v = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                m["disk_device"] = v or None
                continue

            if stripped.startswith("network_interface:"):
                section = None
                v = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                m["network_interface"] = v or None
                continue

            if section == "thresholds" and ":" in stripped:
                k, v = stripped.split(":", 1)
                try: m["thresholds"][k.strip()] = float(v.strip())
                except ValueError: pass
                continue

            if section == "package_checks" and stripped.startswith("-"):
                m["package_checks"].append(stripped.lstrip("-").strip())
                continue

            if section == "packages" and stripped.startswith("-"):
                m["packages"].append(stripped.lstrip("-").strip())
                continue

            if section == "apps":
                if stripped.startswith("-"):
                    rest = stripped.lstrip("-").strip()
                    pending_item = {}
                    m["apps"].append(pending_item)
                    if ":" in rest:
                        k, v = rest.split(":", 1)
                        pending_item[k.strip()] = v.strip().strip('"').strip("'")
                elif ":" in stripped and pending_item is not None:
                    k, v = stripped.split(":", 1)
                    pending_item[k.strip()] = v.strip().strip('"').strip("'")
                continue

            if section == "network_checks":
                if stripped.startswith("-"):
                    rest = stripped.lstrip("-").strip()
                    pending_item = {}
                    m["network_checks"].append(pending_item)
                    if ":" in rest:
                        k, v = rest.split(":", 1)
                        pending_item[k.strip()] = v.strip().strip('"').strip("'")
                elif ":" in stripped and pending_item is not None:
                    k, v = stripped.split(":", 1)
                    pending_item[k.strip()] = v.strip().strip('"').strip("'")
                continue

    return machines, settings

def load_ssh_targets():
    machines = {}
    if not os.path.exists(SSH_TARGETS): return machines

    current_machine = None
    with open(SSH_TARGETS) as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"): continue
            if stripped == "machines:": continue
            if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":"):
                current_machine = stripped[:-1].strip()
                machines[current_machine] = {}
                continue
            if current_machine is not None and ":" in stripped:
                k, v = stripped.split(":", 1)
                machines[current_machine][k.strip()] = v.strip()

    return machines

def load_custom_metrics():
    # Priority: 1) env var, 2) hardcoded path alongside this script, 3) legacy custom_metrics.json
    registry_file = os.environ.get("P1_METRIC_REGISTRY_FILE")
    if not registry_file or not os.path.exists(registry_file):
        if os.path.exists(METRIC_REGISTRY_FILE):
            registry_file = METRIC_REGISTRY_FILE
    if registry_file and os.path.exists(registry_file):
        try:
            import yaml
            with open(registry_file) as f:
                raw = yaml.safe_load(f) or {}
            metrics = raw.get("metrics", {}) or {}
            return {k: v for k, v in metrics.items() if isinstance(v, dict) and v.get("enabled", True)}
        except Exception as e:
            sys.stderr.write(f"[p1_helper] Failed to load metric registry: {e}\n")
            return {}

    if not os.path.exists(CUSTOM_METRICS_FILE):
        return {}
    try:
        with open(CUSTOM_METRICS_FILE) as f:
            raw = json.load(f)
        norm = {}
        for k, v in raw.items():
            col = re.sub(r'[^a-z0-9_]', '', k.lower().replace(" ", "_").replace("-", "_"))
            if not col or not col[0].isalpha():
                col = "metric_" + col
            col = col[:63]
            norm[col] = v
        return norm
    except Exception:
        return {}

def load_state():
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}

# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_run(target, remote_cmd, timeout=20):
    key = os.path.expanduser(target.get("ssh_key", "~/.ssh/hermes_patch_test"))
    host = target.get("host", "localhost")
    port = target.get("port", "22")
    user = target.get("user", "patchuser")

    cmd = [
        "ssh", "-i", key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{user}@{host}", remote_cmd,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired: return False, "", "ssh timeout"
    except Exception as e: return False, "", str(e)

def split_sections(stdout, names):
    sections = {n: [] for n in names}
    current = None
    marker_re = re.compile(r"^---([A-Z0-9]+)---$")
    for line in stdout.splitlines():
        s = line.strip()
        m = marker_re.match(s)
        if m and m.group(1) in sections:
            current = m.group(1)
            continue
        if current: sections[current].append(line.rstrip())
    return sections

def to_float(s, default=None):
    if s is None: return default
    s = s.strip()
    try: return round(float(s), 1)
    except (ValueError, TypeError): return default

def to_int(s, default=None):
    if s is None: return default
    s = s.strip()
    try: return int(float(s))
    except (ValueError, TypeError): return default

# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_stats(target):
    ok, out, err = ssh_run(target, REMOTE_STATS_CMD)
    if not ok: return None, err

    sec = split_sections(out, ["RAM", "SWAP", "DISK", "CPU", "LOAD", "PROCS", "UPTIME", "UPTIMESEC", "FAILEDUNITS"])

    load_line = sec["LOAD"][0].split() if sec["LOAD"] else []
    load_avg_1m = to_float(load_line[0]) if len(load_line) > 0 else None
    load_avg_5m = to_float(load_line[1]) if len(load_line) > 1 else None
    load_avg_15m = to_float(load_line[2]) if len(load_line) > 2 else None

    stats = {
        "ram_pct": to_float(sec["RAM"][0]) if sec["RAM"] else None,
        "swap_pct": to_float(sec["SWAP"][0]) if sec["SWAP"] else None,
        "disk_pct": to_float(sec["DISK"][0]) if sec["DISK"] else None,
        "cpu_pct": to_float(sec["CPU"][0]) if sec["CPU"] else None,
        "load_avg_1m": load_avg_1m, "load_avg_5m": load_avg_5m, "load_avg_15m": load_avg_15m,
        "process_count": to_int(sec["PROCS"][0]) if sec["PROCS"] else None,
        "uptime": sec["UPTIME"][0].strip() if sec["UPTIME"] else None,
        "uptime_seconds": to_int(sec["UPTIMESEC"][0]) if sec["UPTIMESEC"] else None,
        "systemd_failed_units_count": to_int(sec["FAILEDUNITS"][0]) if sec["FAILEDUNITS"] else 0,
    }
    return stats, None

def collect_custom_metrics(target, custom_metrics_cfg):
    results = []
    for metric_key, cfg in custom_metrics_cfg.items():
        if isinstance(cfg, str):
            cmd = cfg
        elif isinstance(cfg, dict):
            cmd = cfg.get("command")
        else:
            continue

        if not cmd: continue

        ok, out, err = ssh_run(target, cmd, timeout=15)
        value = None
        if ok and out:
            match = re.search(r"[-+]?\d*\.\d+|\d+", out.strip())
            if match:
                value = float(match.group())
        
        results.append({
            "metric_key": metric_key, "value": value,
            "raw_output": (out or "").strip()[:500], "status": "ok" if ok else "error"
        })
    return results

def collect_inventory(target):
    ok, out, err = ssh_run(target, REMOTE_INVENTORY_CMD)
    if not ok: return None, err

    sec = split_sections(out, ["HOSTNAME", "OSNAME", "OSVERSION", "CPUMODEL", "CPUCORES", "RAMGB", "DISKTOTALGB", "IPADDR"])
    def first(name):
        return sec[name][0].strip() if sec[name] and sec[name][0].strip() else None

    return {
        "hostname": first("HOSTNAME"), "os_name": first("OSNAME"), "os_version": first("OSVERSION"),
        "cpu_model": first("CPUMODEL"), "cpu_cores": to_int(first("CPUCORES")),
        "ram_gb": to_float(first("RAMGB")), "disk_total_gb": to_float(first("DISKTOTALGB")),
        "ip_address": first("IPADDR"),
    }, None

def check_services(target, services):
    if not services: return {}
    joined = " ".join(services)
    remote_cmd = "for s in " + joined + "; do echo \"$s:$(systemctl is-active $s 2>/dev/null || echo unknown)\"; done"
    ok, out, err = ssh_run(target, remote_cmd)
    result = {}
    if not ok:
        for s in services: result[s] = "error"
        return result
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            name, status = line.split(":", 1)
            result[name.strip()] = status.strip()
    for s in services: result.setdefault(s, "unknown")
    return result

def collect_app_metrics(target, apps):
    results = []
    for app in apps:
        name = app.get("name"); pattern = app.get("pattern")
        if not name or not pattern: continue
        import base64
        encoded = base64.b64encode(pattern.encode()).decode()
        remote_cmd = (
            f"PAT=$(echo '{encoded}' | base64 -d); "
            f"PIDS=$(pgrep -f \"$PAT\" | grep -v -E \"^($$|$PPID)$\"); "
            "if [ -z \"$PIDS\" ]; then echo 'NOPIDS'; else "
            "echo \"$PIDS\" | tr '\\n' ' '; echo; "
            "ps -o pcpu=,rss=,pmem= -p $(echo \"$PIDS\" | tr '\\n' ',' | sed 's/,$//'); "
            "for p in $PIDS; do grep '^Threads:' /proc/$p/status 2>/dev/null | awk '{print $2}'; done; "
            "fi"
        )
        ok, out, err = ssh_run(target, remote_cmd)
        if not ok or not out.strip() or out.strip() == "NOPIDS":
            results.append({
                "app_name": name, "cpu_pct": None, "rss_memory_mb": None,
                "process_count": 0, "thread_count": None, "listening_sockets": None,
                "status": "not_installed" if ok else "error",
            })
            continue

        lines = [l for l in out.splitlines() if l.strip()]
        pid_line = lines[0] if lines else ""
        pids = pid_line.split()
        proc_lines = lines[1:1 + len(pids)] if len(pids) else []

        total_cpu = 0.0; total_rss_kb = 0.0
        for pl in proc_lines:
            parts = pl.split()
            if len(parts) >= 2:
                total_cpu += to_float(parts[0], 0.0) or 0.0
                total_rss_kb += to_float(parts[1], 0.0) or 0.0

        thread_lines = lines[1 + len(proc_lines):]
        thread_total = 0
        for tl in thread_lines:
            v = to_int(tl)
            if v is not None: thread_total += v

        results.append({
            "app_name": name, "cpu_pct": round(total_cpu, 2),
            "rss_memory_mb": round(total_rss_kb / 1024.0, 2) if total_rss_kb else 0.0,
            "process_count": len(pids), "thread_count": thread_total or None,
            "listening_sockets": None, "status": "running",
        })
    return results

def detect_pkg_manager(target):
    ok, out, _ = ssh_run(target, "command -v dpkg-query >/dev/null 2>&1 && echo apt || (command -v rpm >/dev/null 2>&1 && echo rpm || echo none)")
    if not ok: return None
    out = out.strip()
    return out if out in ("apt", "rpm") else None

def collect_package_state(target, packages):
    if not packages: return []
    mgr = detect_pkg_manager(target)
    results = []
    if mgr == "apt":
        joined = " ".join(packages)
        remote_cmd = f"for p in {joined}; do v=$(dpkg-query -W -f='${{Version}}' \"$p\" 2>/dev/null); if [ -n \"$v\" ]; then echo \"$p:installed:$v\"; else echo \"$p:notinstalled:\"; fi; done"
    elif mgr == "rpm":
        joined = " ".join(packages)
        remote_cmd = f"for p in {joined}; do v=$(rpm -q --qf '%{{VERSION}}-%{{RELEASE}}' \"$p\" 2>/dev/null); if [ $? -eq 0 ] && [ -n \"$v\" ]; then echo \"$p:installed:$v\"; else echo \"$p:notinstalled:\"; fi; done"
    else:
        for p in packages: results.append({"package_name": p, "is_installed": False, "version": None})
        return results

    ok, out, err = ssh_run(target, remote_cmd)
    if not ok:
        for p in packages: results.append({"package_name": p, "is_installed": False, "version": None})
        return results

    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or ":" not in line: continue
        parts = line.split(":", 2)
        if len(parts) < 2: continue
        pname, state = parts[0], parts[1]
        version = parts[2] if len(parts) > 2 else None
        results.append({"package_name": pname, "is_installed": state == "installed", "version": version if version else None})
        seen.add(pname)

    for p in packages:
        if p not in seen: results.append({"package_name": p, "is_installed": False, "version": None})
    return results

def collect_network_checks(target, checks):
    results = []
    for chk in checks:
        tgt = chk.get("target"); ctype = chk.get("type", "ping").strip().lower()
        if not tgt: continue

        if ctype == "ping":
            remote_cmd = f"ping -c 3 -W 2 {tgt} 2>&1"
            ok, out, err = ssh_run(target, remote_cmd, timeout=15)
            latency_ms = packet_loss = None; status = "error"; error_message = None

            loss_m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out) if out else None
            rtt_m = re.search(r"= [\d.]+/([\d.]+)/", out) if out else None

            if loss_m:
                packet_loss = to_float(loss_m.group(1))
                if rtt_m: latency_ms = to_float(rtt_m.group(1))
                status = "timeout" if packet_loss is not None and packet_loss >= 100 else "ok"
            elif ok:
                error_message = (out or "").strip()[:300] or "ping output not in expected format"
            else:
                status = "timeout" if re.search(r"timed?\s*out", (err or out or ""), re.IGNORECASE) else "error"
                error_message = (err or out or "ssh/ping failed with no output").strip()[:300]

            results.append({"target": tgt, "check_type": "ping", "latency_ms": latency_ms, "packet_loss_pct": packet_loss, "status": status, "error_message": error_message})

        elif ctype == "dns":
            remote_cmd = f"start=$(date +%s%N); getent hosts {tgt} >/dev/null 2>&1; rc=$?; end=$(date +%s%N); echo \"$rc $(( (end - start) / 1000000 ))\""
            ok, out, err = ssh_run(target, remote_cmd, timeout=10)
            status = "error"; latency_ms = None; error_message = None
            if ok and out.strip():
                parts = out.strip().split()
                rc = to_int(parts[0]) if parts else None
                latency_ms = to_float(parts[1]) if len(parts) > 1 else None
                status = "ok" if rc == 0 else "nxdomain"
                if rc != 0: error_message = f"DNS resolution failed for {tgt}"
            else: error_message = (err or "command failed").strip()[:300]

            results.append({"target": tgt, "check_type": "dns", "latency_ms": latency_ms, "packet_loss_pct": None, "status": status, "error_message": error_message})
        else:
            results.append({"target": tgt, "check_type": "ping", "latency_ms": None, "packet_loss_pct": None, "status": "error", "error_message": f"unrecognized check type '{ctype}'"})
    return results

def collect_network_summary(target):
    ok, out, err = ssh_run(target, REMOTE_NETWORK_SUMMARY_CMD)
    if not ok: return None, err

    sec = split_sections(out, ["TOTALCONN", "LISTENPORTS", "TOPREMOTE"])
    total_conn = to_int(sec["TOTALCONN"][0]) if sec["TOTALCONN"] else None

    ports = [to_int(line) for line in sec["LISTENPORTS"] if to_int(line) is not None]

    top_remote = []
    for line in sec["TOPREMOTE"]:
        parts = line.strip().split()
        if len(parts) == 2:
            count, ip = to_int(parts[0]), parts[1]
            if count is not None and ip: top_remote.append({"ip": ip, "count": count})

    return {"total_connections": total_conn, "listening_ports": sorted(set(ports)), "top_remote_ips": top_remote}, None

def collect_log_summary(target, since_ts):
    since = since_ts if since_ts else "1 hour ago"
    remote_cmd = REMOTE_LOG_SUMMARY_CMD_TMPL.replace("__SINCE__", since)
    ok, out, err = ssh_run(target, remote_cmd, timeout=25)
    if not ok: return None, err

    sec = split_sections(out, ["ERRCOUNT", "WARNCOUNT", "TOPERR"])
    err_count = to_int(sec["ERRCOUNT"][0]) if sec["ERRCOUNT"] else 0
    warn_count = to_int(sec["WARNCOUNT"][0]) if sec["WARNCOUNT"] else 0

    top_errors = []
    for line in sec["TOPERR"]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            count, msg = to_int(parts[0]), parts[1].strip()
            if count is not None and msg: top_errors.append({"msg": msg[:300], "count": count})

    return {"error_count": err_count or 0, "warning_count": warn_count or 0, "top_errors": top_errors}, None

def collect_top_processes(target, n):
    remote_cmd = REMOTE_TOP_PROCESSES_CMD_TMPL.format(n=n)
    ok, out, err = ssh_run(target, remote_cmd, timeout=15)
    if not ok: return [], []

    sec = split_sections(out, ["TOPCPU", "TOPMEM"])
    def parse_block(lines):
        rows = []
        for i, line in enumerate(lines):
            parts = line.split(None, 4)
            if len(parts) < 5: continue
            pid, comm, pcpu, pmem, rss_kb = parts
            rows.append({
                "rank_position": i + 1, "pid": to_int(pid), "process_name": comm,
                "cpu_pct": to_float(pcpu), "mem_pct": to_float(pmem),
                "mem_mb": round((to_float(rss_kb) or 0) / 1024.0, 2),
            })
        return rows
    return parse_block(sec["TOPCPU"]), parse_block(sec["TOPMEM"])

def collect_disk_io(target, device, sleep_seconds=3):
    if not device: return None, "no disk_device configured"
    remote_cmd = REMOTE_DISKSTATS_CMD_TMPL.replace("__DEVICE__", device).replace("__SLEEP__", str(sleep_seconds))
    ok, out, err = ssh_run(target, remote_cmd, timeout=sleep_seconds + 15)
    if not ok: return None, err

    sec = split_sections(out, ["T0", "T1"])
    if not sec["T0"] or not sec["T1"]: return None, f"device '{device}' not found in /proc/diskstats"

    def parse_sample(line):
        parts = line.split()
        if len(parts) < 3: return None
        return {"reads": to_int(parts[0]), "writes": to_int(parts[1]), "ms_doing_io": to_int(parts[2])}

    t0 = parse_sample(sec["T0"][0]); t1 = parse_sample(sec["T1"][0])
    if t0 is None or t1 is None: return None, f"could not parse /proc/diskstats line for device '{device}'"

    elapsed = max(sleep_seconds, 1)
    d_reads = max(t1["reads"] - t0["reads"], 0); d_writes = max(t1["writes"] - t0["writes"], 0); d_io_ms = max(t1["ms_doing_io"] - t0["ms_doing_io"], 0)

    read_iops = round(d_reads / elapsed, 2); write_iops = round(d_writes / elapsed, 2)
    total_ops = d_reads + d_writes
    latency_ms = round(d_io_ms / total_ops, 2) if total_ops > 0 else 0.0

    return {"disk_read_iops": read_iops, "disk_write_iops": write_iops, "disk_latency_ms": latency_ms}, None

def collect_network_throughput(target, iface, sleep_seconds=3):
    if not iface: return None, "no network_interface configured"
    remote_cmd = REMOTE_NETDEV_CMD_TMPL.replace("__IFACE__", iface).replace("__SLEEP__", str(sleep_seconds))
    ok, out, err = ssh_run(target, remote_cmd, timeout=sleep_seconds + 15)
    if not ok: return None, err

    sec = split_sections(out, ["T0", "T1"])
    if not sec["T0"] or not sec["T1"]: return None, f"interface '{iface}' not found in /proc/net/dev"

    def parse_sample(line):
        parts = line.split()
        if len(parts) < 2: return None
        return {"rx_bytes": to_int(parts[0]), "tx_bytes": to_int(parts[1])}

    t0 = parse_sample(sec["T0"][0]); t1 = parse_sample(sec["T1"][0])
    if t0 is None or t1 is None: return None, f"could not parse /proc/net/dev line for interface '{iface}'"

    elapsed = max(sleep_seconds, 1)
    d_rx = max(t1["rx_bytes"] - t0["rx_bytes"], 0); d_tx = max(t1["tx_bytes"] - t0["tx_bytes"], 0)

    return {"net_rx_bytes_sec": int(round(d_rx / elapsed)), "net_tx_bytes_sec": int(round(d_tx / elapsed))}, None

def collect_gateway_ping(target, fallback_target=None):
    ok, out, err = ssh_run(target, REMOTE_GATEWAY_PING_CMD, timeout=15)
    if not ok and not out: return None, err or "ssh failed"

    # Check if gateway exists but ping failed (100% loss) or no gateway
    loss_m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    has_gateway = not out.strip().startswith("NOGATEWAY")
    gateway_unreachable = has_gateway and loss_m and to_float(loss_m.group(1)) >= 100

    if out.strip().startswith("NOGATEWAY") or gateway_unreachable:
        if not fallback_target:
            fallback_target = "8.8.8.8"
        sys.stderr.write(f"[p1_helper] gateway unreachable ({'no gateway' if not has_gateway else '100% loss'}), using fallback ping to {fallback_target}\n")
        fb_cmd = f"ping -c 3 -W 2 {fallback_target} 2>&1"
        ok2, out2, err2 = ssh_run(target, fb_cmd, timeout=15)
        if not ok2 and not out2: return None, err2 or "ssh failed"
        out = out2

    loss_m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    rtt_m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
    if not rtt_m:
        rtt_m = re.search(r"= [\d.]+/([\d.]+)/", out)
    if not loss_m:
        sys.stderr.write(f"[p1_helper] gateway ping parse failed, output: {out[:200]}\n")
        return None, (out or err or "gateway ping produced no parseable output").strip()[:300]

    latency = to_float(rtt_m.group(1)) if rtt_m else None
    loss = to_float(loss_m.group(1))
    sys.stderr.write(f"[p1_helper] gateway ping: latency={latency}ms loss={loss}%\n")
    return {"net_latency_ms": latency, "packet_loss_pct": loss}, None

def collect_mesh_ping_from(target, targets_to_ping):
    results = []
    for entry in targets_to_ping:
        tgt_ip = entry["ip"]
        remote_cmd = f"ping -c 3 -W 2 {tgt_ip} 2>&1"
        ok, out, err = ssh_run(target, remote_cmd, timeout=15)

        loss_m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out) if out else None
        rtt_m = re.search(r"= [\d.]+/([\d.]+)/", out) if out else None

        if loss_m:
            packet_loss = to_float(loss_m.group(1))
            latency_ms = to_float(rtt_m.group(1)) if rtt_m else None
            success = packet_loss is not None and packet_loss < 100
        else:
            success = False
            latency_ms = None

        results.append({
            "target_alias": entry["alias"],
            "target_ip": tgt_ip,
            "success": success,
            "latency_ms": latency_ms,
        })
    return results

# ---------------------------------------------------------------------------
# Postgres writes
# ---------------------------------------------------------------------------

def db_connect():
    if not PSYCOPG2_AVAILABLE: return None, "psycopg2 not installed"
    try:
        conn = psycopg2.connect(**DB_DSN)
        conn.autocommit = True
        return conn, None
    except Exception as e: return None, str(e)

def db_ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS custom_metric_samples (
                id SERIAL PRIMARY KEY,
                server_id UUID NOT NULL,
                ts TIMESTAMPTZ NOT NULL,
                metric_key TEXT NOT NULL,
                value DOUBLE PRECISION,
                raw_output TEXT,
                status TEXT DEFAULT 'ok'
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS metric_registry (
                metric_key TEXT PRIMARY KEY,
                display_name TEXT,
                column_name TEXT,
                unit TEXT,
                chart_group TEXT DEFAULT 'Custom',
                preferred_viz TEXT DEFAULT 'line',
                threshold_warning DOUBLE PRECISION,
                threshold_critical DOUBLE PRECISION,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
def _get_command_text(cfg):
    """Extract the command text from a custom_metrics_cfg entry."""
    if isinstance(cfg, str):
        return cfg
    elif isinstance(cfg, dict):
        return cfg.get("command", "")
    return ""

def db_sync_metric_registry(conn, custom_metrics_cfg):
    with conn.cursor() as cur:
        for metric_key, cfg in custom_metrics_cfg.items():
            if isinstance(cfg, str):
                display_name = metric_key
                unit = ""
            else:
                display_name = cfg.get("display_name", metric_key)
                unit = cfg.get("unit", "")
            
            command_text = _get_command_text(cfg)
            
            cur.execute(
                """
                INSERT INTO metric_registry
                    (metric_key, display_name, column_name, unit, chart_group, preferred_viz, enabled, command_strategy, command_text)
                VALUES (%s, %s, %s, %s, 'Custom', 'line', true, 'first_float', %s)
                ON CONFLICT (metric_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    column_name = EXCLUDED.column_name,
                    unit = EXCLUDED.unit,
                    enabled = true,
                    command_strategy = EXCLUDED.command_strategy,
                    command_text = EXCLUDED.command_text
                """,
                (metric_key, display_name, metric_key, unit, command_text)
            )

def db_ensure_machine(conn, alias, inventory):
    inv = inventory or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO machines (alias, hostname, ip_address, os_name, os_version, cpu_model, cpu_cores, ram_gb, disk_total_gb)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alias) DO UPDATE SET
                hostname = COALESCE(EXCLUDED.hostname, machines.hostname),
                ip_address = machines.ip_address,
                os_name = COALESCE(EXCLUDED.os_name, machines.os_name),
                os_version = COALESCE(EXCLUDED.os_version, machines.os_version),
                cpu_model = COALESCE(EXCLUDED.cpu_model, machines.cpu_model),
                cpu_cores = COALESCE(EXCLUDED.cpu_cores, machines.cpu_cores),
                ram_gb = COALESCE(EXCLUDED.ram_gb, machines.ram_gb),
                disk_total_gb = COALESCE(EXCLUDED.disk_total_gb, machines.disk_total_gb),
                updated_at = now()
            RETURNING server_id
            """,
            (alias, inv.get("hostname"), inv.get("ip_address"), inv.get("os_name"), inv.get("os_version"), inv.get("cpu_model"), inv.get("cpu_cores"), inv.get("ram_gb"), inv.get("disk_total_gb")),
        )
        return cur.fetchone()[0]

def db_write_machine_state(conn, server_id, machine_state):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO machine_state (server_id, install_state, installing_since, breach_counters, last_checked, last_ssh_error, last_ssh_error_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (server_id) DO UPDATE SET
                install_state = EXCLUDED.install_state,
                installing_since = EXCLUDED.installing_since,
                breach_counters = EXCLUDED.breach_counters,
                last_checked = EXCLUDED.last_checked,
                last_ssh_error = EXCLUDED.last_ssh_error,
                last_ssh_error_at = CASE WHEN EXCLUDED.last_ssh_error IS NOT NULL THEN now() ELSE machine_state.last_ssh_error_at END,
                updated_at = now()
            """,
            (server_id, machine_state.get("install_state", "NORMAL"), machine_state.get("installing_since"), json.dumps(machine_state.get("breach_counters", {})), machine_state.get("last_checked"), machine_state.get("last_ssh_error"), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if machine_state.get("last_ssh_error") else None),
        )

def db_write_metric_sample(conn, server_id, ts, mode, stats, status):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO metric_samples (server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct, disk_pct, disk_read_iops, disk_write_iops, disk_latency_ms, net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms, packet_loss_pct, load_avg_1m, load_avg_5m, load_avg_15m, process_count, uptime_seconds, systemd_failed_units_count, raw_extra, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (server_id, ts, mode, stats.get("cpu_pct"), stats.get("ram_pct"), stats.get("swap_pct"), stats.get("disk_pct"), stats.get("disk_read_iops"), stats.get("disk_write_iops"), stats.get("disk_latency_ms"), stats.get("net_rx_bytes_sec"), stats.get("net_tx_bytes_sec"), stats.get("net_latency_ms"), stats.get("packet_loss_pct"), stats.get("load_avg_1m"), stats.get("load_avg_5m"), stats.get("load_avg_15m"), stats.get("process_count"), stats.get("uptime_seconds"), stats.get("systemd_failed_units_count", 0), json.dumps({"uptime_text": stats.get("uptime")}), status),
        )
        return cur.fetchone()[0]

def db_write_custom_metrics(conn, server_id, ts, custom_metrics):
    if not custom_metrics: return
    with conn.cursor() as cur:
        for m in custom_metrics:
            cur.execute("INSERT INTO custom_metric_samples (server_id, ts, metric_key, value, raw_output, status) VALUES (%s, %s, %s, %s, %s, %s)", (server_id, ts, m["metric_key"], m.get("value"), m.get("raw_output"), m.get("status")))


def is_safe_metric_column(name):
    return bool(re.match(r"^[a-z][a-z0-9_]{0,62}$", name or ""))

def db_ensure_custom_metric_columns(conn, custom_metrics_cfg):
    if not custom_metrics_cfg:
        return
    with conn.cursor() as cur:
        for metric_key, cfg in custom_metrics_cfg.items():
            column = cfg.get("column_name", metric_key) if isinstance(cfg, dict) else metric_key
            db_type = cfg.get("db_type", "DOUBLE PRECISION") if isinstance(cfg, dict) else "DOUBLE PRECISION"
            if not is_safe_metric_column(column):
                raise ValueError(f"unsafe metric column: {column}")
            cur.execute(
                sql.SQL("ALTER TABLE metric_samples ADD COLUMN IF NOT EXISTS {} {}")
                .format(sql.Identifier(column), sql.SQL(db_type))
            )


def db_write_custom_metric_columns(conn, sample_id, custom_metrics):
    """
    Write custom metric values into the matching dynamic columns on metric_samples.
    Example: metric_key=cpu_iowait_pct updates metric_samples.cpu_iowait_pct.
    """
    if sample_id is None or not custom_metrics:
        return
    with conn.cursor() as cur:
        for m in custom_metrics:
            metric_key = m.get("metric_key")
            value = m.get("value")
            if value is None or not is_safe_metric_column(metric_key):
                continue
            cur.execute(
                sql.SQL("UPDATE metric_samples SET {} = %s WHERE id = %s")
                .format(sql.Identifier(metric_key)),
                (value, sample_id),
            )

def db_write_service_status(conn, server_id, service_status):
    if not service_status: return
    with conn.cursor() as cur:
        for svc, status in service_status.items():
            cur.execute("INSERT INTO service_status (server_id, service_name, status, last_changed_at, last_checked_at) VALUES (%s, %s, %s, now(), now()) ON CONFLICT (server_id, service_name) DO UPDATE SET last_changed_at = CASE WHEN service_status.status <> EXCLUDED.status THEN now() ELSE service_status.last_changed_at END, status = EXCLUDED.status, last_checked_at = now()", (server_id, svc, status))

def db_write_events(conn, server_id, alerts):
    if not alerts: return
    with conn.cursor() as cur:
        for a in alerts:
            severity = "warning" if a.get("consecutive_breaches") else "critical"
            cur.execute("INSERT INTO events (server_id, event_type, severity, metric, value, threshold, consecutive_breaches, message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (server_id, "threshold_breach", severity, a.get("metric"), a.get("value") if isinstance(a.get("value"), (int, float)) else None, a.get("threshold") if isinstance(a.get("threshold"), (int, float)) else None, a.get("consecutive_breaches"), a.get("message")))

def db_write_app_metrics(conn, server_id, ts, app_metrics):
    if not app_metrics: return
    with conn.cursor() as cur:
        for m in app_metrics:
            cur.execute("INSERT INTO app_metric_samples (server_id, ts, app_name, cpu_pct, rss_memory_mb, process_count, thread_count, listening_sockets, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (server_id, ts, m["app_name"], m.get("cpu_pct"), m.get("rss_memory_mb"), m.get("process_count"), m.get("thread_count"), m.get("listening_sockets"), m.get("status", "running")))

def db_write_package_state(conn, server_id, packages):
    if not packages: return
    with conn.cursor() as cur:
        for p in packages:
            cur.execute("INSERT INTO package_state (server_id, package_name, is_installed, version, last_checked_at) VALUES (%s, %s, %s, %s, now()) ON CONFLICT (server_id, package_name) DO UPDATE SET is_installed = EXCLUDED.is_installed, version = EXCLUDED.version, last_checked_at = now()", (server_id, p["package_name"], p["is_installed"], p.get("version")))

def db_write_network_checks(conn, server_id, ts, checks):
    if not checks: return
    with conn.cursor() as cur:
        for c in checks:
            cur.execute("INSERT INTO network_check_samples (server_id, ts, target, check_type, latency_ms, packet_loss_pct, status, error_message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (server_id, ts, c["target"], c["check_type"], c.get("latency_ms"), c.get("packet_loss_pct"), c.get("status", "error"), c.get("error_message")))

def db_get_prior_network_summary(conn, server_id):
    with conn.cursor() as cur:
        cur.execute("SELECT total_connections FROM network_summaries WHERE server_id = %s ORDER BY ts DESC LIMIT 1", (server_id,))
        row = cur.fetchone()
        return row[0] if row else None

def db_write_network_summary(conn, server_id, ts, summary):
    if not summary: return
    prior_total = db_get_prior_network_summary(conn, server_id)
    total = summary.get("total_connections")
    new_connections = max(total - prior_total, 0) if total is not None and prior_total is not None else None
    with conn.cursor() as cur:
        cur.execute("INSERT INTO network_summaries (server_id, ts, total_connections, new_connections, listening_ports, top_remote_ips) VALUES (%s, %s, %s, %s, %s, %s)", (server_id, ts, total, new_connections, summary.get("listening_ports", []), json.dumps(summary.get("top_remote_ips", []))))
    return new_connections

def db_write_log_summary(conn, server_id, ts, window_seconds, log_summary):
    if not log_summary: return
    with conn.cursor() as cur:
        cur.execute("INSERT INTO log_summaries (server_id, ts, window_seconds, error_count, warning_count, top_errors) VALUES (%s, %s, %s, %s, %s, %s)", (server_id, ts, window_seconds, log_summary.get("error_count", 0), log_summary.get("warning_count", 0), json.dumps(log_summary.get("top_errors", []))))

def db_write_top_processes(conn, server_id, sample_id, ts, top_cpu, top_mem):
    if sample_id is None or (not top_cpu and not top_mem): return
    with conn.cursor() as cur:
        for rank_by, rows in (("cpu", top_cpu), ("memory", top_mem)):
            for r in rows:
                cur.execute("INSERT INTO top_processes (sample_id, server_id, ts, rank_by, rank_position, pid, process_name, cpu_pct, mem_pct, mem_mb) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (sample_id, server_id, ts, rank_by, r["rank_position"], r.get("pid"), r.get("process_name"), r.get("cpu_pct"), r.get("mem_pct"), r.get("mem_mb")))


def db_write_mesh_ping_results(conn, alias_to_server_id, mesh_results):
    if not mesh_results:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for r in mesh_results:
            source_alias = r.get("source_alias")
            target_alias = r.get("target_alias")
            source_id = alias_to_server_id.get(source_alias)
            target_id = alias_to_server_id.get(target_alias)
            if not source_id or not target_id:
                continue
            cur.execute(
                "INSERT INTO mesh_ping_results (source_server_id, source_alias, target_server_id, target_alias, target_ip, success, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (source_id, source_alias, target_id, target_alias, r.get("target_ip"), r.get("success", False), r.get("latency_ms")),
            )
            inserted += 1
    return inserted

# ---------------------------------------------------------------------------
# App commands (from headless_add_app.py registry)
# ---------------------------------------------------------------------------


def load_app_commands():
    """Load the app_commands.json registry created by headless_add_app.py."""
    if not os.path.exists(APP_COMMANDS_FILE):
        return {}
    try:
        with open(APP_COMMANDS_FILE) as f:
            data = json.load(f)
        return data.get("apps", {})
    except Exception as e:
        sys.stderr.write(f"[p1_helper] Failed to load app commands from {APP_COMMANDS_FILE}: {e}\n")
        return {}


def run_local_command(command, timeout=10):
    """Run a shell command locally and return (stdout, error)."""
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, result.stderr.strip() or "command failed"
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


def _parse_number(value, default=0.0):
    """Parse a numeric value from a command output string."""
    try:
        if value is None:
            return default
        text = str(value).strip().splitlines()[0]
        return float(text)
    except (ValueError, TypeError):
        return default


def collect_local_app_metrics():
    """Collect app metrics locally by running commands from app_commands.json.

    Returns a list of metric dicts compatible with db_write_app_metrics().
    """
    apps = load_app_commands()
    results = []

    for app_name, app_config in apps.items():
        if not app_config.get("enabled", True):
            continue

        commands = app_config.get("commands", {})
        raw_outputs = {}
        error = None

        for metric_name, command in commands.items():
            out, err = run_local_command(command)
            if err:
                error = err
                break
            raw_outputs[metric_name] = out

        result = {
            "app_name": app_name,
            "display_name": app_config.get("display_name", app_name),
            "status": raw_outputs.get("status", "unknown"),
            "cpu_pct": _parse_number(raw_outputs.get("cpu_pct")),
            "rss_memory_mb": _parse_number(raw_outputs.get("rss_memory_mb")),
            "process_count": int(_parse_number(raw_outputs.get("process_count"))),
            "thread_count": int(_parse_number(raw_outputs.get("thread_count"))),
            "listening_sockets": int(_parse_number(raw_outputs.get("listening_sockets"))),
        }

        if error:
            result.update({"status": "error", "error": error})

        results.append(result)

    return results


def cmd_collect_apps():
    """Run local app metric collection from app_commands.json and persist to DB.

    Usage: p1_fixed.py collect-apps
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    app_metrics = collect_local_app_metrics()

    if not app_metrics:
        output = {
            "status": "ok",
            "apps_checked": 0,
            "message": "No apps configured in app_commands.json.",
        }
        print(json.dumps(output, indent=2, default=str))
        return

    conn, err = db_connect()
    if conn is None:
        output = {
            "status": "error",
            "error": f"DB connection failed: {err}",
            "apps_checked": len(app_metrics),
            "results": app_metrics,
        }
        print(json.dumps(output, indent=2, default=str))
        return

    try:
        # Get or create the local machine entry
        server_id = db_ensure_machine(
            conn,
            "Local VM",
            {
                "hostname": socket.gethostname(),
                "ip_address": "127.0.0.1",
            },
        )

        db_write_app_metrics(conn, server_id, ts, app_metrics)

        output = {
            "status": "ok",
            "apps_checked": len(app_metrics),
            "rows_inserted": len(app_metrics),
            "server_id": server_id,
            "results": app_metrics,
        }
        print(json.dumps(output, indent=2, default=str))
    except Exception as e:
        output = {
            "status": "error",
            "error": str(e),
            "apps_checked": len(app_metrics),
            "results": app_metrics,
        }
        print(json.dumps(output, indent=2, default=str))
    finally:
        conn.close()


def collect_app_commands_remote(target):
    """Run app_commands.json commands via SSH on a remote machine.

    Returns a list of metric dicts compatible with db_write_app_metrics().
    """
    apps = load_app_commands()
    results = []

    for app_name, app_config in apps.items():
        if not app_config.get("enabled", True):
            continue

        commands = app_config.get("commands", {})
        raw_outputs = {}
        error = None

        for metric_name, command in commands.items():
            ok, out, err = ssh_run(target, command, timeout=15)
            if not ok:
                error = err
                break
            raw_outputs[metric_name] = (out or "").strip()

        result = {
            "app_name": app_name,
            "display_name": app_config.get("display_name", app_name),
            "status": raw_outputs.get("status", "unknown"),
            "cpu_pct": _parse_number(raw_outputs.get("cpu_pct")),
            "rss_memory_mb": _parse_number(raw_outputs.get("rss_memory_mb")),
            "process_count": int(_parse_number(raw_outputs.get("process_count"))),
            "thread_count": int(_parse_number(raw_outputs.get("thread_count"))),
            "listening_sockets": int(_parse_number(raw_outputs.get("listening_sockets"))),
        }

        if error:
            result.update({"status": "error", "error": error})

        results.append(result)

    return results


def persist_to_db(per_machine_records, custom_metrics_cfg, mesh_ping_results=None):
    conn, err = db_connect()
    if conn is None: return False, err, [r["alias"] for r in per_machine_records]

    try:
        db_ensure_schema(conn)
        db_sync_metric_registry(conn, custom_metrics_cfg)
        db_ensure_custom_metric_columns(conn, custom_metrics_cfg)
    except Exception as e:
        sys.stderr.write(f"[p1_helper] Failed to ensure schema/sync registry: {e}\n")

    failed_aliases = []; last_err = None
    alias_to_server_id = {}
    try:
        for rec in per_machine_records:
            try:
                server_id = db_ensure_machine(conn, rec["alias"], rec.get("inventory"))
                alias_to_server_id[rec["alias"]] = server_id
                db_write_machine_state(conn, server_id, rec["machine_state"])

                sample_id = None
                if rec["status"] != "ssh_error":
                    sample_id = db_write_metric_sample(conn, server_id, rec["ts"], rec["mode"], rec["stats"], rec["status"])
                    db_write_custom_metrics(conn, server_id, rec["ts"], rec.get("custom_metrics"))
                    db_write_custom_metric_columns(conn, sample_id, rec.get("custom_metrics"))
                    db_write_service_status(conn, server_id, rec["service_status"])
                    db_write_app_metrics(conn, server_id, rec["ts"], rec.get("app_metrics"))
                    db_write_package_state(conn, server_id, rec.get("packages"))
                    db_write_network_checks(conn, server_id, rec["ts"], rec.get("network_checks"))
                    db_write_network_summary(conn, server_id, rec["ts"], rec.get("network_summary"))
                    db_write_log_summary(conn, server_id, rec["ts"], rec.get("log_window_seconds"), rec.get("log_summary"))
                    top_cpu, top_mem = rec.get("top_processes", ([], []))
                    db_write_top_processes(conn, server_id, sample_id, rec["ts"], top_cpu, top_mem)

                db_write_events(conn, server_id, rec["alerts"])
            except Exception as e:
                failed_aliases.append(rec["alias"]); last_err = f"{rec['alias']}: {e}"
                sys.stderr.write(f"[p1_helper] Postgres write failed for {rec['alias']}: {e}\n")
                continue

        db_write_mesh_ping_results(conn, alias_to_server_id, mesh_ping_results)

        if failed_aliases: return False, last_err, failed_aliases
        return True, None, []
    except Exception as e:
        return False, str(e), [r["alias"] for r in per_machine_records]
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Core monitoring logic
# ---------------------------------------------------------------------------

def evaluate_machine(alias, machine_cfg, ssh_target, prior_state, consecutive_required, mode, custom_metrics_cfg):
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    stats, ssh_err = collect_stats(ssh_target)
    if stats is None:
        new_state = dict(prior_state)
        new_state["last_ssh_error"] = (ssh_err or "")[:300]
        new_state["last_checked"] = timestamp
        db_record = {"alias": alias, "ts": timestamp, "mode": mode, "status": "ssh_error", "inventory": None, "stats": {}, "custom_metrics": [], "service_status": {}, "app_metrics": [], "packages": [], "network_checks": [], "network_summary": None, "log_summary": None, "log_window_seconds": None, "top_processes": ([], []), "alerts": [], "machine_state": new_state}
        return new_state, [], db_record

    thresholds = machine_cfg.get("thresholds", {})
    services = machine_cfg.get("package_checks", [])
    apps = machine_cfg.get("apps", [])
    packages = machine_cfg.get("packages", [])
    network_checks_cfg = machine_cfg.get("network_checks", [])
    top_n = machine_cfg.get("top_n", 5)
    disk_device = machine_cfg.get("disk_device")
    network_interface = machine_cfg.get("network_interface")

    custom_metrics = collect_custom_metrics(ssh_target, custom_metrics_cfg)
    service_status = check_services(ssh_target, services)
    inventory, _ = collect_inventory(ssh_target)
    app_metrics = collect_app_metrics(ssh_target, apps)

    # Also collect apps registered in app_commands.json via SSH
    try:
        registry_app_metrics = collect_app_commands_remote(ssh_target)
        if registry_app_metrics:
            app_metrics.extend(registry_app_metrics)
    except Exception as e:
        sys.stderr.write(f"[p1_helper] {alias}: app_commands.json collection failed: {e}\n")
    package_state = collect_package_state(ssh_target, packages)
    network_checks = collect_network_checks(ssh_target, network_checks_cfg)
    network_summary, _ = collect_network_summary(ssh_target)

    disk_io, _ = (None, None)
    if disk_device:
        disk_io, d_err = collect_disk_io(ssh_target, disk_device)
        if disk_io is None and d_err: sys.stderr.write(f"[p1_helper] {alias}: disk I/O collection failed: {d_err}\n")

    net_throughput, _ = (None, None)
    if network_interface:
        net_throughput, n_err = collect_network_throughput(ssh_target, network_interface)
        if net_throughput is None and n_err: sys.stderr.write(f"[p1_helper] {alias}: network throughput collection failed: {n_err}\n")

    gateway_ping, g_err = collect_gateway_ping(ssh_target, network_checks_cfg[0].get("target") if network_checks_cfg else None)
    if gateway_ping is None and g_err: sys.stderr.write(f"[p1_helper] {alias}: gateway ping failed: {g_err}\n")

    if disk_io: stats.update(disk_io)
    if net_throughput: stats.update(net_throughput)
    if gateway_ping: stats.update(gateway_ping)

    log_summary, _ = collect_log_summary(ssh_target, prior_state.get("last_log_check_ts"))
    nominal_window = 30 if mode == "highfreq" else 300

    top_cpu, top_mem = collect_top_processes(ssh_target, top_n)

    prior_counters = prior_state.get("breach_counters", {})
    new_counters = {}; alerts = []

    metric_map = {"ram": "ram_pct", "disk": "disk_pct", "cpu": "cpu_pct"}
    metric_values = dict(stats)
    if log_summary:
        metric_values["error_count"] = log_summary.get("error_count")
        metric_values["warning_count"] = log_summary.get("warning_count")
        metric_map["error_count"] = "error_count"
        metric_map["warning_count"] = "warning_count"

    for metric_name, stat_key in metric_map.items():
        if metric_name not in thresholds: continue
        value = metric_values.get(stat_key); threshold = thresholds[metric_name]
        prior_count = prior_counters.get(metric_name, 0)
        count = prior_count + 1 if value is not None and value > threshold else 0
        new_counters[metric_name] = count
        if count >= consecutive_required:
            alerts.append({"machine": alias, "metric": metric_name, "value": value, "threshold": threshold, "consecutive_breaches": count, "message": f"{alias}: {metric_name.upper()} at {value} exceeds threshold {threshold} for {count} consecutive checks"})

    for svc in [s for s, status in service_status.items() if status != "active"]:
        alerts.append({"machine": alias, "metric": f"service:{svc}", "value": service_status[svc], "threshold": "active", "consecutive_breaches": None, "message": f"{alias}: service '{svc}' is {service_status[svc]} (expected active)"})

    new_state = dict(prior_state)
    new_state["breach_counters"] = new_counters
    new_state["last_checked"] = timestamp
    new_state["last_log_check_ts"] = timestamp
    new_state.pop("last_ssh_error", None)

    db_record = {"alias": alias, "ts": timestamp, "mode": mode, "status": "ok", "inventory": inventory, "stats": stats, "custom_metrics": custom_metrics, "service_status": service_status, "app_metrics": app_metrics, "packages": package_state, "network_checks": network_checks, "network_summary": network_summary, "log_summary": log_summary, "log_window_seconds": nominal_window, "top_processes": (top_cpu, top_mem), "alerts": alerts, "machine_state": new_state}

    return new_state, alerts, db_record


def load_machines_from_db():
    conn, err = db_connect()
    if conn is None:
        sys.stderr.write(f"[p1_helper] DB machine load failed: {err}\n")
        return {}, {}

    monitor_cfg = {}
    ssh_targets = {}

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                    alias,
                    COALESCE(host(ip_address), hostname) AS host,
                    COALESCE(ssh_port, 22) AS ssh_port,
                    ssh_user,
                    ssh_key_path
                FROM machines
                WHERE COALESCE(monitoring_enabled, true) = true
                ORDER BY alias;
            """)
            rows = cur.fetchall()

        for r in rows:
            alias = r["alias"]

            monitor_cfg[alias] = {
                "thresholds": {},
                "package_checks": [],
                "apps": [],
                "packages": [],
                "network_checks": [],
                "top_n": 5,
                "disk_device": None,
                "network_interface": None,
            }

            if r["host"] and r["ssh_user"] and r["ssh_key_path"]:
                ssh_targets[alias] = {
                    "host": r["host"],
                    "port": r["ssh_port"] or 22,
                    "user": r["ssh_user"],
                    "ssh_key": r["ssh_key_path"],
                }

        # Merge monitor_config.yaml over DB defaults (apps, thresholds, packages, etc.)
        try:
            yaml_cfg, _ = load_monitor_config()
            for alias, yaml_entry in yaml_cfg.items():
                if alias in monitor_cfg:
                    cfg = monitor_cfg[alias]
                    for key in ("thresholds", "package_checks", "apps", "packages", "network_checks"):
                        yaml_val = yaml_entry.get(key)
                        if yaml_val:
                            if isinstance(cfg.get(key), dict) and isinstance(yaml_val, dict):
                                cfg[key].update(yaml_val)
                            elif isinstance(cfg.get(key), list) and isinstance(yaml_val, list):
                                cfg[key] = list(yaml_val)
                    if yaml_entry.get("top_n") is not None:
                        cfg["top_n"] = yaml_entry["top_n"]
                    if yaml_entry.get("disk_device"):
                        cfg["disk_device"] = yaml_entry["disk_device"]
                    if yaml_entry.get("network_interface"):
                        cfg["network_interface"] = yaml_entry["network_interface"]
        except Exception as e:
            sys.stderr.write(f"[p1_helper] Failed to load/merge monitor_config.yaml: {e}\n")

        return monitor_cfg, ssh_targets

    finally:
        conn.close()


def cmd_run(mode):
    monitor_cfg, ssh_targets = load_machines_from_db()
    settings = {"consecutive_threshold_breaches": 2}
    custom_metrics_cfg = load_custom_metrics()
    state = load_state()
    consecutive_required = int(settings.get("consecutive_threshold_breaches", 2))

    new_state = dict(state); all_alerts = []; processed_machines = []; skipped_machines = []; db_records = []

    for alias, machine_cfg in monitor_cfg.items():
        machine_state = state.get(alias, {})
        install_state = machine_state.get("install_state", "NORMAL")

        if mode == "standard":
            if install_state == "INSTALLING": skipped_machines.append(alias); continue
        elif mode == "highfreq":
            if install_state != "INSTALLING": skipped_machines.append(alias); continue
        else:
            print(json.dumps({"error": f"unknown mode: {mode}"})); return

        ssh_target = ssh_targets.get(alias)
        if not ssh_target: skipped_machines.append(alias); continue

        new_machine_state, alerts, db_record = evaluate_machine(alias, machine_cfg, ssh_target, machine_state, consecutive_required, mode, custom_metrics_cfg)
        new_machine_state["install_state"] = install_state
        if "installing_since" in machine_state: new_machine_state["installing_since"] = machine_state["installing_since"]
        db_record["machine_state"]["install_state"] = install_state
        if "installing_since" in machine_state: db_record["machine_state"]["installing_since"] = machine_state["installing_since"]

        new_state[alias] = new_machine_state
        all_alerts.extend(alerts)
        processed_machines.append(alias)
        db_records.append(db_record)

    if mode == "highfreq" and not processed_machines:
        print(json.dumps({"skipped": True, "reason": "no machines in INSTALLING state", "mode": mode})); return

    # Collect mesh ping results between machines that completed SSH-backed evaluation.
    # If a peer is reachable over SSH but ICMP fails, the row is still written with
    # success=false and latency_ms=NULL.
    all_mesh_results = []
    mesh_targets = []
    mesh_peer_records = [rec for rec in db_records if rec["status"] != "ssh_error"]
    for rec in mesh_peer_records:
        inv = rec.get("inventory")
        ip = (inv.get("ip_address") if inv else None) or ssh_targets.get(rec["alias"], {}).get("host")
        if ip:
            mesh_targets.append({"alias": rec["alias"], "ip": ip})

    if mesh_targets:
        for rec in mesh_peer_records:
            source_alias = rec["alias"]
            ssh_target = ssh_targets.get(source_alias)
            if not ssh_target:
                continue
            targets_to_ping = [t for t in mesh_targets if t["alias"] != source_alias]
            if not targets_to_ping:
                continue
            ping_results = collect_mesh_ping_from(ssh_target, targets_to_ping)
            for pr in ping_results:
                all_mesh_results.append({
                    "source_alias": source_alias,
                    **pr,
                })

    db_ok, db_err, db_failed_aliases = persist_to_db(db_records, custom_metrics_cfg, mesh_ping_results=all_mesh_results)
    if not db_ok: sys.stderr.write(f"[p1_helper] Postgres write FAILED for {db_failed_aliases}: {db_err}\n")

    db_status_text = "OK" if db_ok else (f"PARTIAL FAILURE ({', '.join(db_failed_aliases)}) - {db_err}" if db_failed_aliases and len(db_failed_aliases) < len(db_records) else f"FAILED - {db_err}")

    result = {
        "mode": mode, "processed_machines": processed_machines, "skipped_machines": skipped_machines,
        "new_system_state": new_state, "alerts": all_alerts, "db_write_ok": db_ok, "db_write_error": db_err,
        "db_write_failed_machines": db_failed_aliases, "mesh_ping_results_collected": len(all_mesh_results),
        "summary": f"Processed {len(processed_machines)} machine(s) in {mode} mode. {len(all_alerts)} alert(s) fired. Postgres write: {db_status_text}." + (" ALERTS: " + "; ".join(a["message"] for a in all_alerts) if all_alerts else ""),
    }
    if mode == "highfreq": result["note"] = "If new_system_state shows no machines still INSTALLING, this high-freq job will be a no-op."

    print(json.dumps(result, indent=2, default=str))

def cmd_validate(alias):
    monitor_cfg, ssh_targets = load_machines_from_db()
    machine_cfg = monitor_cfg.get(alias); ssh_target = ssh_targets.get(alias)

    if not machine_cfg or not ssh_target:
        print(json.dumps({"passed": False, "details": {"error": f"no monitor/ssh config for machine '{alias}'"}})); return

    stats, ssh_err = collect_stats(ssh_target)
    if stats is None:
        print(json.dumps({"passed": False, "details": {"error": f"ssh failed: {(ssh_err or '')[:300]}"}})); return

    thresholds = machine_cfg.get("thresholds", {})
    services = machine_cfg.get("package_checks", [])
    service_status = check_services(ssh_target, services)
    failed_services = [s for s, status in service_status.items() if status != "active"]

    resource_failures = []
    for metric_name, stat_key in {"ram": "ram_pct", "disk": "disk_pct", "cpu": "cpu_pct"}.items():
        if metric_name not in thresholds: continue
        value = stats.get(stat_key); threshold = thresholds[metric_name]
        if value is not None and value > threshold: resource_failures.append({"metric": metric_name, "value": value, "threshold": threshold})

    print(json.dumps({"passed": not failed_services and not resource_failures, "details": {"machine": alias, "stats": stats, "service_status": service_status, "failed_services": failed_services, "resource_failures": resource_failures}}, indent=2, default=str))

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: p1_fixed.py run --mode standard|highfreq | validate --machine <alias> | collect-apps"})); sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "run":
        mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv and sys.argv.index("--mode") + 1 < len(sys.argv) else None
        if mode not in ("standard", "highfreq"):
            print(json.dumps({"error": "--mode must be 'standard' or 'highfreq'"})); sys.exit(1)
        cmd_run(mode)
    elif cmd == "validate":
        alias = sys.argv[sys.argv.index("--machine") + 1] if "--machine" in sys.argv and sys.argv.index("--machine") + 1 < len(sys.argv) else None
        if not alias:
            print(json.dumps({"error": "validate requires --machine <alias>"})); sys.exit(1)
        cmd_validate(alias)
    elif cmd == "collect-apps":
        cmd_collect_apps()
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"})); sys.exit(1)

if __name__ == "__main__":
    main()
