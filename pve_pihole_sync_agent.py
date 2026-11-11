#!/usr/bin/env python3
"""
PVE & Nebula-Sync Local Control Agent (runs on pve-00:8765)
===========================================================
Provides a fast local JSON HTTP API for pihole-sync.ertechlab.com to:
  - Inspect & modify /opt/nebula-sync/nebula-sync.env on LXC 300 (192.168.0.2)
  - Add/remove Pi-hole nodes in Nebula-Sync & test Pi-hole v6 API authentication
  - Trigger PVE -> Pi-hole Guest DNS Sync (/usr/local/bin/pve-pihole-dns-sync.py --force)
  - Trigger Nebula-Sync replication (/opt/nebula-sync/nebula-sync run)
  - Trigger Full Pipeline Sync (PVE Guest DNS + Nebula-Sync)
  - Manage custom static DNS records and view sync history/logs
"""

import base64
import datetime
import http.server
import json
import os
import re
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8765
STATE_FILE = "/var/cache/pve-pihole-dns-state.json"
HISTORY_FILE = "/var/cache/pve-pihole-sync-history.json"
LABELS_FILE = "/var/cache/pve-nebula-node-labels.json"

SYNC_LOCK = threading.Lock()


def run_cmd(cmd: str, timeout: int = 45) -> tuple:
    start = time.time()
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        dur = round(time.time() - start, 2)
        return res.returncode, res.stdout.strip(), res.stderr.strip(), dur
    except Exception as e:
        dur = round(time.time() - start, 2)
        return 1, "", str(e), dur


def load_json(path: str, default):
    try:
        if os.path.exists(path):
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: str, data):
    try:
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def append_history(action: str, status: str, duration_s: float, output: str):
    hist = load_json(HISTORY_FILE, [])
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
        "epoch": int(time.time()),
        "action": action,
        "status": status,
        "duration_s": duration_s,
        "output": output[-4000:] if output else "",
    }
    hist.insert(0, entry)
    save_json(HISTORY_FILE, hist[:40])
    return entry


def parse_nebula_target(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return None
    if "|" in raw:
        url, pw = raw.split("|", 1)
    else:
        url, pw = raw, ""
    url = url.strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    host = url.split("://", 1)[1].split("/")[0].split(":")[0]
    labels = load_json(LABELS_FILE, {
        "192.168.0.2": "pihole-01.ertech.home (LXC 300 @ pve-00)",
        "192.168.0.5": "pihole-02.ertech.home (LXC 301 @ pve-04)",
        "192.168.0.7": "pihole-03.ertech.home (LXC 302 @ pve-04)",
    })
    return {
        "url": url,
        "host": host,
        "password": pw,
        "masked_password": ("*" * min(len(pw), 8)) if pw else "(empty/none)",
        "label": labels.get(host) or labels.get(url) or host,
    }


def get_nebula_config() -> dict:
    _, out, _, _ = run_cmd("pct exec 300 -- cat /opt/nebula-sync/nebula-sync.env", timeout=10)
    env_map = {
        "PRIMARY": "http://192.168.0.2|sone_420",
        "REPLICAS": "http://192.168.0.5|sone_420,http://192.168.0.7|sone_420",
        "FULL_SYNC": "true",
        "RUN_GRAVITY": "true",
        "CRON": "0 * * * *",
        "TZ": "America/New_York",
    }
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env_map[k.strip()] = v.strip().strip('"').strip("'")

    primary = parse_nebula_target(env_map.get("PRIMARY", ""))
    replicas = []
    for r in env_map.get("REPLICAS", "").split(","):
        parsed = parse_nebula_target(r)
        if parsed:
            replicas.append(parsed)

    return {
        "primary": primary,
        "replicas": replicas,
        "full_sync": env_map.get("FULL_SYNC", "true").lower() == "true",
        "run_gravity": env_map.get("RUN_GRAVITY", "true").lower() == "true",
        "cron": env_map.get("CRON", "0 * * * *"),
        "tz": env_map.get("TZ", "America/New_York"),
        "raw_env": env_map,
    }


def save_nebula_config(primary: dict, replicas: list, full_sync: bool, run_gravity: bool, cron: str) -> tuple:
    p_str = f"{primary['url']}|{primary.get('password', '')}"
    r_list = [f"{r['url']}|{r.get('password', '')}" for r in replicas if r.get("url")]
    r_str = ",".join(r_list)
    env_content = (
        f"PRIMARY={p_str}\n"
        f"REPLICAS={r_str}\n"
        f"FULL_SYNC={'true' if full_sync else 'false'}\n"
        f"RUN_GRAVITY={'true' if run_gravity else 'false'}\n"
        f"CRON={cron.strip() or '0 * * * *'}\n"
        f"TZ=America/New_York\n"
    )
    b64 = base64.b64encode(env_content.encode("utf-8")).decode("ascii")
    cmd = (
        f"pct exec 300 -- bash -c '"
        f"echo {b64} | base64 -d > /opt/nebula-sync/nebula-sync.env && "
        f"cp -f /opt/nebula-sync/nebula-sync.env /etc/nebula-sync.env && "
        f"systemctl restart nebula-sync"
        f"'"
    )
    code, out, err, dur = run_cmd(cmd, timeout=15)
    return code == 0, out or err


def probe_pihole_node(node_info: dict) -> dict:
    if not node_info or not node_info.get("url"):
        return {"online": False, "error": "Unconfigured"}
    url = node_info["url"].rstrip("/")
    pw = node_info.get("password", "")
    start = time.time()
    result = {
        "url": url,
        "host": node_info.get("host", ""),
        "label": node_info.get("label", ""),
        "masked_password": node_info.get("masked_password", ""),
        "online": False,
        "auth_valid": False,
        "latency_ms": 0,
        "dns_records_count": 0,
        "queries_total": 0,
        "queries_blocked": 0,
        "percent_blocked": 0.0,
        "ftl_version": "",
        "status": "offline",
    }
    try:
        # 1. Authenticate with Pi-hole v6 API
        auth_req = urllib.request.Request(
            f"{url}/api/auth",
            data=json.dumps({"password": pw}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(auth_req, timeout=3.5) as resp:
            result["latency_ms"] = int((time.time() - start) * 1000)
            auth_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            session = auth_data.get("session", {})
            sid = session.get("sid")
            result["online"] = True
            result["auth_valid"] = bool(session.get("valid", False))

        headers = {"Content-Type": "application/json"}
        if sid:
            headers["X-FTL-SID"] = sid

        # 2. Fetch summary stats
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{url}/api/stats/summary", headers=headers),
                timeout=3.0,
            ) as sresp:
                sdata = json.loads(sresp.read().decode("utf-8", errors="replace"))
                q = sdata.get("queries", {})
                result["queries_total"] = q.get("total", 0)
                result["queries_blocked"] = q.get("blocked", 0)
                result["percent_blocked"] = round(float(q.get("percent_blocked", 0.0)), 1)
                result["domains_being_blocked"] = sdata.get("gravity", {}).get("domains_being_blocked", 0)
        except Exception:
            pass

        # 3. Fetch local DNS hosts count from config
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{url}/api/config/dns/hosts", headers=headers),
                timeout=3.0,
            ) as cresp:
                cdata = json.loads(cresp.read().decode("utf-8", errors="replace"))
                hosts_arr = cdata.get("config", {}).get("dns", {}).get("hosts", [])
                result["dns_records_count"] = len(hosts_arr)
        except Exception:
            pass

        # 4. Logout session cleanly so we don't exhaust Pi-hole v6 API sessions
        if sid:
            try:
                urllib.request.urlopen(
                    urllib.request.Request(f"{url}/api/auth", headers=headers, method="DELETE"),
                    timeout=2.0,
                ).read()
            except Exception:
                pass

        result["status"] = "online" if result["auth_valid"] else "auth-error"
    except Exception as e:
        result["latency_ms"] = int((time.time() - start) * 1000)
        result["error"] = str(e)
    return result


def get_full_status() -> dict:
    neb_cfg = get_nebula_config()
    primary_status = probe_pihole_node(neb_cfg["primary"])
    replicas_status = [probe_pihole_node(r) for r in neb_cfg["replicas"]]

    primary_dns_count = primary_status.get("dns_records_count", 0)
    for r in replicas_status:
        r["in_sync"] = r["online"] and r["auth_valid"] and (r["dns_records_count"] == primary_dns_count)

    # Read PVE state cache & Pi-hole dns.hosts list
    pve_state = load_json(STATE_FILE, {"hash": "", "guests": []})
    guests = pve_state.get("guests", [])
    guest_fqdns = {g["fqdn"].lower() for g in guests}

    _, custom_list_out, _, _ = run_cmd("pct exec 300 -- cat /etc/pihole/custom.list", timeout=8)
    all_records = []
    for line in custom_list_out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and not parts[0].startswith("#"):
            ip, fqdn = parts[0], parts[1].lower()
            all_records.append({
                "ip": ip,
                "fqdn": fqdn,
                "source": "pve-auto" if fqdn in guest_fqdns else "static",
            })

    # Service statuses
    _, svc_out, _, _ = run_cmd(
        "systemctl is-active pve-pihole-dns-sync.path pve-pihole-dns-sync.timer; "
        "pct exec 300 -- systemctl is-active nebula-sync",
        timeout=8,
    )
    svc_lines = [s.strip() for s in svc_out.splitlines()]
    while len(svc_lines) < 3:
        svc_lines.append("unknown")

    # Recent journalctl from nebula-sync on LXC 300
    _, j_out, _, _ = run_cmd(
        "pct exec 300 -- journalctl -u nebula-sync -n 25 --no-pager -o short-iso",
        timeout=8,
    )

    return {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
        "nebula": {
            "primary": primary_status,
            "replicas": replicas_status,
            "full_sync": neb_cfg["full_sync"],
            "run_gravity": neb_cfg["run_gravity"],
            "cron": neb_cfg["cron"],
        },
        "daemons": {
            "pve_path_watcher": svc_lines[0],
            "pve_timer": svc_lines[1],
            "nebula_sync_service": svc_lines[2],
            "state_hash": pve_state.get("hash", "")[:16],
        },
        "pve_guests": guests,
        "all_dns_records": all_records,
        "history": load_json(HISTORY_FILE, [])[:25],
        "nebula_journal": j_out,
    }


def trigger_sync(mode: str) -> dict:
    with SYNC_LOCK:
        logs = []
        total_start = time.time()
        overall_ok = True

        if mode in ("pve", "full"):
            code, out, err, dur = run_cmd("python3 /usr/local/bin/pve-pihole-dns-sync.py --force --no-nebula", timeout=35)
            comb = f"=== [PVE -> Pi-hole 192.168.0.2 Guest DNS Auto-Injector ({dur}s, exit={code})] ===\n{out}\n{err}".strip()
            logs.append(comb)
            if code != 0:
                overall_ok = False

        if mode in ("nebula", "full", "gravity"):
            grav_val = "true" if mode == "gravity" else "false"
            code, out, err, dur = run_cmd(
                f"pct exec 300 -- bash -c 'grep -vE \"^(CRON|RUN_GRAVITY)=\" /opt/nebula-sync/nebula-sync.env > /tmp/nebula-oneshot.env && echo \"RUN_GRAVITY={grav_val}\" >> /tmp/nebula-oneshot.env && /opt/nebula-sync/nebula-sync run --env-file /tmp/nebula-oneshot.env'",
                timeout=35,
            )
            clean_out = re.sub(r"\x1b\[[0-9;]*m", "", f"{out}\n{err}").strip()
            comb = f"=== [Nebula-Sync Cluster Replication ({dur}s, exit={code})] ===\n{clean_out}".strip()
            logs.append(comb)
            if code != 0:
                overall_ok = False

        total_dur = round(time.time() - total_start, 2)
        full_output = "\n\n".join(logs)
        entry = append_history(
            action={
                "pve": "PVE Guest DNS Sync",
                "nebula": "Nebula-Sync Replication",
                "full": "Full Pipeline Sync (PVE + Nebula-Sync)",
            }.get(mode, mode),
            status="success" if overall_ok else "error",
            duration_s=total_dur,
            output=full_output,
        )
        return {"ok": overall_ok, "entry": entry}


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AgentHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    def do_GET(self):
        if self.path.startswith("/api/status"):
            self._send_json(200, get_full_status())
        elif self.path.startswith("/healthz"):
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        try:
            body = self._read_body()
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return

        if self.path == "/api/sync":
            mode = body.get("mode", "full")
            res = trigger_sync(mode)
            self._send_json(200, res)

        elif self.path == "/api/nebula/test-node":
            url = body.get("url", "").strip()
            pw = body.get("password", "")
            label = body.get("label", "").strip()
            parsed = parse_nebula_target(f"{url}|{pw}")
            if not parsed:
                self._send_json(400, {"ok": False, "error": "URL is required"})
                return
            if label:
                parsed["label"] = label
            probe = probe_pihole_node(parsed)
            self._send_json(200, {"ok": probe["online"] and probe["auth_valid"], "probe": probe})

        elif self.path == "/api/nebula/add-node":
            url = body.get("url", "").strip()
            pw = body.get("password", "sone_420")
            label = body.get("label", "").strip()
            role = body.get("role", "replica").lower()
            auto_sync = bool(body.get("auto_sync", True))

            parsed = parse_nebula_target(f"{url}|{pw}")
            if not parsed:
                self._send_json(400, {"ok": False, "error": "Valid Pi-hole URL or IP is required"})
                return

            if label:
                labels = load_json(LABELS_FILE, {
                    "192.168.0.2": "pihole-01.ertech.home (LXC 300 @ pve-00)",
                    "192.168.0.5": "pihole-02.ertech.home (LXC 301 @ pve-04)",
                    "192.168.0.7": "pihole.ertechhome.com (LXC 302 @ pve-04)",
                })
                labels[parsed["host"]] = label
                labels[parsed["url"]] = label
                save_json(LABELS_FILE, labels)
                parsed["label"] = label

            cfg = get_nebula_config()
            if role == "primary":
                cfg["primary"] = {"url": parsed["url"], "password": pw}
            else:
                # Replace if URL or host already exists, otherwise append
                existing = [r for r in cfg["replicas"] if r["host"] != parsed["host"] and r["url"] != parsed["url"]]
                existing.append({"url": parsed["url"], "password": pw})
                cfg["replicas"] = existing

            ok, msg = save_nebula_config(
                cfg["primary"],
                cfg["replicas"],
                cfg["full_sync"],
                cfg["run_gravity"],
                cfg["cron"],
            )
            sync_res = None
            if ok and auto_sync:
                sync_res = trigger_sync("nebula")
            else:
                append_history(f"Added Nebula-Sync {role.upper()} node {parsed['url']}", "success" if ok else "error", 0.2, msg)
            self._send_json(200, {"ok": ok, "message": msg, "sync": sync_res})

        elif self.path == "/api/nebula/remove-node":
            target_url = body.get("url", "").strip().rstrip("/")
            target_host = body.get("host", "").strip()
            cfg = get_nebula_config()
            new_replicas = [
                r for r in cfg["replicas"]
                if r["url"].rstrip("/") != target_url and (not target_host or r["host"] != target_host)
            ]
            ok, msg = save_nebula_config(
                cfg["primary"],
                new_replicas,
                cfg["full_sync"],
                cfg["run_gravity"],
                cfg["cron"],
            )
            append_history(f"Removed Nebula-Sync replica {target_url or target_host}", "success" if ok else "error", 0.2, msg)
            self._send_json(200, {"ok": ok, "message": msg})

        elif self.path == "/api/nebula/settings":
            cfg = get_nebula_config()
            full_sync = bool(body.get("full_sync", cfg["full_sync"]))
            run_gravity = bool(body.get("run_gravity", cfg["run_gravity"]))
            cron = str(body.get("cron", cfg["cron"])).strip() or "0 * * * *"
            ok, msg = save_nebula_config(cfg["primary"], cfg["replicas"], full_sync, run_gravity, cron)
            append_history(f"Updated Nebula-Sync settings (FULL_SYNC={full_sync}, RUN_GRAVITY={run_gravity}, CRON='{cron}')", "success" if ok else "error", 0.2, msg)
            self._send_json(200, {"ok": ok, "message": msg})

        elif self.path == "/api/dns/static-add":
            ip = body.get("ip", "").strip()
            fqdn = body.get("fqdn", "").strip().lower()
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) or not fqdn:
                self._send_json(400, {"ok": False, "error": "Valid IPv4 and hostname required"})
                return
            if "." not in fqdn:
                fqdn = f"{fqdn}.ertech.home"
            entry = f"{ip} {fqdn}"
            py_snip = (
                f'p = "/etc/pihole/pihole.toml"\n'
                f'txt = open(p).read()\n'
                f'if "{entry}" not in txt and "hosts = [" in txt:\n'
                f'    txt = txt.replace("hosts = [", "hosts = [\\n    \\"{entry}\\",", 1)\n'
                f'    open(p, "w").write(txt)\n'
            )
            b64 = base64.b64encode(py_snip.encode("utf-8")).decode("ascii")
            run_cmd(f"pct exec 300 -- bash -c 'echo {b64} | base64 -d | python3'", timeout=10)
            res = trigger_sync("full")
            self._send_json(200, {"ok": True, "sync": res})

        elif self.path == "/api/dns/static-remove":
            fqdn = body.get("fqdn", "").strip().lower()
            if not fqdn:
                self._send_json(400, {"ok": False, "error": "Hostname required"})
                return
            py_snip = (
                f'import re\n'
                f'p = "/etc/pihole/pihole.toml"\n'
                f'lines = open(p).read().splitlines()\n'
                f'out = [l for l in lines if " {fqdn}\\"" not in l.lower()]\n'
                f'open(p, "w").write("\\n".join(out) + "\\n")\n'
            )
            b64 = base64.b64encode(py_snip.encode("utf-8")).decode("ascii")
            run_cmd(f"pct exec 300 -- bash -c 'echo {b64} | base64 -d | python3'", timeout=10)
            res = trigger_sync("full")
            self._send_json(200, {"ok": True, "sync": res})

        else:
            self._send_json(404, {"ok": False, "error": "Endpoint not found"})


if __name__ == "__main__":
    # Seed initial history entry if empty
    if not os.path.exists(HISTORY_FILE):
        append_history("System Initialized", "success", 0.1, "PVE -> Pi-hole Auto-Injector & Nebula-Sync Control Agent online.")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), AgentHandler)
    print(f"PVE & Nebula-Sync Control Agent listening on 0.0.0.0:{PORT}")
    server.serve_forever()
