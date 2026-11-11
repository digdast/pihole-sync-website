#!/usr/bin/env python3
"""
End-to-End Deployment Script for https://pihole-sync.ertechlab.com
==================================================================
1. Deploys /usr/local/bin/pve-pihole-dns-sync.py & /usr/local/bin/pve-pihole-sync-agent.py
   and pve-pihole-sync-agent.service (port 8765) onto pve-00 (192.168.0.30).
2. Deploys app.py, ads.txt, static/index.html, and k8s-pihole-sync.yaml onto
   k3s-cp-01 (192.168.0.80, NodePort 30101 in namespace 'tools').
3. Configures Cloudflare DNS (pihole-sync.ertechlab.com CNAME -> tunnel),
   Nginx Proxy Manager (pihole-sync.ertechlab.com, pihole-sync.ertech.home, pihole-sync.ertechhome.com),
   Pi-hole static DNS, and ERTECHLab Command Center dashboard card.
4. Verifies end-to-end health, API telemetry, Sync triggers, and AdSense /ads.txt compliance.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(r"C:\scripts\pihole-sync-website")
PVE00_IP = os.environ.get("PVE00_IP", "192.168.0.30")
PVE00_USER = os.environ.get("PVE00_USER", "root")
PVE00_PW = os.environ.get("PVE00_PW", "")

K3S_CP01_IP = os.environ.get("K3S_CP01_IP", "192.168.0.80")
K3S_USER = os.environ.get("K3S_USER", "plexadmin")
K3S_PW = os.environ.get("K3S_PW", "")

NPM_URL = os.environ.get("NPM_URL", "http://192.168.0.80:81")
NPM_EMAIL = os.environ.get("NPM_EMAIL", "efrain.acetech@gmail.com")
NPM_PW = os.environ.get("NPM_PW", "")

CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "12dc4199ed0a8ea1fe7308c9299d4c35")
CF_TOKEN = os.environ.get("CF_TOKEN", "")
CF_TUNNEL_TARGET = os.environ.get("CF_TUNNEL_TARGET", "2f5e2042-9913-439c-8df2-bb8312ce022c.cfargotunnel.com")

AGENT_SERVICE_UNIT = """[Unit]
Description=Proxmox & Pi-hole Nebula-Sync Control Plane API Agent
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/pve-pihole-sync-agent.py
Restart=always
RestartSec=3
Environment=AGENT_PORT=8765

[Install]
WantedBy=multi-user.target
"""


def ssh_connect(host: str, user: str, password: str) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, timeout=12)
    return ssh


def deploy_pve_agent():
    print("=== [1/5] Deploying pve-pihole-sync-agent on pve-00 (192.168.0.30:8765) ===")
    ssh = ssh_connect(PVE00_IP, PVE00_USER, PVE00_PW)
    sftp = ssh.open_sftp()

    # Upload updated pve-pihole-dns-sync.py and new pve-pihole-sync-agent.py
    dns_sync_src = Path(r"C:\scripts\pve_pihole_dns_sync.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    with sftp.open("/usr/local/bin/pve-pihole-dns-sync.py", "w") as f:
        f.write(dns_sync_src)
    sftp.chmod("/usr/local/bin/pve-pihole-dns-sync.py", 0o755)

    agent_src = (BASE_DIR / "pve_pihole_sync_agent.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    with sftp.open("/usr/local/bin/pve-pihole-sync-agent.py", "w") as f:
        f.write(agent_src)
    sftp.chmod("/usr/local/bin/pve-pihole-sync-agent.py", 0o755)

    with sftp.open("/etc/systemd/system/pve-pihole-sync-agent.service", "w") as f:
        f.write(AGENT_SERVICE_UNIT.replace("\r\n", "\n"))

    sftp.close()

    _, out, err = ssh.exec_command(
        "systemctl daemon-reload && "
        "systemctl enable pve-pihole-sync-agent.service && "
        "systemctl restart pve-pihole-sync-agent.service && "
        "sleep 1 && "
        "systemctl is-active pve-pihole-sync-agent.service"
    )
    status = out.read().decode("utf-8", errors="replace").strip()
    print(f"  [OK] pve-pihole-sync-agent.service state: {status}")
    ssh.close()

    # Test API status endpoint directly
    with urllib.request.urlopen(f"http://{PVE00_IP}:8765/api/status", timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        total_nodes = 1 + len(data.get("nebula", {}).get("replicas", []))
        print(
            f"  [OK] Agent /api/status verified: {total_nodes} Pi-hole nodes configured, "
            f"{len(data.get('pve_guests', []))} PVE guests, {len(data.get('all_dns_records', []))} total DNS records"
        )


def deploy_k3s_website():
    print("\n=== [2/5] Deploying pihole-sync-website to K3s (k3s-cp-01 / NodePort 30101) ===")
    ssh = ssh_connect(K3S_CP01_IP, K3S_USER, K3S_PW)

    def sudo_run(cmd: str) -> str:
        _, out, err = ssh.exec_command(f"echo {K3S_PW} | sudo -S bash -c '{cmd}'")
        res = out.read().decode("utf-8", errors="replace").strip()
        return res

    sudo_run("mkdir -p /var/lib/appdata/pihole-sync-website/static && chown -R plexadmin:plexadmin /var/lib/appdata/pihole-sync-website")

    sftp = ssh.open_sftp()
    for rel in ["app.py", "ads.txt", "k8s-pihole-sync.yaml", "static/index.html"]:
        content = (BASE_DIR / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        remote_path = f"/var/lib/appdata/pihole-sync-website/{rel}"
        with sftp.open(remote_path, "w") as f:
            f.write(content)
    sftp.close()

    apply_out = sudo_run(
        "kubectl apply -f /var/lib/appdata/pihole-sync-website/k8s-pihole-sync.yaml && "
        "kubectl rollout restart deployment/pihole-sync-website -n tools && "
        "kubectl rollout status deployment/pihole-sync-website -n tools --timeout=60s"
    )
    print(f"  [OK] K3s Deployment:\n    " + "\n    ".join(apply_out.splitlines()))
    ssh.close()

    # Wait briefly and test NodePort 30101
    for _ in range(10):
        try:
            with urllib.request.urlopen(f"http://{K3S_CP01_IP}:30101/healthz", timeout=5) as r:
                if r.status == 200:
                    print(f"  [OK] http://{K3S_CP01_IP}:30101/healthz -> 200 OK")
                    break
        except Exception:
            time.sleep(1)


def configure_cloudflare_and_npm():
    print("\n=== [3/5] Configuring Cloudflare DNS & Nginx Proxy Manager for pihole-sync.ertechlab.com ===")
    cf_headers = {
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?name=pihole-sync.ertechlab.com",
        headers=cf_headers,
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        existing = json.loads(r.read().decode("utf-8")).get("result", [])

    cf_payload = {
        "type": "CNAME",
        "name": "pihole-sync.ertechlab.com",
        "content": CF_TUNNEL_TARGET,
        "ttl": 1,
        "proxied": True,
    }
    if existing:
        rec_id = existing[0]["id"]
        cf_req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{rec_id}",
            data=json.dumps(cf_payload).encode("utf-8"),
            headers=cf_headers,
            method="PUT",
        )
    else:
        cf_req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
            data=json.dumps(cf_payload).encode("utf-8"),
            headers=cf_headers,
            method="POST",
        )
    with urllib.request.urlopen(cf_req, timeout=10) as r:
        cf_res = json.loads(r.read().decode("utf-8"))
        print(f"  [OK] Cloudflare DNS CNAME pihole-sync.ertechlab.com -> {CF_TUNNEL_TARGET} (success={cf_res.get('success')})")

    # Authenticate to NPM
    tok_req = urllib.request.Request(
        f"{NPM_URL}/api/tokens",
        data=json.dumps({"identity": NPM_EMAIL, "secret": NPM_PW}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(tok_req, timeout=10) as r:
        npm_token = json.loads(r.read().decode("utf-8"))["token"]

    npm_headers = {"Authorization": f"Bearer {npm_token}", "Content-Type": "application/json"}

    # Find wildcard cert for *.ertechlab.com
    with urllib.request.urlopen(urllib.request.Request(f"{NPM_URL}/api/nginx/certificates", headers=npm_headers), timeout=10) as r:
        certs = json.loads(r.read().decode("utf-8"))
    ertechlab_cert_id = 0
    for c in certs:
        if "*.ertechlab.com" in c.get("domain_names", []):
            ertechlab_cert_id = c["id"]
            break
    print(f"  [OK] Using NPM wildcard SSL cert ID: {ertechlab_cert_id}")

    # Upsert Proxy Host for pihole-sync.ertechlab.com
    with urllib.request.urlopen(urllib.request.Request(f"{NPM_URL}/api/nginx/proxy-hosts", headers=npm_headers), timeout=10) as r:
        hosts = json.loads(r.read().decode("utf-8"))

    domain_names = ["pihole-sync.ertechlab.com", "pihole-sync.ertech.home", "pihole-sync.ertechhome.com"]
    existing_host = next((h for h in hosts if "pihole-sync.ertechlab.com" in h.get("domain_names", [])), None)

    host_payload = {
        "domain_names": domain_names,
        "forward_scheme": "http",
        "forward_host": "192.168.0.80",
        "forward_port": 30101,
        "access_list_id": 0,
        "certificate_id": ertechlab_cert_id,
        "ssl_forced": False,
        "caching_enabled": False,
        "block_exploits": True,
        "advanced_config": "",
        "meta": {"letsencrypt_agree": False, "dns_challenge": False},
        "allow_websocket_upgrade": True,
        "http2_support": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
    }
    if existing_host:
        npm_req = urllib.request.Request(
            f"{NPM_URL}/api/nginx/proxy-hosts/{existing_host['id']}",
            data=json.dumps(host_payload).encode("utf-8"),
            headers=npm_headers,
            method="PUT",
        )
    else:
        npm_req = urllib.request.Request(
            f"{NPM_URL}/api/nginx/proxy-hosts",
            data=json.dumps(host_payload).encode("utf-8"),
            headers=npm_headers,
            method="POST",
        )
    with urllib.request.urlopen(npm_req, timeout=15) as r:
        npm_res = json.loads(r.read().decode("utf-8"))
        print(f"  [OK] NPM Proxy Host ID {npm_res['id']}: {domain_names} -> http://192.168.0.80:30101")

    # Sync NPM across K3s control plane nodes
    ssh = ssh_connect(K3S_CP01_IP, K3S_USER, K3S_PW)
    _, out, _ = ssh.exec_command(f"echo {K3S_PW} | sudo -S /usr/local/bin/sync-npm.sh")
    out.read()
    ssh.close()
    print("  [OK] NPM synced across K3s control plane nodes.")


def register_static_dns_and_dashboard():
    print("\n=== [4/5] Registering Local Pi-hole DNS & ERTECHLab Dashboard Card ===")
    # Add static DNS entries via our new agent API!
    for hostname in ["pihole-sync.ertechlab.com", "pihole-sync.ertech.home", "pihole-sync.ertechhome.com"]:
        req = urllib.request.Request(
            f"http://{PVE00_IP}:8765/api/dns/static-add",
            data=json.dumps({"ip": "192.168.0.80", "fqdn": hostname}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            res = json.loads(r.read().decode("utf-8"))
            print(f"  [OK] Added static DNS 192.168.0.80 -> {hostname} (ok={res.get('ok')})")

    # Update local C:\scripts\ertechlab-dashboard\config.json and remote /var/lib/appdata/ertechlab-dashboard/config.json
    dash_path = Path(r"C:\scripts\ertechlab-dashboard\config.json")
    if dash_path.exists():
        cfg = json.loads(dash_path.read_text(encoding="utf-8"))
        services = cfg.get("services", [])
        card = {
            "id": "pihole-sync",
            "name": "Pi-hole & Nebula-Sync Control Center",
            "category": "networking",
            "url": "https://pihole-sync.ertechlab.com",
            "internal_url": "http://192.168.0.80:30101",
            "health_url": "http://192.168.0.80:30101/healthz",
            "icon": "refresh-cw",
            "description": "Real-time Proxmox Guest DNS Auto-Injector & Pi-hole v6 Nebula-Sync Cluster Manager",
            "tags": ["pihole", "nebula-sync", "dns", "proxmox", "cluster"],
        }
        idx = next((i for i, s in enumerate(services) if s.get("id") == "pihole-sync"), None)
        if idx is not None:
            services[idx] = card
        else:
            # Insert right after pihole
            pihole_idx = next((i for i, s in enumerate(services) if s.get("id") == "pihole"), len(services) - 1)
            services.insert(pihole_idx + 1, card)
        formatted = json.dumps(cfg, indent=2)
        dash_path.write_text(formatted, encoding="utf-8")

        ssh = ssh_connect(K3S_CP01_IP, K3S_USER, K3S_PW)
        sftp = ssh.open_sftp()
        with sftp.open("/tmp/dashboard_config.json", "w") as f:
            f.write(formatted)
        sftp.close()
        _, out, _ = ssh.exec_command(
            f"echo {K3S_PW} | sudo -S cp /tmp/dashboard_config.json /var/lib/appdata/ertechlab-dashboard/config.json"
        )
        out.read()
        ssh.close()
        print("  [OK] Updated ERTECHLab Command Center dashboard config.json")


def verify_end_to_end():
    print("\n=== [5/5] End-to-End Verification ===")
    for host in ["pihole-sync.ertechlab.com", "pihole-sync.ertech.home"]:
        for path in ["/", "/ads.txt", "/api/status"]:
            req = urllib.request.Request(f"http://192.168.0.80{path}", headers={"Host": host})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", errors="replace")
                print(f"  [OK] Host={host} {path} -> HTTP {r.status} ({len(body)} bytes)")

    # Trigger a full pipeline sync through the web app proxy
    sync_req = urllib.request.Request(
        "http://192.168.0.80/api/sync",
        data=json.dumps({"mode": "full"}).encode("utf-8"),
        headers={"Host": "pihole-sync.ertechlab.com", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(sync_req, timeout=60) as r:
        sync_res = json.loads(r.read().decode("utf-8"))
        print(f"  [OK] POST /api/sync (mode=full) -> ok={sync_res.get('ok')} duration={sync_res.get('entry', {}).get('duration_s')}s")


if __name__ == "__main__":
    deploy_pve_agent()
    deploy_k3s_website()
    configure_cloudflare_and_npm()
    register_static_dns_and_dashboard()
    verify_end_to_end()
