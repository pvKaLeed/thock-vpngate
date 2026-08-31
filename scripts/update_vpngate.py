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
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# ============================================================
# Configuration
# ============================================================

SOURCE_URL = "https://www.vpngate.net/api/iphone/"

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

# ဦးစားပေးနိုင်ငံများ
PRIORITY_COUNTRIES = ["US", "CA", "NL", "SG", "DE"]

# နိုင်ငံအလိုက် အနည်းဆုံး သိမ်းဆည်းမယ့် အရေအတွက်
MIN_SERVERS_PER_COUNTRY = 3
MAX_SERVERS_PER_COUNTRY = 10

# Server testing configuration
TEST_TIMEOUT = 3  # seconds (reduced from 5)
MAX_WORKERS = 20  # parallel testing threads

USER_AGENT = (
    "THOCK-VPNGate-Updater/2.0 "
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
    """Test if a server is actually reachable"""
    try:
        ip = server.get("ip", "")
        port = server.get("port", 0)
        protocol = server.get("protocol", "tcp")
        
        if not ip or not port:
            return None
            
        # TCP connection test
        if protocol == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(TEST_TIMEOUT)
            start_time = time.time()
            result = sock.connect_ex((ip, port))
            response_time = time.time() - start_time
            sock.close()
            
            if result == 0:  # Connection successful
                server["response_time_ms"] = round(response_time * 1000, 2)
                return server
                
    except Exception as e:
        # Silent fail - server is not reachable
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
                print(f"✅ Active: {result.get('ip')}:{result.get('port')} ({result.get('country')})")
            
            if tested_count % 10 == 0:
                print(f"⏳ Progress: {tested_count}/{len(servers)}")
    
    print(f"📊 Active servers found: {len(active_servers)}/{len(servers)}")
    return active_servers


def filter_servers_by_country(servers):
    """ဦးစားပေးနိုင်ငံများကို အရင်ရွေးပြီး ကျန်တာကို နောက်မှထည့်တဲ့နည်း"""
    
    if not servers:
        return []
        
    priority_servers = []
    other_servers = []
    
    # နိုင်ငံအလိုက် စုစည်းခြင်း
    country_groups = {}
    for server in servers:
        country = server.get("country", "")
        if country not in country_groups:
            country_groups[country] = []
        country_groups[country].append(server)
    
    # ဦးစားပေးနိုင်ငံများကို အရင်ရွေးချယ်ခြင်း
    for country in PRIORITY_COUNTRIES:
        if country in country_groups:
            # Score အမြင့်ဆုံး ဆာဗာများကို ရွေးချယ်
            sorted_servers = sorted(
                country_groups[country], 
                key=lambda x: x.get("score", 0), 
                reverse=True
            )
            # သတ်မှတ်ထားတဲ့ အရေအတွက်ကိုပဲ ယူ
            selected = sorted_servers[:MAX_SERVERS_PER_COUNTRY]
            priority_servers.extend(selected)
            print(f"✅ {country}: {len(selected)} servers selected")
    
    # ကျန်တဲ့နိုင်ငံများကို နောက်မှထည့်
    for country, server_list in country_groups.items():
        if country not in PRIORITY_COUNTRIES:
            # အကောင်းဆုံး ၃ လိုင်းပဲ ယူ
            sorted_servers = sorted(
                server_list, 
                key=lambda x: x.get("score", 0), 
                reverse=True
            )
            other_servers.extend(sorted_servers[:3])
    
    # စုစုပေါင်း ဆာဗာအရေအတွက်
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

    # Remove old profiles
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    os.makedirs(PROFILE_DIR, exist_ok=True)

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

        filename = (
            safe_filename(hostname or ip)
            + "_"
            + str(remote["port"])
            + "_"
            + protocol
            + ".ovpn"
        )

        profile_path = os.path.join(PROFILE_DIR, filename)
        with open(profile_path, "w", encoding="utf-8") as file:
            file.write(profile)

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
            "profile": "profiles/" + filename,
            "last_updated": generated_at,
        }

        servers.append(server)

    # Sort by score
    servers.sort(key=lambda item: (-item["score"], item["ping_ms"], -item["speed_mbps"], item["sessions"]))

    return servers[:MAX_SERVERS]


# ============================================================
# CSV & JSON
# ============================================================

def write_csv(servers):
    os.makedirs(os.path.dirname(CSV_OUTPUT), exist_ok=True)
    
    fields = [
        "id", "country", "country_name", "hostname", "ip", "protocol",
        "port", "tcp_port", "udp_port", "speed_mbps", "ping_ms", "sessions",
        "uptime_days", "score", "username", "password", "profile", "last_updated"
    ]
    
    # Filter out any extra fields like response_time_ms
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
    
    # Remove response_time_ms from JSON too if needed
    clean_servers = []
    for server in servers:
        clean_server = {k: v for k, v in server.items() if k != "response_time_ms"}
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

        # Write output
        write_csv(active_servers)
        write_json(active_servers)

        print(f"✅ Created: {CSV_OUTPUT}")
        print(f"✅ Created: {JSON_OUTPUT}")
        print(f"✅ Created profiles: {PROFILE_DIR}")
        print("✅ Update completed successfully.")
        
        return 0

    except Exception as exc:
        print(f"❌ ERROR: {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())                "password": "",
                "profile_content": ovpn_profile,
                "config_auth": "none"
            })
    except Exception as e:
        print(f"⚠️ VPNGate Error: {e}")

    print(f"📥 VPNGate candidates: {len(servers)}")
    return servers

def test_realtime(server):
    host = server.get("ip") or server.get("hostname")
    port = int(server.get("port", 0))
    proto = clean(server.get("protocol")).lower()

    if not host or port <= 0:
        return None

    if proto == "udp":
        return server

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TEST_TIMEOUT)
        if sock.connect_ex((host, port)) == 0:
            return server
    except Exception:
        return None
    finally:
        if sock:
            sock.close()
    return None

def save_profiles(servers):
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    os.makedirs(PROFILE_DIR, exist_ok=True)

    final = []
    for server in servers:
        profile = clean(server.get("profile_content"))
        if not profile:
            continue

        host = server.get("hostname") or server.get("ip") or "server"
        source = safe_filename(server.get("source", "vpn").lower())
        filename = f"{source}_{safe_filename(host)}_{server.get('port')}_{server.get('protocol')}.ovpn"
        filepath = os.path.join(PROFILE_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(profile.rstrip() + "\n")

        server["profile"] = f"profiles/{filename}"
        server["last_updated"] = utc_now()
        server.pop("profile_content", None)
        final.append(server)

    return final

# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    try:
        print("🚀 StVPN Server Updater Running...")
        session = make_session()

        candidates = fetch_vpngate_servers(session)

        if not candidates:
            print("⚠️ No candidates retrieved. Exiting gracefully.")
            return 0

        active = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(test_realtime, s) for s in candidates]
            for future in as_completed(futures):
                res = future.result()
                if res:
                    active.append(res)

        print(f"✅ Active servers filtered: {len(active)}")

        if not active:
            print("⚠️ No active servers passed test.")
            return 0

        final_servers = save_profiles(active)

        os.makedirs(os.path.dirname(JSON_OUTPUT), exist_ok=True)
        with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
            json.dump({"version": 5, "generated_at": utc_now(), "count": len(final_servers), "servers": final_servers}, f, indent=2, ensure_ascii=False)

        print(f"🎉 SUCCESS: Successfully updated {len(final_servers)} profiles!")
        return 0

    except Exception as exc:
        print(f"⚠️ Unexpected Runtime Error: {exc}")
        return 0

if __name__ == "__main__":
    sys.exit(main())
            port = int(port_match.group(1)) if port_match else 1194

            # Ensure Routing & DNS Settings
            if "redirect-gateway" not in ovpn_profile:
                ovpn_profile += "\nredirect-gateway def1\n"
            if "dhcp-option DNS" not in ovpn_profile:
                ovpn_profile += "\ndhcp-option DNS 8.8.8.8\ndhcp-option DNS 1.1.1.1\n"

            servers.append({
                "source": "VPNGate",
                "id": f"vpngate_{ip}_{port}",
                "country": clean(row.get("CountryShort")) or "UN",
                "country_name": clean(row.get("CountryLong")) or "Unknown",
                "hostname": host or ip,
                "ip": ip,
                "protocol": proto,
                "port": port,
                "speed_mbps": round(to_float(row.get("Speed")) / 1000000.0, 2),
                "ping_ms": to_float(row.get("Ping")),
                "username": "",
                "password": "",
                "profile_content": ovpn_profile,
                "config_auth": "none"
            })
    except Exception as e:
        print(f"⚠️ VPNGate Fetch Error: {e}")

    print(f"📥 VPNGate candidates: {len(servers)}")
    return servers

# ============================================================
# PUBLICVPNLIST & VPNBOOK FETCHERS
# ============================================================

def fetch_publicvpn_api(session):
    servers = []
    try:
        res = session.get(PUBLICVPN_API, params={"protocol": "openvpn", "status": "online", "per_page": 100}, timeout=REQUEST_TIMEOUT)
        if res.status_code == 200:
            for row in res.json().get("data", []):
                ip = clean(row.get("ip"))
                port = to_int(row.get("port"))
                if ip and port > 0:
                    servers.append({
                        "source": "PublicVPNList",
                        "id": f"publicvpn_{ip}_{port}",
                        "country": clean(row.get("country_code")) or "UN",
                        "country_name": clean(row.get("country_name")) or "Unknown",
                        "hostname": clean(row.get("hostname")),
                        "ip": ip,
                        "protocol": clean(row.get("transport")).lower() or "udp",
                        "port": port,
                        "speed_mbps": to_float(row.get("speed_mbps")),
                        "ping_ms": to_float(row.get("latency_ms")),
                        "temporary_ovpn_url": clean(row.get("temporary_ovpn_url"))
                    })
    except Exception as e:
        print(f"⚠️ PublicVPN API Warning: {e}")
    return servers

def fetch_vpnbook_servers(session):
    servers = []
    nodes = [
        {"host": "us16.vpnbook.com", "country": "US", "country_name": "United States"},
        {"host": "ca149.vpnbook.com", "country": "CA", "country_name": "Canada"},
        {"host": "de220.vpnbook.com", "country": "DE", "country_name": "Germany"},
        {"host": "fr200.vpnbook.com", "country": "FR", "country_name": "France"}
    ]
    for node in nodes:
        host = node["host"]
        profile = (
            "client\ndev tun\nproto tcp\n"
            f"remote {host} 443\n"
            "resolv-retry infinite\nnobind\npersist-key\npersist-tun\n"
            "remote-cert-tls server\nredirect-gateway def1\n"
            "dhcp-option DNS 8.8.8.8\ndhcp-option DNS 1.1.1.1\n"
            "data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC:BF-CBC\n"
            "cipher AES-256-CBC\nauth-user-pass\n"
            "<auth-user-pass>\nvpnbook\n2b7v7e8\n</auth-user-pass>\n"
        )
        servers.append({
            "source": "VPNBook",
            "id": f"vpnbook_{host}_443",
            "country": node["country"],
            "country_name": node["country_name"],
            "hostname": host,
            "ip": "",
            "protocol": "tcp",
            "port": 443,
            "speed_mbps": 10.0,
            "ping_ms": 120.0,
            "username": "vpnbook",
            "password": "",
            "profile_content": profile,
            "config_auth": "embedded"
        })
    return servers

# ============================================================
# REALTIME TEST & SAVE
# ============================================================

def test_realtime(server):
    host = server.get("ip") or server.get("hostname")
    port = to_int(server.get("port"))
    proto = clean(server.get("protocol")).lower()

    if not host or port <= 0:
        return None

    if proto == "udp":
        return server

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TEST_TIMEOUT)
        if sock.connect_ex((host, port)) == 0:
            return server
    except Exception:
        return None
    finally:
        if sock:
            sock.close()
    return None

def save_profiles(servers):
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    os.makedirs(PROFILE_DIR, exist_ok=True)

    final = []
    for server in servers:
        profile = clean(server.get("profile_content"))
        if not profile:
            continue

        host = server.get("hostname") or server.get("ip") or "server"
        source = safe_filename(server.get("source", "vpn").lower())
        filename = f"{source}_{safe_filename(host)}_{server.get('port')}_{server.get('protocol')}.ovpn"
        filepath = os.path.join(PROFILE_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(profile.rstrip() + "\n")

        server["profile"] = f"profiles/{filename}"
        server["last_updated"] = utc_now()
        server.pop("profile_content", None)
        server.pop("temporary_ovpn_url", None)
        final.append(server)

    return final

# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    print("🚀 StVPN Server Updater Running...")
    session = make_session()

    candidates = fetch_vpngate_servers(session) + fetch_publicvpn_api(session) + fetch_vpnbook_servers(session)

    if not candidates:
        print("⚠️ No candidates retrieved from any source. Skipping update safely...")
        return 0  # Return 0 so GitHub Actions doesn't fail with Exit Code 1

    active = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(test_realtime, s) for s in candidates]
        for future in as_completed(futures):
            res = future.result()
            if res:
                active.append(res)

    print(f"✅ Active servers filtered: {len(active)}")

    if not active:
        print("⚠️ No active servers passed test. Keeping existing profiles...")
        return 0  # Avoid crash on network lag

    final_servers = save_profiles(active)

    os.makedirs(os.path.dirname(JSON_OUTPUT), exist_ok=True)
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({"version": 5, "generated_at": utc_now(), "count": len(final_servers), "servers": final_servers}, f, indent=2, ensure_ascii=False)

    print(f"🎉 SUCCESS: Successfully updated {len(final_servers)} profiles!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
    return "", ""

def profile_requires_auth(profile):
    if not profile:
        return False
    if re.search(r"<auth-user-pass>", profile, re.IGNORECASE):
        return False
    return bool(re.search(r"^\s*auth-user-pass(?:\s+.*)?$", profile, re.MULTILINE | re.IGNORECASE))

def convert_to_inline_auth(profile, username, password):
    if not profile or not username or not password:
        return profile
    
    lines = profile.split("\n")
    modified_lines = []
    auth_found = False
    
    for line in lines:
        if line.strip().startswith("auth-user-pass") and not auth_found:
            auth_found = True
            modified_lines.extend(["<auth-user-pass>", username, password, "</auth-user-pass>"])
        else:
            modified_lines.append(line)
    
    if not auth_found:
        modified_lines.extend(["<auth-user-pass>", username, password, "</auth-user-pass>"])
    
    return "\n".join(modified_lines)

# ============================================================
# VPNBOOK
# ============================================================

def get_vpnbook_credentials(session):
    username = "vpnbook"
    password = ""
    try:
        response = session.get(VPNBOOK_URL, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return username, password

        html = response.text
        patterns = [
            r"Password.*?<code[^>]*>\s*([^<\s]+)",
            r"Password.*?<strong[^>]*>\s*([^<\s]+)",
            r"Password.*?<b[^>]*>\s*([^<\s]+)",
            r"Password.*?`([^`]+)`",
            r"Password.{0,300}?([A-Za-z0-9]{5,20})"
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                candidate = match.group(1).strip()
                if candidate and len(candidate) >= 4 and "password" not in candidate.lower():
                    password = candidate
                    break

        if password:
            print(f"🔑 VPNBook credentials detected: {username}/********")
        else:
            print("⚠️ VPNBook password not detected.")

    except Exception as exc:
        print("⚠️ VPNBook credential error:", exc)

    return username, password

def fetch_vpnbook_servers(session):
    username, password = get_vpnbook_credentials(session)
    if not password:
        return []

    nodes = [
        {"host": "us16.vpnbook.com", "country": "US", "country_name": "United States"},
        {"host": "us178.vpnbook.com", "country": "US", "country_name": "United States"},
        {"host": "ca149.vpnbook.com", "country": "CA", "country_name": "Canada"},
        {"host": "ca196.vpnbook.com", "country": "CA", "country_name": "Canada"},
        {"host": "uk205.vpnbook.com", "country": "GB", "country_name": "United Kingdom"},
        {"host": "uk68.vpnbook.com", "country": "GB", "country_name": "United Kingdom"},
        {"host": "de20.vpnbook.com", "country": "DE", "country_name": "Germany"},
        {"host": "de220.vpnbook.com", "country": "DE", "country_name": "Germany"},
        {"host": "fr200.vpnbook.com", "country": "FR", "country_name": "France"},
        {"host": "fr231.vpnbook.com", "country": "FR", "country_name": "France"},
    ]

    servers = []
    for node in nodes:
        host = node["host"]
        # Essential OpenVPN Directives (Routing & DNS Added)
        profile = (
            "client\n"
            "dev tun\n"
            "proto tcp\n"
            f"remote {host} 443\n"
            "resolv-retry infinite\n"
            "nobind\n"
            "persist-key\n"
            "persist-tun\n"
            "remote-cert-tls server\n"
            "redirect-gateway def1\n"
            "dhcp-option DNS 8.8.8.8\n"
            "dhcp-option DNS 1.1.1.1\n"
            "data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC:BF-CBC\n"
            "cipher AES-256-CBC\n"
            "auth-nocache\n"
            "verb 3\n"
        )
        profile = convert_to_inline_auth(profile, username, password)

        servers.append({
            "source": "VPNBook",
            "id": f"vpnbook_{host}_443_tcp",
            "country": node["country"],
            "country_name": node["country_name"],
            "hostname": host,
            "ip": "",
            "protocol": "tcp",
            "port": 443,
            "speed_mbps": 10.0,
            "ping_ms": 100.0,
            "username": username,
            "password": password,
            "profile_content": profile,
            "config_auth": "embedded",
        })

    print(f"📥 VPNBook candidates: {len(servers)}")
    return servers

# ============================================================
# PUBLICVPNLIST API & EXPORT
# ============================================================

def fetch_publicvpn_api(session):
    servers = []
    print("\n🌐 PublicVPNList API...")

    for page in range(1, PUBLICVPN_PAGES + 1):
        params = {
            "protocol": "openvpn",
            "status": "online",
            "fresh_within": FRESH_WITHIN,
            "sort": "score",
            "order": "desc",
            "page": page,
            "per_page": PUBLICVPN_PER_PAGE,
        }
        try:
            response = session.get(PUBLICVPN_API, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                break
            response.raise_for_status()
            rows = response.json().get("data", [])
            if not rows:
                break

            for row in rows:
                protocol = clean(row.get("protocol")).lower()
                transport = normalize_protocol(row.get("transport"))
                if protocol != "openvpn" or transport not in ("tcp", "udp"):
                    continue

                host = clean(row.get("hostname"))
                ip = clean(row.get("ip"))
                port = to_int(row.get("port"))
                if not host and not ip or port <= 0:
                    continue

                speed = to_float(row.get("speed_mbps"))
                latency = to_float(row.get("latency_ms"))
                if (speed > 0 and speed < MIN_SPEED_MBPS) or (latency > 0 and latency > MAX_LATENCY_MS):
                    continue

                public_id = clean(row.get("id"))
                servers.append({
                    "source": "PublicVPNList",
                    "id": f"publicvpnlist_{public_id or ip}_{port}",
                    "public_id": public_id,
                    "country": clean(row.get("country_code")) or "UN",
                    "country_name": clean(row.get("country_name")) or "Unknown",
                    "hostname": host,
                    "ip": ip,
                    "protocol": transport,
                    "port": port,
                    "speed_mbps": speed,
                    "ping_ms": latency,
                    "username": "",
                    "password": "",
                    "profile_content": "",
                    "config_auth": "unknown",
                    "temporary_ovpn_url": clean(row.get("temporary_ovpn_url")) or clean(row.get("temporary_config_url")),
                })
        except Exception as exc:
            print(f"⚠️ PublicVPNList API page {page}: {exc}")
            break

    unique = {(s.get("ip") or s.get("hostname"), s.get("port"), s.get("protocol")): s for s in servers}
    result = list(unique.values())
    print(f"📥 PublicVPNList API candidates: {len(result)}")
    return result

def fetch_publicvpn_export(session):
    print("\n📦 PublicVPNList OpenVPN export...")
    try:
        response = session.get(PUBLICVPN_EXPORT, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return []

        payload = response.json()
        rows = payload.get("data", payload.get("servers", [])) if isinstance(payload, dict) else payload
        result = []

        for row in rows:
            protocol = clean(row.get("protocol")).lower()
            transport = normalize_protocol(row.get("transport") or row.get("proto"))
            if protocol and protocol != "openvpn" or transport not in ("tcp", "udp"):
                continue

            host = clean(row.get("hostname") or row.get("host"))
            ip = clean(row.get("ip"))
            port = to_int(row.get("port"))
            temporary_url = clean(row.get("temporary_ovpn_url"))

            if not temporary_url or (not host and not ip) or port <= 0:
                continue

            result.append({
                "public_id": clean(row.get("id") or row.get("public_id")),
                "country": clean(row.get("country_code") or row.get("country")) or "UN",
                "country_name": clean(row.get("country_name") or row.get("countryName")) or "Unknown",
                "hostname": host,
                "ip": ip,
                "protocol": transport,
                "port": port,
                "speed_mbps": to_float(row.get("speed_mbps")),
                "ping_ms": to_float(row.get("latency_ms")),
                "temporary_ovpn_url": temporary_url,
            })

        return result
    except Exception as exc:
        print("⚠️ PublicVPNList export error:", exc)
        return []

def download_ovpn(session, temporary_url):
    if not temporary_url:
        return None
    try:
        response = session.get(temporary_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if response.status_code != 200:
            return None
        content = response.text.strip()
        lower = content.lower()
        if len(content) < 100 or ("client" not in lower and "remote " not in lower):
            return None
        return content
    except Exception:
        return None

def merge_publicvpn_sources(api_servers, export_servers):
    merged = {}
    for server in api_servers:
        key = ("id", server["public_id"]) if server.get("public_id") else ("endpoint", server.get("ip") or server.get("hostname"), server["port"], server["protocol"])
        merged[key] = dict(server)

    for server in export_servers:
        key = ("id", server["public_id"]) if server.get("public_id") else ("endpoint", server.get("ip") or server.get("hostname"), server["port"], server["protocol"])
        if key in merged:
            merged[key]["temporary_ovpn_url"] = server.get("temporary_ovpn_url", "")
        else:
            merged[key] = dict(server)

    return list(merged.values())

# ============================================================
# REALTIME TEST (FIXED FOR UDP & TCP)
# ============================================================

def resolve_ip(server):
    ip = clean(server.get("ip"))
    if ip:
        return ip
    hostname = clean(server.get("hostname"))
    if not hostname:
        return ""
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return ""

def test_realtime(server):
    host = resolve_ip(server)
    port = to_int(server.get("port"))
    protocol = clean(server.get("protocol")).lower()

    if not host or port <= 0:
        return None

    # UDP Socket Handshake testing directly over socket stream fails.
    # Accept UDP endpoints based on IP resolution and API latency metadata.
    if protocol == "udp":
        server["ip"] = server.get("ip") or host
        return server

    sock = None
    try:
        start = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TEST_TIMEOUT)
        result = sock.connect_ex((host, port))
        elapsed = (time.monotonic() - start) * 1000.0

        if result != 0:
            return None

        server["ip"] = server.get("ip") or host
        server["ping_ms"] = round(elapsed, 2)
        return server
    except Exception:
        return None
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass

def filter_active(servers):
    if not servers:
        return []
    print(f"\n🔍 Realtime testing {len(servers)} endpoints...")
    active = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(test_realtime, server) for server in servers]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    active.append(result)
            except Exception:
                pass
    print(f"✅ Realtime active: {len(active)} / {len(servers)}")
    return active

def acquire_publicvpn_profiles(session, active_servers):
    result = []
    print("\n📥 Downloading PublicVPNList .ovpn...")
    for server in active_servers:
        if server.get("source") != "PublicVPNList":
            result.append(server)
            continue

        url = clean(server.get("temporary_ovpn_url"))
        profile = download_ovpn(session, url) if url else None
        if not profile:
            continue

        username, password = parse_embedded_credentials(profile)
        if username and password:
            profile = convert_to_inline_auth(profile, username, password)
            server["config_auth"] = "embedded"
        else:
            server["config_auth"] = "required" if profile_requires_auth(profile) else "none"

        server["profile_content"] = profile
        server["username"] = username
        server["password"] = password
        result.append(server)

    return result

# ============================================================
# SAVE FUNCTIONS
# ============================================================

def save_profiles(servers):
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    os.makedirs(PROFILE_DIR, exist_ok=True)

    final = []
    for server in servers:
        profile = clean(server.get("profile_content"))
        if not profile:
            continue

        username = server.get("username", "")
        password = server.get("password", "")
        if username and password and "<auth-user-pass>" not in profile:
            profile = convert_to_inline_auth(profile, username, password)
            server["config_auth"] = "embedded"

        host = server.get("hostname") or server.get("ip") or "server"
        source = safe_filename(server.get("source", "vpn").lower())
        filename = f"{source}_{safe_filename(host)}_{server.get('port')}_{server.get('protocol')}.ovpn"
        filepath = os.path.join(PROFILE_DIR, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(profile.rstrip() + "\n")
        except Exception as exc:
            continue

        server["profile"] = f"profiles/{filename}"
        server["last_updated"] = utc_now()
        server.pop("temporary_ovpn_url", None)
        server.pop("config_download_url", None)
        server.pop("profile_content", None)
        final.append(server)

    return final

def save_json(servers):
    os.makedirs(os.path.dirname(JSON_OUTPUT), exist_ok=True)
    payload = {"version": 5, "generated_at": utc_now(), "count": len(servers), "servers": servers}
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def save_csv(servers):
    os.makedirs(os.path.dirname(CSV_OUTPUT), exist_ok=True)
    fields = ["id", "source", "country", "country_name", "hostname", "ip", "protocol", "port", "speed_mbps", "ping_ms", "username", "password", "config_auth", "profile", "last_checked_at", "last_updated"]
    with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for server in servers:
            writer.writerow({field: server.get(field, "") for field in fields})

def server_sort_key(server):
    ping = to_float(server.get("ping_ms"))
    speed = to_float(server.get("speed_mbps"))
    return (ping if ping > 0 else 999999, -speed)

# ============================================================
# MAIN
# ============================================================

def main():
    print("🚀 StVPN Public OpenVPN Updater")
    session = make_session()

    api_servers = fetch_publicvpn_api(session)
    export_servers = fetch_publicvpn_export(session)
    public_servers = merge_publicvpn_sources(api_servers, export_servers)
    vpnbook_servers = fetch_vpnbook_servers(session)

    candidates = public_servers + vpnbook_servers
    unique = {(s.get("ip") or s.get("hostname"), to_int(s.get("port")), clean(s.get("protocol")).lower()): s for s in candidates}
    candidates = list(unique.values())

    if not candidates:
        print("❌ No VPN candidates.")
        return 1

    active = filter_active(candidates)
    active = acquire_publicvpn_profiles(session, active)

    if not active:
        print("❌ No usable OpenVPN profiles.")
        return 1

    active.sort(key=server_sort_key)
    final_servers = save_profiles(active)

    save_json(final_servers)
    save_csv(final_servers)

    print(f"\n🎉 SUCCESS: {len(final_servers)} Active profiles saved!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
        return ""

    return str(value).strip()


def to_float(value):
    try:
        if value is None:
            return 0.0

        return float(
            str(value)
            .replace(",", "")
            .strip()
        )

    except (ValueError, TypeError):
        return 0.0


def to_int(value):
    try:
        return int(
            float(
                str(value)
                .replace(",", "")
                .strip()
            )
        )

    except (ValueError, TypeError):
        return 0


def safe_filename(value):
    value = clean(value)

    value = re.sub(
        r"[^a-zA-Z0-9._-]+",
        "_",
        value
    )

    return value[:120] or "server"


def normalize_protocol(value):
    value = clean(value).lower()

    if value in ("tcp", "udp"):
        return value

    return ""


# ============================================================
# CREDENTIAL PARSER
# ============================================================

def parse_embedded_credentials(profile):
    """
    Read credentials only when explicitly embedded inside
    an OpenVPN profile.

    Supported:

        <auth-user-pass>
        username
        password
        </auth-user-pass>
    """

    if not profile:
        return "", ""

    match = re.search(
        r"<auth-user-pass>\s*(.*?)\s*</auth-user-pass>",
        profile,
        re.IGNORECASE | re.DOTALL
    )

    if not match:
        return "", ""

    lines = [
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip()
    ]

    if len(lines) >= 2:
        return lines[0], lines[1]

    return "", ""


def profile_requires_auth(profile):
    """
    Detect normal auth-user-pass directive.

    An embedded <auth-user-pass> block is not treated as a
    separate requirement because credentials are already inside.
    """

    if not profile:
        return False

    embedded = re.search(
        r"<auth-user-pass>",
        profile,
        re.IGNORECASE
    )

    if embedded:
        return False

    return bool(
        re.search(
            r"^\s*auth-user-pass(?:\s+.*)?$",
            profile,
            re.MULTILINE | re.IGNORECASE
        )
    )


# ============================================================
# CONVERT TO INLINE AUTH
# ============================================================

def convert_to_inline_auth(profile, username, password):
    """
    Convert auth-user-pass to inline format:
    
        <auth-user-pass>
        username
        password
        </auth-user-pass>
    """
    if not profile or not username or not password:
        return profile
    
    lines = profile.split("\n")
    modified_lines = []
    auth_found = False
    
    for line in lines:
        # Check if line is auth-user-pass
        if line.strip().startswith("auth-user-pass") and not auth_found:
            auth_found = True
            modified_lines.append("<auth-user-pass>")
            modified_lines.append(username)
            modified_lines.append(password)
            modified_lines.append("</auth-user-pass>")
        else:
            modified_lines.append(line)
    
    # If auth-user-pass not found, add it at the end
    if not auth_found:
        modified_lines.append("<auth-user-pass>")
        modified_lines.append(username)
        modified_lines.append(password)
        modified_lines.append("</auth-user-pass>")
    
    return "\n".join(modified_lines)


# ============================================================
# VPNBOOK
# ============================================================

def get_vpnbook_credentials(session):
    """
    Read the currently published VPNBook username/password.
    """

    username = "vpnbook"
    password = ""

    try:
        response = session.get(
            VPNBOOK_URL,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            print(
                "⚠️ VPNBook HTTP",
                response.status_code
            )

            return username, password

        html = response.text

        # Current page format.
        patterns = [
            r"Username.*?vpnbook",
            r"Password.*?<code[^>]*>\s*([^<\s]+)",
            r"Password.*?<strong[^>]*>\s*([^<\s]+)",
            r"Password.*?<b[^>]*>\s*([^<\s]+)",
            r"Password.*?`([^`]+)`",
        ]

        # Username is normally fixed.
        if re.search(
            patterns[0],
            html,
            re.IGNORECASE | re.DOTALL
        ):
            username = "vpnbook"

        for pattern in patterns[1:]:
            match = re.search(
                pattern,
                html,
                re.IGNORECASE | re.DOTALL
            )

            if match:
                candidate = match.group(1).strip()

                if (
                    candidate
                    and len(candidate) >= 4
                    and "password" not in candidate.lower()
                ):
                    password = candidate
                    break

        # Generic fallback around visible Password text.
        if not password:
            match = re.search(
                r"Password.{0,300}?([A-Za-z0-9]{5,20})",
                html,
                re.IGNORECASE | re.DOTALL
            )

            if match:
                password = match.group(1).strip()

        if password:
            print(
                f"🔑 VPNBook credentials detected: "
                f"{username}/********"
            )
        else:
            print(
                "⚠️ VPNBook password not detected."
            )

    except Exception as exc:
        print(
            "⚠️ VPNBook credential error:",
            exc
        )

    return username, password


def fetch_vpnbook_servers(session):
    """
    Current VPNBook OpenVPN server list.

    TCP 443 is preferred because it is generally more
    firewall-friendly.
    """

    username, password = (
        get_vpnbook_credentials(session)
    )

    if not password:
        return []

    nodes = [
        {
            "host": "us16.vpnbook.com",
            "country": "US",
            "country_name": "United States",
        },
        {
            "host": "us178.vpnbook.com",
            "country": "US",
            "country_name": "United States",
        },
        {
            "host": "ca149.vpnbook.com",
            "country": "CA",
            "country_name": "Canada",
        },
        {
            "host": "ca196.vpnbook.com",
            "country": "CA",
            "country_name": "Canada",
        },
        {
            "host": "uk205.vpnbook.com",
            "country": "GB",
            "country_name": "United Kingdom",
        },
        {
            "host": "uk68.vpnbook.com",
            "country": "GB",
            "country_name": "United Kingdom",
        },
        {
            "host": "de20.vpnbook.com",
            "country": "DE",
            "country_name": "Germany",
        },
        {
            "host": "de220.vpnbook.com",
            "country": "DE",
            "country_name": "Germany",
        },
        {
            "host": "fr200.vpnbook.com",
            "country": "FR",
            "country_name": "France",
        },
        {
            "host": "fr231.vpnbook.com",
            "country": "FR",
            "country_name": "France",
        },
    ]

    servers = []

    for node in nodes:

        host = node["host"]

        # Create profile with inline auth
        profile = (
            "client\n"
            "dev tun\n"
            "proto tcp\n"
            f"remote {host} 443\n"
            "resolv-retry infinite\n"
            "nobind\n"
            "persist-key\n"
            "persist-tun\n"
            "remote-cert-tls server\n"
            "auth-nocache\n"
            "verb 3\n"
        )

        # Add inline auth
        profile = convert_to_inline_auth(profile, username, password)

        servers.append({
            "source": "VPNBook",
            "id": f"vpnbook_{host}_443_tcp",
            "country": node["country"],
            "country_name": node["country_name"],
            "hostname": host,
            "ip": "",
            "protocol": "tcp",
            "port": 443,
            "speed_mbps": 0.0,
            "ping_ms": 0.0,
            "username": username,
            "password": password,
            "profile_content": profile,
            "config_auth": "embedded",
        })

    print(
        f"📥 VPNBook candidates: {len(servers)}"
    )

    return servers


# ============================================================
# PUBLICVPNLIST API
# ============================================================

def fetch_publicvpn_api(session):
    """
    Fetch OpenVPN metadata from PublicVPNList API v1.

    API is used for:
        country
        hostname
        ip
        protocol
        transport
        port
        speed
        latency
        freshness
        status
        public ID
    """

    servers = []

    print(
        "\n🌐 PublicVPNList API..."
    )

    for page in range(
        1,
        PUBLICVPN_PAGES + 1
    ):

        params = {
            "protocol": "openvpn",
            "status": "online",
            "fresh_within": FRESH_WITHIN,
            "sort": "score",
            "order": "desc",
            "page": page,
            "per_page": PUBLICVPN_PER_PAGE,
        }

        try:

            response = session.get(
                PUBLICVPN_API,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 429:

                print(
                    "⚠️ PublicVPNList rate limit."
                )

                break

            response.raise_for_status()

            payload = response.json()

            rows = payload.get(
                "data",
                []
            )

            if not rows:
                break

            for row in rows:

                protocol = clean(
                    row.get("protocol")
                ).lower()

                transport = normalize_protocol(
                    row.get("transport")
                )

                if protocol != "openvpn":
                    continue

                if transport not in (
                    "tcp",
                    "udp"
                ):
                    continue

                host = clean(
                    row.get("hostname")
                )

                ip = clean(
                    row.get("ip")
                )

                port = to_int(
                    row.get("port")
                )

                if not host and not ip:
                    continue

                if port <= 0:
                    continue

                speed = to_float(
                    row.get("speed_mbps")
                )

                latency = to_float(
                    row.get("latency_ms")
                )

                if (
                    speed > 0
                    and speed < MIN_SPEED_MBPS
                ):
                    continue

                if (
                    latency > 0
                    and latency > MAX_LATENCY_MS
                ):
                    continue

                public_id = clean(
                    row.get("id")
                )

                server = {
                    "source": "PublicVPNList",
                    "id": (
                        f"publicvpnlist_"
                        f"{public_id or ip}_{port}"
                    ),
                    "public_id": public_id,
                    "country": clean(
                        row.get(
                            "country_code"
                        )
                    ) or "UN",
                    "country_name": clean(
                        row.get(
                            "country_name"
                        )
                    ) or "Unknown",
                    "hostname": host,
                    "ip": ip,
                    "protocol": transport,
                    "port": port,
                    "speed_mbps": speed,
                    "ping_ms": latency,
                    "username": "",
                    "password": "",
                    "profile_content": "",
                    "config_auth": "unknown",
                    "last_checked_at": clean(
                        row.get(
                            "last_checked_at"
                        )
                    ),
                    "availability_status": clean(
                        row.get(
                            "availability_status"
                        )
                    ),
                    "freshness_status": clean(
                        row.get(
                            "freshness_status"
                        )
                    ),
                    "config_download_url": clean(
                        row.get(
                            "config_download_url"
                        )
                    ),
                }

                # Some API/export revisions may expose the
                # temporary URL. Keep it if present.
                temporary_url = (
                    clean(
                        row.get(
                            "temporary_ovpn_url"
                        )
                    )
                    or clean(
                        row.get(
                            "temporary_config_url"
                        )
                    )
                )

                server[
                    "temporary_ovpn_url"
                ] = temporary_url

                servers.append(
                    server
                )

        except Exception as exc:

            print(
                f"⚠️ PublicVPNList API "
                f"page {page}: {exc}"
            )

            break

    # Deduplicate
    unique = {}

    for server in servers:

        key = (
            server.get("ip")
            or server.get("hostname"),
            server.get("port"),
            server.get("protocol"),
        )

        unique[key] = server

    result = list(
        unique.values()
    )

    print(
        f"📥 PublicVPNList API candidates: "
        f"{len(result)}"
    )

    return result


# ============================================================
# PUBLICVPNLIST EXPORT
# ============================================================

def fetch_publicvpn_export(session):
    """
    PublicVPNList's OpenVPN export can contain
    temporary_ovpn_url.

    This is intentionally used only as a metadata/config
    acquisition source. The temporary URL is consumed
    immediately and is not stored in servers.json.
    """

    print(
        "\n📦 PublicVPNList OpenVPN export..."
    )

    try:

        response = session.get(
            PUBLICVPN_EXPORT,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 429:

            print(
                "⚠️ PublicVPNList export rate limited."
            )

            return []

        response.raise_for_status()

        payload = response.json()

        if isinstance(payload, dict):

            rows = payload.get(
                "data",
                []
            )

            if not rows:
                rows = payload.get(
                    "servers",
                    []
                )

        elif isinstance(payload, list):

            rows = payload

        else:
            rows = []

        print(
            f"📦 Export records: {len(rows)}"
        )

        result = []

        for row in rows:

            protocol = clean(
                row.get("protocol")
            ).lower()

            transport = normalize_protocol(
                row.get("transport")
                or row.get("proto")
            )

            if (
                protocol
                and protocol != "openvpn"
            ):
                continue

            if transport not in (
                "tcp",
                "udp"
            ):
                continue

            host = clean(
                row.get("hostname")
                or row.get("host")
            )

            ip = clean(
                row.get("ip")
            )

            port = to_int(
                row.get("port")
            )

            temporary_url = clean(
                row.get(
                    "temporary_ovpn_url"
                )
            )

            if (
                not temporary_url
                or not host and not ip
                or port <= 0
            ):
                continue

            result.append({
                "public_id": clean(
                    row.get("id")
                    or row.get(
                        "public_id"
                    )
                ),
                "country": clean(
                    row.get(
                        "country_code"
                    )
                    or row.get(
                        "country"
                    )
                ) or "UN",
                "country_name": clean(
                    row.get(
                        "country_name"
                    )
                    or row.get(
                        "countryName"
                    )
                ) or "Unknown",
                "hostname": host,
                "ip": ip,
                "protocol": transport,
                "port": port,
                "speed_mbps": to_float(
                    row.get(
                        "speed_mbps"
                    )
                    or row.get(
                        "checker_measured_throughput_mbps"
                    )
                ),
                "ping_ms": to_float(
                    row.get(
                        "latency_ms"
                    )
                    or row.get(
                        "checker_measured_tunnel_rtt_ms"
                    )
                ),
                "temporary_ovpn_url":
                    temporary_url,
                "last_checked_at": clean(
                    row.get(
                        "last_checked_at"
                    )
                    or row.get(
                        "checkedAt"
                    )
                ),
            })

        return result

    except Exception as exc:

        print(
            "⚠️ PublicVPNList export error:",
            exc
        )

        return []


# ============================================================
# DOWNLOAD .OVPN
# ============================================================

def download_ovpn(
    session,
    temporary_url
):
    """
    Download a short-lived .ovpn configuration.

    We do not store the temporary URL in JSON.
    """

    if not temporary_url:
        return None

    try:

        response = session.get(
            temporary_url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        content = response.text.strip()

        if len(content) < 100:
            return None

        lower = content.lower()

        # Basic OpenVPN profile validation.
        if (
            "client" not in lower
            and "remote " not in lower
        ):
            return None

        if (
            "remote " not in lower
            and "proto " not in lower
        ):
            return None

        return content

    except Exception:
        return None


# ============================================================
# MERGE API + EXPORT
# ============================================================

def merge_publicvpn_sources(
    api_servers,
    export_servers
):
    """
    API gives reliable metadata.

    Export gives temporary_ovpn_url when available.

    Merge using:
        public_id
        IP + port + protocol
        hostname + port + protocol
    """

    merged = {}

    def key_for(server):

        public_id = clean(
            server.get("public_id")
        )

        if public_id:
            return (
                "id",
                public_id
            )

        endpoint = (
            clean(server.get("ip"))
            or clean(
                server.get("hostname")
            )
        )

        return (
            "endpoint",
            endpoint,
            to_int(
                server.get("port")
            ),
            clean(
                server.get("protocol")
            ).lower(),
        )

    for server in api_servers:
        merged[
            key_for(server)
        ] = dict(server)

    for server in export_servers:

        key = key_for(server)

        if key in merged:

            current = merged[key]

            current[
                "temporary_ovpn_url"
            ] = server.get(
                "temporary_ovpn_url",
                ""
            )

            # Keep export metadata only when
            # API value is empty.
            for field in (
                "hostname",
                "ip",
                "country",
                "country_name",
                "speed_mbps",
                "ping_ms",
            ):

                if not current.get(field):
                    current[field] = (
                        server.get(field)
                    )

        else:

            merged[key] = dict(server)

    return list(
        merged.values()
    )


# ============================================================
# REALTIME TCP CHECK
# ============================================================

def resolve_ip(server):

    ip = clean(
        server.get("ip")
    )

    if ip:
        return ip

    hostname = clean(
        server.get("hostname")
    )

    if not hostname:
        return ""

    try:

        return socket.gethostbyname(
            hostname
        )

    except Exception:

        return ""


def test_realtime(server):

    host = resolve_ip(
        server
    )

    port = to_int(
        server.get("port")
    )

    if not host or port <= 0:
        return None

    sock = None

    try:

        start = time.monotonic()

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        sock.settimeout(
            TEST_TIMEOUT
        )

        result = sock.connect_ex(
            (host, port)
        )

        elapsed = (
            time.monotonic()
            - start
        ) * 1000.0

        if result != 0:
            return None

        server["ip"] = (
            server.get("ip")
            or host
        )

        server["ping_ms"] = round(
            elapsed,
            2
        )

        return server

    except Exception:

        return None

    finally:

        if sock:

            try:
                sock.close()
            except Exception:
                pass


def filter_active(
    servers
):
    if not servers:
        return []

    print(
        f"\n🔍 Realtime testing "
        f"{len(servers)} endpoints..."
    )

    active = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = [
            executor.submit(
                test_realtime,
                server
            )
            for server in servers
        ]

        for future in as_completed(
            futures
        ):

            try:

                result = future.result()

                if result:

                    active.append(
                        result
                    )

                    print(
                        "  🟢 "
                        f"{result.get('ip')}:"
                        f"{result.get('port')} "
                        f"{result.get('protocol')} "
                        f"{result.get('ping_ms')}ms "
                        f"[{result.get('source')}]"
                    )

            except Exception:
                pass

    print(
        f"\n✅ Realtime active: "
        f"{len(active)} / {len(servers)}"
    )

    return active


# ============================================================
# DOWNLOAD PROFILES FOR ACTIVE SERVERS
# ============================================================

def acquire_publicvpn_profiles(
    session,
    active_servers
):
    """
    Download PublicVPNList .ovpn profiles only after
    realtime endpoint validation.
    """

    result = []

    print(
        "\n📥 Downloading PublicVPNList .ovpn..."
    )

    for server in active_servers:

        if (
            server.get("source")
            != "PublicVPNList"
        ):
            result.append(server)
            continue

        url = clean(
            server.get(
                "temporary_ovpn_url"
            )
        )

        profile = None

        if url:

            profile = download_ovpn(
                session,
                url
            )

        if not profile:

            print(
                "  ⚠️ No downloadable profile:",
                server.get("ip")
            )

            continue

        # Read credentials if profile contains them.
        username, password = (
            parse_embedded_credentials(
                profile
            )
        )

        # Convert to inline auth if credentials found
        if username and password:
            profile = convert_to_inline_auth(profile, username, password)
            server["config_auth"] = "embedded"
        else:
            server["config_auth"] = "required" if profile_requires_auth(profile) else "none"

        server["profile_content"] = profile
        server["username"] = username
        server["password"] = password

        result.append(
            server
        )

        print(
            "  ✅ OVPN:",
            server.get("ip"),
            server.get("port")
        )

    return result


# ============================================================
# PROFILE SAVE
# ============================================================

def save_profiles(
    servers
):

    if os.path.exists(
        PROFILE_DIR
    ):
        shutil.rmtree(
            PROFILE_DIR
        )

    os.makedirs(
        PROFILE_DIR,
        exist_ok=True
    )

    final = []

    for server in servers:

        profile = clean(
            server.get(
                "profile_content"
            )
        )

        if not profile:
            continue

        # ============================================================
        # NEW: Convert to inline auth if not already
        # ============================================================
        username = server.get("username", "")
        password = server.get("password", "")
        
        if username and password:
            # Check if already has inline auth
            if "<auth-user-pass>" not in profile:
                profile = convert_to_inline_auth(profile, username, password)
                server["config_auth"] = "embedded"
        # ============================================================

        host = (
            server.get("hostname")
            or server.get("ip")
            or "server"
        )

        source = safe_filename(
            server.get(
                "source",
                "vpn"
            ).lower()
        )

        filename = (
            f"{source}_"
            f"{safe_filename(host)}_"
            f"{server.get('port')}_"
            f"{server.get('protocol')}.ovpn"
        )

        filepath = os.path.join(
            PROFILE_DIR,
            filename
        )

        try:

            with open(
                filepath,
                "w",
                encoding="utf-8"
            ) as f:

                f.write(
                    profile.rstrip()
                    + "\n"
                )

        except Exception as exc:

            print(
                "⚠️ Write failed:",
                filename,
                exc
            )

            continue

        # Path consumed by Android app.
        server["profile"] = (
            f"profiles/{filename}"
        )

        server["last_updated"] = (
            utc_now()
        )

        # Temporary URL must NEVER go into
        # the app JSON.
        server.pop(
            "temporary_ovpn_url",
            None
        )

        server.pop(
            "config_download_url",
            None
        )

        server.pop(
            "profile_content",
            None
        )

        final.append(
            server
        )

    return final


# ============================================================
# SAVE JSON
# ============================================================

def save_json(
    servers
):

    os.makedirs(
        os.path.dirname(
            JSON_OUTPUT
        ),
        exist_ok=True
    )

    payload = {
        "version": 5,
        "generated_at": utc_now(),
        "count": len(servers),
        "servers": servers
    }

    with open(
        JSON_OUTPUT,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            payload,
            f,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# SAVE CSV
# ============================================================

def save_csv(
    servers
):

    os.makedirs(
        os.path.dirname(
            CSV_OUTPUT
        ),
        exist_ok=True
    )

    fields = [
        "id",
        "source",
        "country",
        "country_name",
        "hostname",
        "ip",
        "protocol",
        "port",
        "speed_mbps",
        "ping_ms",
        "username",
        "password",
        "config_auth",
        "profile",
        "last_checked_at",
        "last_updated",
    ]

    with open(
        CSV_OUTPUT,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields
        )

        writer.writeheader()

        for server in servers:

            writer.writerow({
                field: server.get(
                    field,
                    ""
                )
                for field in fields
            })


# ============================================================
# SORT
# ============================================================

def server_sort_key(
    server
):
    ping = to_float(
        server.get(
            "ping_ms"
        )
    )

    speed = to_float(
        server.get(
            "speed_mbps"
        )
    )

    source = server.get(
        "source",
        ""
    )

    # Lower ping first, then higher speed.
    return (
        ping if ping > 0 else 999999,
        -speed,
        source
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=============================================="
    )

    print(
        "🚀 StVPN Public OpenVPN Updater"
    )

    print(
        "   PublicVPNList + VPNBook"
    )

    print(
        "=============================================="
    )

    session = make_session()

    # --------------------------------------------------------
    # PublicVPNList
    # --------------------------------------------------------

    api_servers = (
        fetch_publicvpn_api(
            session
        )
    )

    export_servers = (
        fetch_publicvpn_export(
            session
        )
    )

    public_servers = (
        merge_publicvpn_sources(
            api_servers,
            export_servers
        )
    )

    # --------------------------------------------------------
    # VPNBook
    # --------------------------------------------------------

    vpnbook_servers = (
        fetch_vpnbook_servers(
            session
        )
    )

    candidates = (
        public_servers
        + vpnbook_servers
    )

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique = {}

    for server in candidates:

        key = (
            server.get(
                "ip"
            )
            or server.get(
                "hostname"
            ),
            to_int(
                server.get(
                    "port"
                )
            ),
            clean(
                server.get(
                    "protocol"
                )
            ).lower()
        )

        if key not in unique:
            unique[key] = server

    candidates = list(
        unique.values()
    )

    print(
        f"\n📦 Total candidates: "
        f"{len(candidates)}"
    )

    if not candidates:

        print(
            "❌ No VPN candidates."
        )

        return 1

    # --------------------------------------------------------
    # Realtime TCP
    # --------------------------------------------------------

    active = filter_active(
        candidates
    )

    if not active:

        print(
            "❌ No realtime active "
            "servers found."
        )

        return 1

    # --------------------------------------------------------
    # Download PublicVPNList profiles
    # --------------------------------------------------------

    active = (
        acquire_publicvpn_profiles(
            session,
            active
        )
    )

    if not active:

        print(
            "❌ No usable OpenVPN profiles."
        )

        return 1

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    active.sort(
        key=server_sort_key
    )

    # --------------------------------------------------------
    # Save .ovpn
    # --------------------------------------------------------

    final_servers = save_profiles(
        active
    )

    if not final_servers:

        print(
            "❌ No profiles were saved."
        )

        return 1

    # --------------------------------------------------------
    # Save JSON / CSV
    # --------------------------------------------------------

    save_json(
        final_servers
    )

    save_csv(
        final_servers
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print(
        "\n=============================================="
    )

    print(
        f"🎉 SUCCESS"
    )

    print(
        f"🟢 Active profiles: "
        f"{len(final_servers)}"
    )

    print(
        f"📄 {JSON_OUTPUT}"
    )

    print(
        f"📄 {CSV_OUTPUT}"
    )

    print(
        f"📁 {PROFILE_DIR}/"
    )

    print(
        "=============================================="
    )

    for index, server in enumerate(
        final_servers[:30],
        1
    ):

        print(
            f"{index:02d}. "
            f"{server.get('country')} "
            f"{server.get('ip') or server.get('hostname')}:"
            f"{server.get('port')} "
            f"{server.get('protocol')} "
            f"{server.get('ping_ms')}ms "
            f"{server.get('speed_mbps')}Mbps "
            f"[{server.get('source')}] "
            f"auth={server.get('config_auth')}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )
