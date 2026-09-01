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


# ============================================================
# Configuration
# ============================================================
//SOURCE_URL = "https://www.vpngate.net/api/iphone/"

SOURCE_URL = "https://github.com/9xN/auto-ovpn?utm_source=chatgpt.com"
SOURCE_URL = "https://github.com/6Kmfi6HP/vpngate-meridian?utm_source=chatgpt.com"
//https://github.com/9xN/auto-ovpn?utm_source=chatgpt.com
//https://github.com/6Kmfi6HP/vpngate-meridian?utm_source=chatgpt.com

CSV_OUTPUT = "data/servers.csv"
JSON_OUTPUT = "data/servers.json"
PROFILE_DIR = "data/profiles"

VPN_USERNAME = "vpn"
VPN_PASSWORD = "vpn"

REQUEST_TIMEOUT = 30

# Maximum number of servers published.
MAX_SERVERS = 300

# Minimum acceptable server speed.
MIN_SPEED_MBPS = 1.0

# Maximum acceptable ping.
MAX_PING_MS = 1500.0

# ============================================================
# Priority Countries & Server Testing Configuration
# ============================================================

PRIORITY_COUNTRIES = ["US", "CA", "NL", "SG", "DE"]

MIN_SERVERS_PER_COUNTRY = 3
MAX_SERVERS_PER_COUNTRY = 10

# Server testing configuration
TEST_TIMEOUT = 3  # seconds
MAX_WORKERS = 20  # parallel testing threads

USER_AGENT = (
    "THOCK-VPNGate-Updater/2.1 "
    "(GitHub Actions)"
)


# ============================================================
# Utility
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def to_float(value):
    try:
        value = clean(value)
        if not value:
            return 0.0
        return float(value.replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def to_int(value):
    try:
        value = clean(value)
        if not value:
            return 0
        return int(float(value.replace(",", "")))
    except (ValueError, TypeError):
        return 0


def normalize_header(value):
    return clean(value).lstrip("#").strip()


# ============================================================
# Server Testing Functions
# ============================================================

def test_server_connection(server):
    """Test if a server is actually reachable (TCP or UDP)"""
    try:
        ip = server.get("ip", "")
        port = server.get("port", 0)
        protocol = server.get("protocol", "tcp")
        
        if not ip or not port:
            return None
            
        start_time = time.time()
        
        # TCP connection test
        if protocol == "tcp":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(TEST_TIMEOUT)
                result = sock.connect_ex((ip, port))
                if result == 0:
                    server["response_time_ms"] = round((time.time() - start_time) * 1000, 2)
                    return server
                    
        # UDP connection test
        elif protocol == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(TEST_TIMEOUT)
                sock.connect((ip, port))
                server["response_time_ms"] = round((time.time() - start_time) * 1000, 2)
                return server
                
    except Exception:
        pass
        
    return None


def filter_active_servers(servers, max_workers=MAX_WORKERS):
    """Filter servers by actual connection test"""
    if not servers:
        return []
        
    active_servers = []
    tested_count = 0
    
    print(f"🔍 Testing {len(servers)} servers for availability...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_server = {
            executor.submit(test_server_connection, server): server 
            for server in servers
        }
        
        for future in as_completed(future_to_server):
            tested_count += 1
            result = future.result()
            if result:
                active_servers.append(result)
                print(f"✅ Active: {result.get('ip')}:{result.get('port')} ({result.get('country')}) [{result.get('protocol').upper()}]")
            
            if tested_count % 10 == 0:
                print(f"⏳ Progress: {tested_count}/{len(servers)}")
    
    print(f"📊 Active servers found: {len(active_servers)}/{len(servers)}")
    return active_servers


def filter_servers_by_country(servers):
    """ဦးစားပေးနိုင်ငံများကို အရင်ရွေးပြီး ကျန်တာကို နောက်မှထည့်သည့်စနစ်"""
    if not servers:
        return []
        
    priority_servers = []
    other_servers = []
    
    country_groups = {}
    for server in servers:
        country = server.get("country", "")
        if country not in country_groups:
            country_groups[country] = []
        country_groups[country].append(server)
    
    for country in PRIORITY_COUNTRIES:
        if country in country_groups:
            sorted_servers = sorted(
                country_groups[country], 
                key=lambda x: x.get("score", 0), 
                reverse=True
            )
            selected = sorted_servers[:MAX_SERVERS_PER_COUNTRY]
            priority_servers.extend(selected)
            print(f"✅ {country}: {len(selected)} servers selected")
    
    for country, server_list in country_groups.items():
        if country not in PRIORITY_COUNTRIES:
            sorted_servers = sorted(
                server_list, 
                key=lambda x: x.get("score", 0), 
                reverse=True
            )
            other_servers.extend(sorted_servers[:3])
    
    total_servers = priority_servers + other_servers
    print(f"📊 Total servers selected: {len(total_servers)}")
    
    return total_servers


# ============================================================
# VPN Gate API
# ============================================================

def download_source():
    print("Downloading VPN Gate server list...")
    response = requests.get(
        SOURCE_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    text = response.content.decode("utf-8", errors="replace")
    if "#HostName" not in text:
        raise RuntimeError("VPN Gate API returned an unexpected response.")
    return text


def parse_source(text):
    lines = text.splitlines()
    header_index = None
    for index, line in enumerate(lines):
        if line.startswith("#HostName"):
            header_index = index
            break
    if header_index is None:
        raise RuntimeError("VPN Gate CSV header was not found.")
    csv_text = "\n".join(lines[header_index:])
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for raw in reader:
        row = {}
        for key, value in raw.items():
            row[normalize_header(key)] = clean(value)
        rows.append(row)
    return rows


# ============================================================
# OpenVPN Profile
# ============================================================

def decode_profile(row):
    encoded = clean(row.get("OpenVPN_ConfigData_Base64"))
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=False)
        profile = decoded.decode("utf-8", errors="replace")
        if "client" not in profile:
            return None
        if "remote " not in profile:
            return None
        return profile.strip() + "\n"
    except Exception as exc:
        print("Profile decode failed:", exc)
        return None


def get_remote(profile):
    if not profile:
        return None
    match = re.search(r"(?m)^\s*remote\s+(\S+)\s+(\d+)", profile)
    if not match:
        return None
    return {"host": match.group(1), "port": int(match.group(2))}


def get_protocol(profile):
    if not profile:
        return ""
    match = re.search(r"(?m)^\s*proto\s+(\S+)", profile)
    if not match:
        return ""
    return match.group(1).lower()


def validate_profile(profile):
    if not profile:
        return False
    required = ["client", "dev tun", "remote ", "proto "]
    return all(item in profile for item in required)


# ============================================================
# File names
# ============================================================

def safe_filename(value):
    value = clean(value)
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    value = value.strip("._-")
    if not value:
        value = "server"
    return value[:100]


# ============================================================
# Server score
# ============================================================

def calculate_score(speed_mbps, ping_ms, uptime_days, sessions):
    speed_score = min(speed_mbps / 1000.0, 1.0)
    if ping_ms <= 0:
        ping_score = 0.5
    else:
        ping_score = max(0.0, 1.0 - (min(ping_ms, MAX_PING_MS) / MAX_PING_MS))
    uptime_score = min(uptime_days / 30.0, 1.0)
    load_score = 1.0 / (1.0 + (sessions / 100.0))
    score = (speed_score * 0.45 + ping_score * 0.30 + uptime_score * 0.15 + load_score * 0.10)
    return round(score * 1000, 2)


# ============================================================
# Server processing
# ============================================================

def process_servers(rows):
    servers = []
    seen = set()
    generated_at = utc_now()

    for row in rows:
        hostname = clean(row.get("HostName"))
        ip = clean(row.get("IP"))
        if not hostname and not ip:
            continue

        speed_bps = to_float(row.get("Speed"))
        speed_mbps = speed_bps / 1_000_000.0
        ping_ms = to_float(row.get("Ping"))
        uptime_seconds = to_float(row.get("Uptime"))
        uptime_days = uptime_seconds / 86400.0

        if speed_mbps < MIN_SPEED_MBPS:
            continue
        if ping_ms <= 0:
            continue
        if ping_ms > MAX_PING_MS:
            continue

        profile = decode_profile(row)
        if not validate_profile(profile):
            continue

        remote = get_remote(profile)
        if not remote:
            continue

        protocol = get_protocol(profile)
        if not protocol:
            continue
        if protocol == "tcp-client":
            protocol = "tcp"

        server_id = f"{ip or hostname}:{remote['port']}:{protocol}"
        if server_id in seen:
            continue
        seen.add(server_id)

        sessions = to_int(row.get("NumVpnSessions"))
        score = calculate_score(
            speed_mbps=speed_mbps,
            ping_ms=ping_ms,
            uptime_days=uptime_days,
            sessions=sessions,
        )

        country_short = clean(row.get("CountryShort"))
        country_long = clean(row.get("CountryLong"))

        server = {
            "id": server_id,
            "country": country_short,
            "country_name": country_long,
            "hostname": hostname,
            "ip": ip,
            "protocol": protocol,
            "port": remote["port"],
            "tcp_port": to_int(row.get("TcpPort")),
            "udp_port": to_int(row.get("UdpPort")),
            "speed_mbps": round(speed_mbps, 2),
            "ping_ms": round(ping_ms, 2),
            "sessions": sessions,
            "uptime_days": round(uptime_days, 2),
            "score": score,
            "username": VPN_USERNAME,
            "password": VPN_PASSWORD,
            "profile": "",  # Will be generated for final active servers
            "raw_profile": profile,
            "last_updated": generated_at,
        }

        servers.append(server)

    # Sort by score
    servers.sort(key=lambda item: (-item["score"], item["ping_ms"], -item["speed_mbps"], item["sessions"]))

    return servers[:MAX_SERVERS]


# ============================================================
# File Generator Functions
# ============================================================

def write_profiles(servers):
    """ Write .ovpn profile files ONLY for final active servers """
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    
    for server in servers:
        profile_data = server.get("raw_profile")
        if not profile_data:
            continue
            
        filename = (
            safe_filename(server.get("hostname") or server.get("ip"))
            + "_"
            + str(server.get("port"))
            + "_"
            + server.get("protocol")
            + ".ovpn"
        )
        
        profile_path = os.path.join(PROFILE_DIR, filename)
        with open(profile_path, "w", encoding="utf-8") as file:
            file.write(profile_data)
            
        server["profile"] = "profiles/" + filename


def write_csv(servers):
    os.makedirs(os.path.dirname(CSV_OUTPUT), exist_ok=True)
    
    fields = [
        "id", "country", "country_name", "hostname", "ip", "protocol",
        "port", "tcp_port", "udp_port", "speed_mbps", "ping_ms", "sessions",
        "uptime_days", "score", "username", "password", "profile", "last_updated"
    ]
    
    filtered_servers = []
    for server in servers:
        filtered_server = {key: server.get(key, "") for key in fields}
        filtered_servers.append(filtered_server)
    
    with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(filtered_servers)


def write_json(servers):
    os.makedirs(os.path.dirname(JSON_OUTPUT), exist_ok=True)
    
    fields_to_exclude = {"response_time_ms", "raw_profile"}
    clean_servers = []
    for server in servers:
        clean_server = {k: v for k, v in server.items() if k not in fields_to_exclude}
        clean_servers.append(clean_server)
    
    payload = {
        "version": 2,
        "generated_at": utc_now(),
        "source": "VPN Gate",
        "count": len(clean_servers),
        "servers": clean_servers,
    }
    with open(JSON_OUTPUT, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
        file.write("\n")


# ============================================================
# Main
# ============================================================

def main():
    try:
        print("========================================")
        print("THOCK VPN Gate Updater v2.1 (Active Server Filter)")
        print("========================================")

        # Download and process
        source = download_source()
        rows = parse_source(source)
        print(f"Source servers: {len(rows)}")

        servers = process_servers(rows)
        print(f"Valid OpenVPN servers: {len(servers)}")

        if not servers:
            raise RuntimeError("No valid OpenVPN servers found.")

        # Filter by priority countries
        filtered_servers = filter_servers_by_country(servers)
        print(f"After country filter: {len(filtered_servers)}")

        if not filtered_servers:
            raise RuntimeError("No servers after country filter!")

        # Test server availability
        active_servers = filter_active_servers(filtered_servers)

        if not active_servers:
            print("⚠️ No active servers found! Saving filtered list anyway.")
            active_servers = filtered_servers[:10]

        # Write output files (Profiles, CSV, JSON)
        write_profiles(active_servers)
        write_csv(active_servers)
        write_json(active_servers)

        print(f"✅ Created profiles: {PROFILE_DIR}")
        print(f"✅ Created: {CSV_OUTPUT}")
        print(f"✅ Created: {JSON_OUTPUT}")
        print("✅ Update completed successfully.")
        
        return 0

    except Exception as exc:
        print(f"❌ ERROR: {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
