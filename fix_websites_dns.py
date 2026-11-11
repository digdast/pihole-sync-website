import json
import urllib.request
import paramiko

K3S_CP01 = "192.168.0.80"
USER = "plexadmin"
PW = "sone_420"

def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(K3S_CP01, username=USER, password=PW, timeout=10)

    def sudo(cmd):
        _, out, err = ssh.exec_command(f"echo {PW} | sudo -S bash -c '{cmd}'")
        return out.read().decode("utf-8", errors="replace").strip()

    print("=== [1/3] Updating CoreDNS in kube-system to forward via working Pi-hole 192.168.0.2 & 192.168.0.5 ===")
    coredns_cm = sudo("kubectl get configmap coredns -n kube-system -o json")
    cm = json.loads(coredns_cm)
    corefile = cm["data"]["Corefile"]
    # Replace forward . /etc/resolv.conf with forward . 192.168.0.2 192.168.0.5
    new_corefile = corefile.replace("forward . /etc/resolv.conf", "forward . 192.168.0.2 192.168.0.5")
    cm["data"]["Corefile"] = new_corefile
    sftp = ssh.open_sftp()
    with sftp.open("/tmp/coredns-cm.json", "w") as f:
        f.write(json.dumps(cm, indent=2))
    sftp.close()
    print(sudo("kubectl apply -f /tmp/coredns-cm.json && kubectl rollout restart deployment/coredns -n kube-system && kubectl rollout status deployment/coredns -n kube-system --timeout=30s"))

    print("\n=== [2/3] Restarting cloudflared in nginx-proxy-manager pod so it picks up working CoreDNS ===")
    print(sudo("kubectl rollout restart deployment/nginx-proxy-manager -n npm && kubectl rollout status deployment/nginx-proxy-manager -n npm --timeout=60s"))

    ssh.close()

    print("\n=== [3/3] Configuring Split-Horizon LAN DNS for *.ertechlab.com -> 192.168.0.80 on all 3 Pi-holes ===")
    dnsmasq_conf = (
        "# Split-Horizon LAN DNS for ERTECHLab & ERTECHHome\n"
        "address=/ertechlab.com/192.168.0.80\n"
        "address=/ertechhome.com/192.168.0.80\n"
        "address=/pihole.ertechhome.com/192.168.0.7\n"
        "address=/pihole-01.ertechhome.com/192.168.0.7\n"
        "address=/dns.ertechhome.com/192.168.0.7\n"
    )
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
                "misc": {
                    "etc_dnsmasq_d": True,
                    "dnsmasq_lines": [
                        "address=/ertechlab.com/192.168.0.80",
                        "address=/ertechhome.com/192.168.0.80",
                        "address=/pihole.ertechhome.com/192.168.0.7",
                        "address=/pihole-01.ertechhome.com/192.168.0.7",
                        "address=/dns.ertechhome.com/192.168.0.7",
                    ],
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
            print(f"  [OK] {url} split-horizon dnsmasq_lines configured -> HTTP {r.status}")
        if sid:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{url}/api/auth", headers=headers, method="DELETE"), timeout=3).read()
            except Exception:
                pass

if __name__ == "__main__":
    main()
