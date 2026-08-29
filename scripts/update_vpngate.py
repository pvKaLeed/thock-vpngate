#!/usr/bin/env python3
# filename: update_vpngate.py

import base64
import csv
import io
import json
import os
import re
import shutil
import sys
import socket
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# Configuration
# ============================================================

VPNGATE_URLS = [
    "https://www.vpngate.net/api/iphone/",
    "http://www.vpngate.net/api/iphone/",
]

VPNBOOK_NODES = [
    {"host": "us1.vpnbook.com", "ip": "198.27.70.18", "country": "US", "country_name": "United States"},
    {"host": "us2.vpnbook.com", "ip": "198.27.70.19", "country": "US", "country_name": "United States"},
    {"host": "ca199.vpnbook.com", "ip": "192.99.11.199", "country": "CA", "country_name": "Canada"},
    {"host": "fr1.vpnbook.com", "ip": "51.254.120.1", "country": "FR", "country_name": "France"},
    {"host": "de4.vpnbook.com", "ip": "178.33.109.118", "country": "DE", "country_name": "Germany"},
    {"host": "uk1.vpnbook.com", "ip": "151.80.32.223", "country": "GB", "country_name": "United Kingdom"},
]

CSV_OUTPUT = "data/servers.csv"
JSON_OUTPUT = "data/servers.json"
PROFILE_DIR = "data/profiles"

REQUEST_TIMEOUT = 15
TEST_TIMEOUT = 2.5  # Realtime Socket Test Timeout
MAX_WORKERS = 30    # Concurrent Threads for fast testing

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VPN-Realtime-Updater/3.0"


# ============================================================
# Utility
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def clean(value):
    return str(value).strip() if value is not None else ""

def to_float(value):
    try:
        return float(clean(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0

def to_int(value):
    try:
        return int(float(clean(value).replace(",", "")))
    except (ValueError, TypeError):
        return 0

def normalize_header(value):
    return clean(value).lstrip("#").strip()


# ============================================================
# Dynamic Password Scraper for VPNBook
# ============================================================

def get_vpnbook_credentials():
    """Scrape active password from VPNBook website."""
    default_pass = "vpnbook"
    try:
        res = requests.get("https://www.vpnbook.com/", timeout=10, headers={"User-Agent": USER_AGENT})
        if res.status_code == 200:
            match = re.search(r"Password:\s*<strong>([^<]+)</strong>", res.text, re.IGNORECASE)
            if match:
                return "vpnbook", match.group(1).strip()
    except Exception as e:
        print(f"⚠️ Could not scrape VPNBook password, using fallback: {e}")
    return "vpnbook", default_pass


# ============================================================
# Strict Realtime Connection Test
# ============================================================

def test_realtime_reachability(server):
    """
    Directly test if the server port is reachable in real-time using TCP socket.
    Servers that fail this test are STRICTLY REJECTED (No Fake Servers).
    """
    ip = server.get("ip") or server.get("hostname")
    port = server.get("port")

    if not ip or not port:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TEST_TIMEOUT)
        start_time = time.time()
        
        # Perform real socket connection test
        result = sock.connect_ex((ip, port))
        latency = (time.time() - start_time) * 1000.0
        sock.close()

        if result == 0:  # Port is OPEN and active right now
            server["ping_ms"] = round(latency, 2)
            return server
    except Exception:
        pass

    return None


def filter_only_active_servers(candidates):
    if not candidates:
        return []

    print(f"🔍 Testing {len(candidates)} server candidates in REAL-TIME...")
    verified_servers = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_realtime_reachability, s): s for s in candidates}
        for future in as_completed(futures):
            res = future.result()
            if res:
                verified_servers.append(res)
                print(f"  🟢 ACTIVE: {res['ip']}:{res['port']} [{res['country']}] - {res['ping_ms']}ms")

    print(f"✅ Real-time verified active servers: {len(verified_servers)} / {len(candidates)}")
    return verified_servers


# ============================================================
# Fetchers: VPN Gate & VPNBook
# ============================================================

def fetch_vpngate_servers():
    servers = []
    text = ""
    
    for url in VPNGATE_URLS:
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, verify=False)
            if r.status_code == 200 and "#HostName" in r.text:
                text = r.text
                break
        except Exception:
            continue

    if not text:
        print("⚠️ VPN Gate API unavailable.")
        return []

    lines = text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.startswith("#HostName")), None)
    if header_idx is None:
        return []

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    
    for raw in reader:
        row = {normalize_header(k): clean(v) for k, v in raw.items() if k}
        b64_config = row.get("OpenVPN_ConfigData_Base64")
        ip = row.get("IP")
        hostname = row.get("HostName")

        if not b64_config or not ip:
            continue

        try:
            profile_str = base64.b64decode(b64_config).decode("utf-8", errors="replace")
            port_match = re.search(r"(?m)^\s*remote\s+\S+\s+(\d+)", profile_str)
            proto_match = re.search(r"(?m)^\s*proto\s+(\S+)", profile_str)

            if not port_match:
                continue

            port = int(port_match.group(1))
            protocol = proto_match.group(1).lower() if proto_match else "tcp"
            protocol = "tcp" if "tcp" in protocol else "udp"

            servers.append({
                "source": "VPNGate",
                "id": f"vpngate_{ip}_{port}",
                "country": row.get("CountryShort", "UN"),
                "country_name": row.get("CountryLong", "Unknown"),
                "hostname": hostname,
                "ip": ip,
                "protocol": protocol,
                "port": port,
                "speed_mbps": round(to_float(row.get("Speed")) / 1_000_000.0, 2),
                "ping_ms": to_float(row.get("Ping")),
                "username": "vpn",
                "password": "vpn",
                "profile_content": profile_str,
            })
        except Exception:
            continue

    return servers


def fetch_vpnbook_servers():
    servers = []
    v_user, v_pass = get_vpnbook_credentials()
    
    # VPNBook OpenVPN profile template
    ovpn_template = (
        "client\n"
        "dev tun\n"
        "proto tcp\n"
        "remote {host} 443\n"
        "resolv-retry infinite\n"
        "nobind\n"
        "persist-key\n"
        "persist-tun\n"
        "cipher AES-256-CBC\n"
        "auth SHA256\n"
        "verb 3\n"
    )

    for node in VPNBOOK_NODES:
        config_data = ovpn_template.format(host=node["host"])
        servers.append({
            "source": "VPNBook",
            "id": f"vpnbook_{node['host']}_443",
            "country": node["country"],
            "country_name": node["country_name"],
            "hostname": node["host"],
            "ip": node["ip"],
            "protocol": "tcp",
            "port": 443,
            "speed_mbps": 10.0,
            "ping_ms": 100.0,
            "username": v_user,
            "password": v_pass,
            "profile_content": config_data,
        })

    return servers


# ============================================================
# Main Processor
# ============================================================

def main():
    print("========================================")
    print("🚀 REALTIME Multi-Source VPN Fetcher")
    print("========================================")

    # 1. Gather all candidates
    candidates = []
    
    vpngate_list = fetch_vpngate_servers()
    print(f"📥 Pulled {len(vpngate_list)} candidates from VPN Gate")
    candidates.extend(vpngate_list)

    vpnbook_list = fetch_vpnbook_servers()
    print(f"📥 Pulled {len(vpnbook_list)} candidates from VPNBook")
    candidates.extend(vpnbook_list)

    if not candidates:
        print("❌ No VPN candidates found across all sources.")
        return 1

    # 2. Strict Real-time Socket Connection Filter
    active_servers = filter_only_active_servers(candidates)

    if not active_servers:
        print("❌ CRITICAL: Zero active servers passed the real-time socket check!")
        return 1

    # 3. Clean up directory & Write Profiles
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    os.makedirs(PROFILE_DIR, exist_ok=True)

    final_server_list = []
    generated_at = utc_now()

    for s in active_servers:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", s["hostname"] or s["ip"])
        filename = f"{s['source'].lower()}_{safe_name}_{s['port']}_{s['protocol']}.ovpn"
        filepath = os.path.join(PROFILE_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(s["profile_content"])

        s["profile"] = f"profiles/{filename}"
        s["last_updated"] = generated_at
        del s["profile_content"]  # Clean up memory
        final_server_list.append(s)

    # Sort by lowest latency
    final_server_list.sort(key=lambda x: x["ping_ms"])

    # 4. Save CSV & JSON
    os.makedirs(os.path.dirname(CSV_OUTPUT), exist_ok=True)
    os.makedirs(os.path.dirname(JSON_OUTPUT), exist_ok=True)

    fields = [
        "id", "source", "country", "country_name", "hostname", "ip",
        "protocol", "port", "speed_mbps", "ping_ms", "username",
        "password", "profile", "last_updated"
    ]

    with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in final_server_list:
            writer.writerow({k: row.get(k, "") for k in fields})

    payload = {
        "version": 3,
        "generated_at": generated_at,
        "count": len(final_server_list),
        "servers": final_server_list,
    }

    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n🎉 SUCCESS: {len(final_server_list)} ACTIVE REAL-TIME SERVERS SAVED.")
    print(f"📁 CSV: {CSV_OUTPUT}")
    print(f"📁 JSON: {JSON_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
