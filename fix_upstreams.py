import json
import subprocess
import urllib.request
import paramiko

def fix_pihole_upstreams():
    # Update upstreams via Pi-hole v6 API on all 3 nodes (192.168.0.2, 192.168.0.5, 192.168.0.7)
    for url in ["http://192.168.0.2", "http://192.168.0.5", "http://192.168.0.7"]:
        auth_req = urllib.request.Request(
            f"{url}/api/auth",
            data=json.dumps({"password": "sone_420"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(auth_req, timeout=8) as r:
            sid = json.loads(r.read().decode("utf-8")).get("session", {}).get("sid")

        headers = {"Content-Type": "application/json"}
        if sid:
            headers["X-FTL-SID"] = sid

        patch_payload = {
            "config": {
                "dns": {
                    "upstreams": ["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"],
                    "dnssec": False,
                    "listeningMode": "ALL",
                }
            }
        }
        req = urllib.request.Request(
            f"{url}/api/config",
            data=json.dumps(patch_payload).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[OK] {url} upstreams updated -> HTTP {r.status}")

        if sid:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{url}/api/auth", headers=headers, method="DELETE"), timeout=3).read()
            except Exception:
                pass

    # Verify external DNS resolution on all 3 Pi-hole nodes
    for ip in ["192.168.0.2", "192.168.0.5", "192.168.0.7"]:
        res = subprocess.run(["nslookup", "google.com.", ip], capture_output=True, text=True, timeout=5)
        print(f"\n=== nslookup google.com. @{ip} ===\n{res.stdout.strip()}")

if __name__ == "__main__":
    fix_pihole_upstreams()
