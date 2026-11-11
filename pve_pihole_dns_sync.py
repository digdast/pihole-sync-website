#!/usr/bin/env python3
"""
Proxmox VE -> Pi-hole (192.168.0.2, 192.168.0.5, 192.168.0.7) Dynamic Guest DNS Auto-Injector
=============================================================================================
Scans all Proxmox VE nodes (/etc/pve/nodes/*/{qemu-server,lxc}/*.conf),
dynamically resolves each VM and LXC guest's hostname and IPv4 address
(via static PVE config, QEMU Guest Agent, LXC netns, DHCP leases, or ARP),
and injects them into Pi-hole while enforcing split-horizon DNS (A + NODATA for AAAA/HTTPS TYPE65)
so LAN browsers never receive Cloudflare ECH / IPv6 records for proxied domains.

Can run:
  1. Directly on Proxmox (e.g. via systemd .path + .timer on pve-00)
  2. Remotely from Windows/workstation via SSH (--remote 192.168.40.30)
"""

import argparse
import base64
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_DOMAIN = "ertech.home"
PREFERRED_SUBNET_PREFIX = "192.168.0."
CACHE_FILE = "/var/cache/pve-pihole-dns-state.json"

PROXIED_PUBLIC_DOMAINS = ("ertechlab.com", "ertechhome.com", "drumandrace.com")

REQUIRED_DNSMASQ_LINES = [
    "local=/ertechlab.com/",
    "local=/ertechhome.com/",
    "local=/drumandrace.com/",
    "local=/ertech.home/",
    "address=/ertechlab.com/192.168.0.80",
    "address=/ertechhome.com/192.168.0.80",
    "address=/drumandrace.com/192.168.0.80",
]

# Baseline static infrastructure & NPM reverse-proxy hosts that must ALWAYS resolve to their exact IPs
STATIC_INFRA_HOSTS = [
    # Proxmox VE cluster nodes (.ertech.home direct IPs)
    ("192.168.40.30", "pve-00.ertech.home"),
    ("192.168.40.30", "pve-cluster-01"),
    ("192.168.40.31", "pve-01.ertech.home"),
    ("192.168.40.32", "pve-02.ertech.home"),
    ("192.168.40.33", "pve-03.ertech.home"),
    ("192.168.40.34", "pve-04.ertech.home"),
    # Pi-hole DNS nodes (.ertech.home direct IPs)
    ("192.168.0.2", "pihole-01.ertech.home"),
    ("192.168.0.5", "pihole-02.ertech.home"),
    ("192.168.0.7", "pihole-03.ertech.home"),
    # All Nginx Proxy Manager (192.168.0.80) proxied domains
    ("192.168.0.80", "ertechlab.com"),
    ("192.168.0.80", "www.ertechlab.com"),
    ("192.168.0.80", "dashboard.ertechlab.com"),
    ("192.168.0.80", "aethergate.ertechlab.com"),
    ("192.168.0.80", "gateway.ertechlab.com"),
    ("192.168.0.80", "portainer.ertechlab.com"),
    ("192.168.0.80", "n8n.ertechlab.com"),
    ("192.168.0.80", "printforge.ertechlab.com"),
    ("192.168.0.80", "it-tools.ertechlab.com"),
    ("192.168.0.80", "tools.ertechlab.com"),
    ("192.168.0.80", "resume.ertechlab.com"),
    ("192.168.0.80", "ffmpeg.ertechlab.com"),
    ("192.168.0.80", "tomcat.ertechlab.com"),
    ("192.168.0.80", "pve-00.ertechlab.com"),
    ("192.168.0.80", "pve-01.ertechlab.com"),
    ("192.168.0.80", "pve-02.ertechlab.com"),
    ("192.168.0.80", "pve-03.ertechlab.com"),
    ("192.168.0.80", "pve-04.ertechlab.com"),
    ("192.168.0.80", "proxmox.ertechlab.com"),
    ("192.168.0.80", "pve.ertechlab.com"),
    ("192.168.0.80", "dns-01.ertechlab.com"),
    ("192.168.0.80", "dns.ertechlab.com"),
    ("192.168.0.80", "pihole-01.ertechlab.com"),
    ("192.168.0.80", "pihole-02.ertechlab.com"),
    ("192.168.0.80", "pihole.ertechlab.com"),
    ("192.168.0.80", "clip.ertechlab.com"),
    ("192.168.0.80", "paperclip-ollama.ertechlab.com"),
    ("192.168.0.80", "paperclip.ertechlab.com"),
    ("192.168.0.80", "peperclip.ertechlab.com"),
    ("192.168.0.80", "scripts.ertechlab.com"),
    ("192.168.0.80", "scriptshub.ertechlab.com"),
    ("192.168.0.80", "homelab-notes.ertechlab.com"),
    ("192.168.0.80", "homelab.ertechlab.com"),
    ("192.168.0.80", "notes.ertechlab.com"),
    ("192.168.0.80", "auth.ertechlab.com"),
    ("192.168.0.80", "idp.ertechlab.com"),
    ("192.168.0.80", "cad.ertechlab.com"),
    ("192.168.0.80", "forgecad.ertechlab.com"),
    ("192.168.0.80", "hermes-agent.ertechlab.com"),
    ("192.168.0.80", "hermes.ertechlab.com"),
    ("192.168.0.80", "npm-01.ertechlab.com"),
    ("192.168.0.80", "npm.ertechlab.com"),
    ("192.168.0.80", "hardware.ertechlab.com"),
    ("192.168.0.80", "database.ertechlab.com"),
    ("192.168.0.80", "db.ertechlab.com"),
    ("192.168.0.80", "grafana.ertechlab.com"),
    ("192.168.0.80", "prometheus.ertechlab.com"),
    ("192.168.0.80", "homelabber.ertechlab.com"),
    ("192.168.0.80", "homelabbing.ertechlab.com"),
    ("192.168.0.80", "omada-rtr.ertechlab.com"),
    ("192.168.0.80", "drumandrace.com"),
    ("192.168.0.80", "www.drumandrace.com"),
    ("192.168.0.80", "drumandrace.ertechlab.com"),
    ("192.168.0.80", "race.ertechlab.com"),
    ("192.168.0.80", "plexserver.ertechlab.com"),
    ("192.168.0.80", "arrstack.ertechlab.com"),
    ("192.168.0.80", "carshows.drumandrace.com"),
    ("192.168.0.80", "carshow.ertechlab.com"),
    ("192.168.0.80", "carshows-api.ertechlab.com"),
    ("192.168.0.80", "carshows.ertechlab.com"),
    ("192.168.0.80", "radar.ertechlab.com"),
    ("192.168.0.80", "ntfy.ertechlab.com"),
    ("192.168.0.80", "npm-01.ertechhome.com"),
    ("192.168.0.80", "npm-proxy-01.ertechhome.com"),
    ("192.168.0.80", "npm.ertechhome.com"),
    ("192.168.0.80", "dns.ertechhome.com"),
    ("192.168.0.80", "pihole-01.ertechhome.com"),
    ("192.168.0.80", "pihole.ertechhome.com"),
    ("192.168.0.80", "pihole-sync.ertech.home"),
    ("192.168.0.80", "pihole-sync.ertechhome.com"),
    ("192.168.0.80", "pihole-sync.ertechlab.com"),
]


def run_local(cmd: str, timeout: int = 15) -> str:
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def normalize_fqdn(raw_name: str, vmid: str = "") -> str:
    name = raw_name.strip().lower()
    if not name:
        return ""
    if str(vmid) == "302" or name == "pihole.ertechhome.com":
        return f"pihole-03.{DEFAULT_DOMAIN}"
    if "." not in name:
        return f"{name}.{DEFAULT_DOMAIN}"
    # Never allow a PVE container/VM hostname to hijack a proxied public domain
    for pub_dom in PROXIED_PUBLIC_DOMAINS:
        if name == pub_dom or name.endswith("." + pub_dom):
            short = name.split(".")[0]
            return f"{short}.{DEFAULT_DOMAIN}"
    return name


def parse_conf_text(content: str) -> dict:
    info = {
        "name": "",
        "ip": "",
        "mac": "",
        "template": False,
    }
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Stop at snapshot sections ([snapshot_name])
        if line.startswith("["):
            break
        if line.startswith("template:") and "1" in line.split(":", 1)[1]:
            info["template"] = True
        elif line.startswith("name:") and not info["name"]:
            info["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("hostname:") and not info["name"]:
            info["name"] = line.split(":", 1)[1].strip()
        elif re.match(r"^(ipconfig\d+|net\d+):", line):
            # Look for static IPv4 e.g. ip=192.168.0.84/24
            m_ip = re.search(r"\bip=(\d+\.\d+\.\d+\.\d+)(?:/\d+)?", line)
            if m_ip and m_ip.group(1) != "dhcp" and not info["ip"]:
                info["ip"] = m_ip.group(1)
            # Look for MAC address
            m_mac = re.search(r"(?:virtio|hwaddr|e1000|vmxnet3|rtl8139)=([0-9A-Fa-f:]{17})", line)
            if m_mac and not info["mac"]:
                info["mac"] = m_mac.group(1).lower()
    return info


def get_Dynamic_ip_on_pve(node: str, guest_type: str, vmid: str, mac: str, local_node: str) -> str:
    """Resolve IP for a running guest that has no static IP in its PVE config."""
    ssh_prefix = "" if node == local_node else f"ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=no {node} "

    if guest_type == "qemu":
        out = run_local(f"{ssh_prefix}qm guest cmd {vmid} network-get-interfaces", timeout=8)
        if out:
            try:
                ifaces = json.loads(out)
                fallback_ip = ""
                for iface in ifaces:
                    ifname = iface.get("name", "")
                    if ifname == "lo" or ifname.startswith(("wg", "tun", "tap", "docker", "cni", "flannel", "veth")):
                        continue
                    for addr in iface.get("ip-addresses", []):
                        if addr.get("ip-address-type") == "ipv4":
                            ip = addr.get("ip-address", "")
                            if ip.startswith(PREFERRED_SUBNET_PREFIX):
                                return ip
                            if not ip.startswith("127.") and not fallback_ip:
                                fallback_ip = ip
                if fallback_ip:
                    return fallback_ip
            except Exception:
                pass
    elif guest_type == "lxc":
        out = run_local(f"{ssh_prefix}pct exec {vmid} -- ip -4 -o addr show scope global", timeout=6)
        if out:
            for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/\d+", out):
                ip = m.group(1)
                if ip.startswith(PREFERRED_SUBNET_PREFIX):
                    return ip

    # Fallback: match MAC in ARP table or Pi-hole dhcp.leases
    if mac:
        arp_out = run_local(f"ip -4 neigh show | grep -i '{mac}'")
        if arp_out:
            m_arp = re.search(r"^(\d+\.\d+\.\d+\.\d+)", arp_out)
            if m_arp:
                return m_arp.group(1)
    return ""


def collect_pve_guests_local() -> list:
    """Run directly on a PVE node where /etc/pve is mounted."""
    local_node = run_local("hostname").split(".")[0]
    guests = []

    conf_paths = sorted(
        glob.glob("/etc/pve/nodes/*/lxc/*.conf")
        + glob.glob("/etc/pve/nodes/*/qemu-server/*.conf")
    )

    for path in conf_paths:
        parts = Path(path).parts
        # /etc/pve/nodes/<node>/<type>/<vmid>.conf
        node = parts[4]
        gtype = "lxc" if parts[5] == "lxc" else "qemu"
        vmid = Path(path).stem

        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        info = parse_conf_text(content)
        if info["template"] or not info["name"]:
            continue

        fqdn = normalize_fqdn(info["name"], vmid=vmid)
        ip = info["ip"]
        if not ip:
            ip = get_Dynamic_ip_on_pve(node, gtype, vmid, info["mac"], local_node)

        if ip and fqdn:
            guests.append({
                "vmid": int(vmid) if vmid.isdigit() else vmid,
                "type": gtype,
                "node": node,
                "fqdn": fqdn,
                "ip": ip,
            })

    guests.sort(key=lambda g: (g["ip"], g["fqdn"]))
    return guests


def update_pihole_instance(exec_prefix: str, guests: list, prev_guest_fqdns: set = None, is_oci_container: bool = False) -> bool:
    """
    Injects discovered PVE guest records into Pi-hole's /etc/pihole/pihole.toml (dns.hosts),
    enforces split-horizon dnsmasq_lines + /etc/dnsmasq.d/99-split-horizon.conf,
    and updates /etc/pihole/custom.list while preserving existing non-guest custom records.
    Returns True if changes were applied.
    """
    toml_text = run_local(f"{exec_prefix}cat /etc/pihole/pihole.toml", timeout=15)
    if not toml_text:
        return False

    m = re.search(r"(  hosts = \[)(.*?)(\])", toml_text, flags=re.DOTALL)
    if not m:
        return False

    existing_raw = [
        line.strip().strip(",").strip('"')
        for line in m.group(2).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    # Map of guest FQDNs and shortnames managed by PVE (current + previously synced)
    guest_fqdns = {g["fqdn"].lower() for g in guests}
    if prev_guest_fqdns:
        guest_fqdns |= {h.lower() for h in prev_guest_fqdns}
    guest_shortnames = {f.split(".")[0].lower() for f in guest_fqdns}
    infra_fqdns = {h.lower() for _, h in STATIC_INFRA_HOSTS}

    preserved_entries = []
    seen = set()
    seen_hosts = set()

    # 1. Keep user's custom static entries that aren't PVE guests or static infra hosts
    for entry in existing_raw:
        parts = entry.split()
        if len(parts) < 2:
            continue
        ip, host = parts[0], parts[1].lower()
        if host in guest_fqdns or host in guest_shortnames or host in infra_fqdns:
            continue
        key = (ip, host)
        if key not in seen and host not in seen_hosts:
            seen.add(key)
            seen_hosts.add(host)
            preserved_entries.append(f"{ip} {host}")

    # 2. Add static PVE infrastructure & NPM proxy hosts (authoritative priority)
    final_entries = list(preserved_entries)
    for ip, host in STATIC_INFRA_HOSTS:
        h_low = host.lower()
        key = (ip, h_low)
        if key not in seen and h_low not in seen_hosts:
            seen.add(key)
            seen_hosts.add(h_low)
            final_entries.append(f"{ip} {h_low}")

    # 3. Add all dynamically discovered PVE VMs & LXCs (never overriding STATIC_INFRA_HOSTS)
    for g in sorted(guests, key=lambda x: tuple(int(p) for p in x["ip"].split("."))):
        ip = g["ip"]
        fqdn = g["fqdn"].lower()
        key = (ip, fqdn)
        if key not in seen and fqdn not in seen_hosts:
            seen.add(key)
            seen_hosts.add(fqdn)
            final_entries.append(f"{ip} {fqdn}")

    formatted_array = "\n" + ",\n".join(f'    "{e}"' for e in final_entries) + "\n  "
    new_toml = toml_text[: m.start(1)] + "  hosts = [" + formatted_array + "]" + toml_text[m.end(3) :]

    # Enforce split-horizon dnsmasq_lines in [misc] so TYPE65 (HTTPS/ECH) & AAAA never leak to Cloudflare
    dnsmasq_formatted = "\n" + ",\n".join(f'    "{l}"' for l in REQUIRED_DNSMASQ_LINES) + "\n  "
    m_dl = re.search(r"(  dnsmasq_lines = \[)(.*?)(\])", new_toml, flags=re.DOTALL)
    if m_dl:
        new_toml = new_toml[: m_dl.start(1)] + "  dnsmasq_lines = [" + dnsmasq_formatted + "]" + new_toml[m_dl.end(3) :]

    # Also ensure etc_dnsmasq_d = true
    new_toml = re.sub(r"(\betc_dnsmasq_d\s*=\s*)false\b", r"\1true", new_toml)

    dnsmasq_conf_text = "\n".join(REQUIRED_DNSMASQ_LINES) + "\n"
    existing_dnsmasq_conf = run_local(f"{exec_prefix}cat /etc/dnsmasq.d/99-split-horizon.conf", timeout=10)

    if new_toml == toml_text and existing_dnsmasq_conf.strip() == dnsmasq_conf_text.strip():
        return False

    custom_list_text = "\n".join(final_entries) + "\n"
    b64_custom = base64.b64encode(custom_list_text.encode("utf-8")).decode("ascii")
    b64_dnsmasq = base64.b64encode(dnsmasq_conf_text.encode("utf-8")).decode("ascii")
    hosts_json_b64 = base64.b64encode(json.dumps(final_entries).encode("utf-8")).decode("ascii")
    dnsmasq_json_b64 = base64.b64encode(json.dumps(REQUIRED_DNSMASQ_LINES).encode("utf-8")).decode("ascii")

    inner_script = (
        f"mkdir -p /etc/dnsmasq.d /etc/pihole/hosts\n"
        f"echo {b64_dnsmasq} | base64 -d > /etc/dnsmasq.d/99-split-horizon.conf\n"
        f"echo {b64_custom} | base64 -d > /etc/pihole/custom.list\n"
        f"echo {b64_custom} | base64 -d > /etc/pihole/hosts/custom.list\n"
        f"chown pihole:pihole /etc/pihole/custom.list /etc/pihole/hosts/custom.list 2>/dev/null || true\n"
        f"pihole-FTL --config webserver.api.allow_destructive true >/dev/null 2>&1 || true\n"
        f"pihole-FTL --config misc.etc_dnsmasq_d true >/dev/null 2>&1 || true\n"
        f"pihole-FTL --config misc.dnsmasq_lines \"$(echo {dnsmasq_json_b64} | base64 -d)\" >/dev/null 2>&1 || true\n"
        f"pihole-FTL --config dns.hosts \"$(echo {hosts_json_b64} | base64 -d)\" >/dev/null 2>&1 || true\n"
    )
    if is_oci_container:
        inner_script += "sed -i 's/pihole.ertechhome.com/pihole-03.ertech.home/g' /etc/hosts 2>/dev/null || true\n"
        inner_script += "hostname pihole-03 2>/dev/null || true\n"

    b64_script = base64.b64encode(inner_script.encode("utf-8")).decode("ascii")
    apply_cmd = f"echo {b64_script} | base64 -d | {exec_prefix}sh"
    run_local(apply_cmd, timeout=25)
    return True


def sync_on_pve(force: bool = False, quiet: bool = False, no_nebula: bool = False) -> int:
    guests = collect_pve_guests_local()
    state_obj = {
        "guests": guests,
        "infra_count": len(STATIC_INFRA_HOSTS),
        "dnsmasq_lines": REQUIRED_DNSMASQ_LINES,
    }
    payload_hash = hashlib.sha256(json.dumps(state_obj, sort_keys=True).encode("utf-8")).hexdigest()

    prev_guest_fqdns = set()
    if os.path.exists(CACHE_FILE):
        try:
            cached = json.loads(Path(CACHE_FILE).read_text(encoding="utf-8"))
            for cg in cached.get("guests", []):
                if cg.get("fqdn"):
                    prev_guest_fqdns.add(cg["fqdn"].lower())
            if not force and cached.get("hash") == payload_hash:
                if not quiet:
                    print(f"[OK] No PVE guest DNS changes detected ({len(guests)} guests up to date).")
                return 0
        except Exception:
            pass

    local_node = run_local("hostname").split(".")[0]

    # Primary Pi-hole: 192.168.0.2 (LXC 300 on pve-00)
    pihole1_prefix = "pct exec 300 -- " if local_node == "pve-00" else "ssh -o BatchMode=yes -o StrictHostKeyChecking=no pve-00 pct exec 300 -- "
    changed_1 = update_pihole_instance(pihole1_prefix, guests, prev_guest_fqdns, is_oci_container=False)

    # Secondary Pi-hole: 192.168.0.5 (LXC 301 on pve-04)
    pihole2_prefix = "pct exec 301 -- " if local_node == "pve-04" else "ssh -o BatchMode=yes -o StrictHostKeyChecking=no pve-04 pct exec 301 -- "
    changed_2 = update_pihole_instance(pihole2_prefix, guests, prev_guest_fqdns, is_oci_container=False)

    # Tertiary Pi-hole: 192.168.0.7 (LXC 302 on pve-04)
    pihole3_prefix = "pct exec 302 -- " if local_node == "pve-04" else "ssh -o BatchMode=yes -o StrictHostKeyChecking=no pve-04 pct exec 302 -- "
    changed_3 = update_pihole_instance(pihole3_prefix, guests, prev_guest_fqdns, is_oci_container=True)

    # Trigger fast Nebula-Sync config/DNS replication to all replicas if Primary changed
    if (changed_1 or force) and not no_nebula:
        run_local(
            f"{pihole1_prefix}bash -c 'grep -vE \"^(CRON|RUN_GRAVITY)=\" /opt/nebula-sync/nebula-sync.env > /tmp/nebula-oneshot.env && echo \"RUN_GRAVITY=false\" >> /tmp/nebula-oneshot.env && /opt/nebula-sync/nebula-sync run --env-file /tmp/nebula-oneshot.env'",
            timeout=25,
        )

    try:
        Path(CACHE_FILE).write_text(
            json.dumps({"hash": payload_hash, "guests": guests}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    if not quiet or changed_1 or changed_2 or changed_3:
        print(
            f"[SYNCED] {len(guests)} PVE guests + {len(STATIC_INFRA_HOSTS)} infra records synced to Pi-hole "
            f"192.168.0.2 (changed={changed_1}), 192.168.0.5 (changed={changed_2}), 192.168.0.7 (changed={changed_3}):"
        )
        for g in guests:
            print(f"  - [{g['type'].upper():4s} {str(g['vmid']):4s} @ {g['node']}] {g['ip']:15s} -> {g['fqdn']}")
    return 0


def run_remote_from_workstation(host: str, user: str, password: str, force: bool) -> int:
    import paramiko

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, timeout=10)
    flag = "--force" if force else ""
    _, stdout, stderr = ssh.exec_command(f"python3 /usr/local/bin/pve-pihole-dns-sync.py {flag}")
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    ssh.close()
    if out:
        print(out.strip())
    if err:
        print(err.strip(), file=sys.stderr)
    return code


def main():
    parser = argparse.ArgumentParser(description="Dynamically sync Proxmox VMs and LXCs to Pi-hole local DNS.")
    parser.add_argument("--force", action="store_true", help="Force Pi-hole verification/write even if state cache matches.")
    parser.add_argument("--quiet", action="store_true", help="Only log when DNS records actually change.")
    parser.add_argument("--no-nebula", action="store_true", help="Skip inline Nebula-Sync trigger (used when caller runs Nebula-Sync separately).")
    parser.add_argument("--remote", default="", help="Run via SSH on the specified PVE host (e.g. 192.168.40.30).")
    parser.add_argument("--user", default="root", help="SSH username when using --remote.")
    parser.add_argument("--password", default="sone_420", help="SSH password when using --remote.")
    args = parser.parse_args()

    if args.remote or sys.platform == "win32":
        target = args.remote or "192.168.40.30"
        sys.exit(run_remote_from_workstation(target, args.user, args.password, args.force))
    else:
        sys.exit(sync_on_pve(force=args.force, quiet=args.quiet, no_nebula=args.no_nebula))


if __name__ == "__main__":
    main()
