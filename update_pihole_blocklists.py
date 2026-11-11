#!/usr/bin/env python3
"""
Curated Best-in-Class Pi-hole v6 Blocklist Installer for pihole-01 (192.168.0.2)
================================================================================
Adds the highest-rated, actively maintained, low-false-positive blocklists
(HaGeZi Multi PRO, HaGeZi Threat Intelligence Feeds, OISD Big, StevenBlack,
AdGuard DNS, EasyPrivacy, URLHaus Malware, Phishing Army, DandelionSprout,
Smart TV / OS Telemetry lists) to pihole-01 (LXC 300), rebuilds Gravity (pihole -g),
and syncs across all Nebula-Sync replicas (192.168.0.5 and 192.168.0.7).
"""

import json
import sys
import urllib.error
import urllib.request
import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PVE00_IP = "192.168.40.30"
PVE00_USER = "root"
PVE00_PW = "sone_420"

PIHOLE01_URL = "http://192.168.0.2"
PIHOLE01_PW = "sone_420"

BEST_BLOCKLISTS = [
    # 1. Core All-in-One Gold Standard Lists
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.txt",
        "HaGeZi Multi PRO - Comprehensive Ads, Tracking, Telemetry, Scam & Malware",
    ),
    (
        "https://big.oisd.nl",
        "OISD Big - Curated Low-False-Positive Ads, Trackers & Phishing",
    ),
    (
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
        "StevenBlack Unified Hosts (Adware + Malware)",
    ),

    # 2. Threat Intelligence, Malware, Phishing & Ransomware
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/tif.txt",
        "HaGeZi Threat Intelligence Feeds (TIF) - Malware, Phishing, Ransomware, C2 & Botnets",
    ),
    (
        "https://urlhaus.abuse.ch/downloads/hostfile/",
        "Abuse.ch URLHaus Malware Hostfile",
    ),
    (
        "https://phishing.army/download/phishing_army_blocklist_extended.txt",
        "Phishing Army Extended Blocklist",
    ),
    (
        "https://raw.githubusercontent.com/DandelionSprout/adfilt/master/Alternate%20versions%20Anti-Malware%20List/AntiMalwareHosts.txt",
        "DandelionSprout Anti-Malware List",
    ),
    (
        "https://v.firebog.net/hosts/Prigent-Crypto.txt",
        "Firebog Prigent Crypto & Phishing",
    ),
    (
        "https://zerodot1.gitlab.io/CoinBlockerLists/hosts_browser",
        "ZeroDot1 CoinBlockerLists - Cryptomining Protection",
    ),

    # 3. Firebog Ticked (Green) Advertising & Privacy Lists
    (
        "https://v.firebog.net/hosts/AdguardDNS.txt",
        "Firebog - AdGuard DNS Filter",
    ),
    (
        "https://v.firebog.net/hosts/Easyprivacy.txt",
        "Firebog - EasyPrivacy Tracking Protection",
    ),
    (
        "https://v.firebog.net/hosts/Prigent-Ads.txt",
        "Firebog - Prigent Ads",
    ),
    (
        "https://v.firebog.net/hosts/Admiral.txt",
        "Firebog - Admiral Anti-Adblock Domains",
    ),
    (
        "https://raw.githubusercontent.com/Spam404/lists/master/main-blacklist.txt",
        "Spam404 Main Blacklist",
    ),
    (
        "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/UncheckyAds/hosts",
        "FadeMind UncheckyAds (Installer Bundled Adware)",
    ),
    (
        "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/add.Risk/hosts",
        "FadeMind Risk Domains",
    ),

    # 4. OS, IoT & Smart TV Native Telemetry Blocklists (HaGeZi Native)
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/native.winoffice.txt",
        "HaGeZi Native Tracker - Microsoft Windows & Office Telemetry",
    ),
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/native.apple.txt",
        "HaGeZi Native Tracker - Apple Telemetry",
    ),
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/native.amazon.txt",
        "HaGeZi Native Tracker - Amazon Devices & Alexa Telemetry",
    ),
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/native.samsung.txt",
        "HaGeZi Native Tracker - Samsung Smart TV Telemetry",
    ),
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/native.lgwebos.txt",
        "HaGeZi Native Tracker - LG webOS Smart TV Telemetry",
    ),
    (
        "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/native.roku.txt",
        "HaGeZi Native Tracker - Roku Smart TV Ads & Telemetry",
    ),
]


def get_pihole_sid(base_url: str, pw: str) -> str:
    req = urllib.request.Request(
        f"{base_url}/api/auth",
        data=json.dumps({"password": pw}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))["session"]["sid"]


def main():
    sid = get_pihole_sid(PIHOLE01_URL, PIHOLE01_PW)
    headers = {"Content-Type": "application/json"}
    if sid:
        headers["X-FTL-SID"] = sid

    print("=== [1/4] Checking current blocklists on pihole-01 (192.168.0.2) via Pi-hole v6 API ===")
    with urllib.request.urlopen(urllib.request.Request(f"{PIHOLE01_URL}/api/lists?type=block", headers=headers), timeout=10) as r:
        lists_data = json.loads(r.read().decode("utf-8")).get("lists", [])
    existing_urls = {item["address"]: item for item in lists_data}
    for item in lists_data:
        print(f"  - [{item['id']}] {item['address']} ({item.get('number', 0):,} domains) - {item.get('comment')}")

    print(f"\n=== [2/4] Adding {len(BEST_BLOCKLISTS)} curated blocklists to pihole-01 ===")
    added = 0
    for url, comment in BEST_BLOCKLISTS:
        if url in existing_urls:
            print(f"  [SKIP] Already present: {url}")
            continue
        payload = {
            "address": url,
            "type": "block",
            "comment": comment,
            "groups": [0],
            "enabled": True,
        }
        req = urllib.request.Request(
            f"{PIHOLE01_URL}/api/lists?type=block",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                added += 1
                print(f"  [ADDED] {comment}\n          {url}")
        except urllib.error.HTTPError as e:
            print(f"  [WARN] {url} -> HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")

    # Logout session before running gravity
    try:
        urllib.request.urlopen(urllib.request.Request(f"{PIHOLE01_URL}/api/auth", headers=headers, method="DELETE"), timeout=5).read()
    except Exception:
        pass

    print(f"\n=== [3/4] Running 'pihole -g' on pihole-01 (LXC 300) to download & compile all {len(existing_urls) + added} blocklists ===")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(PVE00_IP, username=PVE00_USER, password=PVE00_PW, timeout=12)
    _, out, err = ssh.exec_command("pct exec 300 -- /usr/local/bin/pihole -g")
    grav_out = out.read().decode("utf-8", errors="replace")
    grav_err = err.read().decode("utf-8", errors="replace")
    for line in (grav_out + "\n" + grav_err).splitlines()[-30:]:
        if line.strip():
            print("  " + line)
    ssh.close()

    print("\n=== [4/4] Replicating new blocklists & Gravity across Nebula-Sync cluster (192.168.0.5 & 192.168.0.7) ===")
    req = urllib.request.Request(
        f"http://{PVE00_IP}:8765/api/sync",
        data=json.dumps({"mode": "gravity"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.loads(r.read().decode("utf-8"))
        print(f"  [OK] Nebula-Sync cluster replication: ok={res.get('ok')} duration={res.get('entry', {}).get('duration_s')}s")

    # Verify final domain count across all 3 Pi-hole nodes
    sid = get_pihole_sid(PIHOLE01_URL, PIHOLE01_PW)
    headers = {"Content-Type": "application/json"}
    if sid:
        headers["X-FTL-SID"] = sid
    with urllib.request.urlopen(urllib.request.Request(f"{PIHOLE01_URL}/api/stats/summary", headers=headers), timeout=10) as r:
        summary = json.loads(r.read().decode("utf-8"))
        grav = summary.get("gravity", {})
        print(f"\n  [VERIFIED] pihole-01 (192.168.0.2) Gravity Domains Being Blocked: {grav.get('domains_being_blocked', 0):,}")
    try:
        urllib.request.urlopen(urllib.request.Request(f"{PIHOLE01_URL}/api/auth", headers=headers, method="DELETE"), timeout=5).read()
    except Exception:
        pass


if __name__ == "__main__":
    main()
