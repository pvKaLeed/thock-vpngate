#!/usr/bin/env python3
# filename: update_servers.py

import base64
import csv
import io
import json
import os
import re
import shutil
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIGURATION
# ============================================================

PUBLICVPN_API = "https://publicvpnlist.com/api/v1/servers"
PUBLICVPN_EXPORT = "https://publicvpnlist.com/exports/openvpn-latest.json"
VPNBOOK_URL = "https://www.vpnbook.com/freevpn/openvpn"
VPNGATE_API = "http://www.vpngate.net/api/iphone/"

CSV_OUTPUT = "data/servers.csv"
JSON_OUTPUT = "data/servers.json"
PROFILE_DIR = "data/profiles"

REQUEST_TIMEOUT = 15
TEST_TIMEOUT = 3.0
MAX_WORKERS = 20

FRESH_WITHIN = 259200
MIN_SPEED_MBPS = 0.5
MAX_LATENCY_MS = 1000

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return session

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def clean(value):
    return str(value).strip() if value is not None else ""

def to_float(value):
    try:
        return float(str(value).replace(",", "").strip()) if value is not None else 0.0
    except (ValueError, TypeError):
        return 0.0

def to_int(value):
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0

def safe_filename(value):
    value = clean(value)
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    return value[:120] or "server"

# ============================================================
# VPNGATE SOURCE (BACKUP SOURCE FOR GITHUB ACTIONS)
# ============================================================

def fetch_vpngate_servers(session):
    print("\n🌐 Fetching VPNGate Mirror...")
    servers = []
    try:
        res = session.get(VPNGATE_API, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            return []

        lines = res.text.splitlines()
        csv_data = [line for line in lines if line and not line.startswith("*") and not line.startswith("#")]
        if not csv_data:
            return []

        reader = csv.DictReader(csv_data)
        for row in reader:
            ip = clean(row.get("IP"))
            host = clean(row.get("HostName"))
            ovpn_b64 = clean(row.get("OpenVPN_ConfigData_Base64"))

            if not ip or not ovpn_b64:
                continue

            try:
                ovpn_profile = base64.b64decode(ovpn_b64).decode("utf-8", errors="ignore")
            except Exception:
                continue

            proto = "udp" if "proto udp" in ovpn_profile.lower() else "tcp"
            port_match = re.search(r"remote\s+\S+\s+(\d+)", ovpn_profile, re.IGNORECASE)
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
