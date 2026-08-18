
#!/usr/bin/env python3

import base64
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests


SOURCE_URL = "https://www.vpngate.net/api/iphone/"

CSV_OUTPUT = "data/servers.csv"
JSON_OUTPUT = "data/servers.json"
PROFILE_DIR = "data/profiles"

USERNAME = "vpn"
PASSWORD = "vpn"

REQUEST_TIMEOUT = 30

MAX_SERVERS = 300

MIN_SPEED_MBPS = 1.0
MAX_PING_MS = 1500.0

USER_AGENT = (
    "THOCK-VPNGate-Updater/1.0 "
    "(GitHub Actions)"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if value is None:
        return ""

    return str(value).strip()


def number(value):
    try:
        value = clean(value)

        if not value:
            return 0.0

        return float(
            value.replace(",", "")
        )

    except (ValueError, TypeError):
        return 0.0


def integer(value):
    try:
        value = clean(value)

        if not value:
            return 0

        return int(
            float(
                value.replace(",", "")
            )
        )

    except (ValueError, TypeError):
        return 0


def normalize_header(value):
    return clean(value).lstrip("#").strip()


def download_source():
    print("Downloading VPN Gate data...")

    response = requests.get(
        SOURCE_URL,
        headers={
            "User-Agent": USER_AGENT
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    text = response.content.decode(
        "utf-8",
        errors="replace",
    )

    if "#HostName" not in text:
        raise RuntimeError(
            "VPN Gate response does not contain expected CSV data"
        )

    return text


def parse_source(text):
    lines = text.splitlines()

    header_index = None

    for index, line in enumerate(lines):

        if line.startswith("#HostName"):
            header_index = index
            break

    if header_index is None:
        raise RuntimeError(
            "VPN Gate CSV header not found"
        )

    csv_text = "\n".join(
        lines[header_index:]
    )

    reader = csv.DictReader(
        io.StringIO(csv_text)
    )

    rows = []

    for raw in reader:

        row = {}

        for key, value in raw.items():

            row[
                normalize_header(key)
            ] = clean(value)

        rows.append(row)

    return rows


def decode_profile(row):
    encoded = clean(
        row.get(
            "OpenVPN_ConfigData_Base64"
        )
    )

    if not encoded:
        return None

    try:

        decoded = base64.b64decode(
            encoded
        )

        profile = decoded.decode(
            "utf-8",
            errors="replace",
        )

        if "client" not in profile:
            return None

        if "remote " not in profile:
            return None

        return profile

    except Exception as exc:

        print(
            "Profile decode failed:",
            exc,
        )

        return None


def get_remote(profile):
    if not profile:
        return None

    match = re.search(
        r"(?m)^\s*remote\s+(\S+)\s+(\d+)",
        profile,
    )

    if not match:
        return None

    return {
        "host": match.group(1),
        "port": int(match.group(2)),
    }


def get_protocol(profile):
    if not profile:
        return ""

    match = re.search(
        r"(?m)^\s*proto\s+(\S+)",
        profile,
    )

    if not match:
        return ""

    return match.group(1).lower()


def validate_profile(profile):
    if not profile:
        return False

    required = [
        "client",
        "dev tun",
        "remote ",
        "proto ",
    ]

    for item in required:

        if item not in profile:
            return False

    return True


def safe_filename(value):
    value = clean(value)

    value = re.sub(
        r"[^a-zA-Z0-9._-]+",
        "_",
        value,
    )

    return value[:100]


def calculate_score(
    speed,
    ping,
    uptime,
    sessions,
):
    speed_score = min(
        speed / 1000.0,
        1.0,
    )

    if ping <= 0:

        ping_score = 0.5

    else:

        ping_score = max(
            0.0,
            1.0 - (
                min(ping, 1500.0)
                / 1500.0
            ),
        )

    uptime_score = min(
        uptime / 720.0,
        1.0,
    )

    load_score = 1.0 / (
        1.0 + (
            sessions / 100.0
        )
    )

    score = (
        speed_score * 0.45
        + ping_score * 0.30
        + uptime_score * 0.15
        + load_score * 0.10
    )

    return round(
        score * 1000,
        2,
    )


def process_servers(rows):
    os.makedirs(
        PROFILE_DIR,
        exist_ok=True,
    )

    servers = []

    seen = set()

    generated_at = utc_now()

    for row in rows:

        hostname = clean(
            row.get("HostName")
        )

        ip = clean(
            row.get("IP")
        )

        if not hostname and not ip:
            continue

        speed = number(
            row.get("Speed")
        )

        ping = number(
            row.get("Ping")
        )

        if speed < MIN_SPEED_MBPS:
            continue

        if ping > MAX_PING_MS:
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

        server_id = (
            f"{ip or hostname}:"
            f"{remote['port']}:"
            f"{protocol}"
        )

        if server_id in seen:
            continue

        seen.add(server_id)

        sessions = integer(
            row.get(
                "NumVpnSessions"
            )
        )

        uptime = number(
            row.get("Uptime")
        )

        score = calculate_score(
            speed,
            ping,
            uptime,
            sessions,
        )

        filename = (
            safe_filename(
                hostname
                or ip
            )
            + ".ovpn"
        )

        profile_path = os.path.join(
            PROFILE_DIR,
            filename,
        )

        with open(
            profile_path,
            "w",
            encoding="utf-8",
        ) as file:

            file.write(profile)

        server = {
            "id": server_id,
            "country": clean(
                row.get(
                    "CountryShort"
                )
            ),
            "country_name": clean(
                row.get(
                    "Country"
                )
            ),
            "hostname": hostname,
            "ip": ip,
            "protocol": protocol,
            "port": remote["port"],
            "tcp_port": integer(
                row.get("TcpPort")
            ),
            "udp_port": integer(
                row.get("UdpPort")
            ),
            "speed_mbps": round(
                speed,
                2,
            ),
            "ping_ms": round(
                ping,
                2,
            ),
            "sessions": sessions,
            "uptime_days": round(
                uptime,
                2,
            ),
            "score": score,
            "username": USERNAME,
            "password": PASSWORD,
            "profile": (
                "profiles/"
                + filename
            ),
            "last_updated": generated_at,
        }

        servers.append(server)

    servers.sort(
        key=lambda item: (
            -item["score"],
            item["ping_ms"]
            if item["ping_ms"] > 0
            else 999999,
            -item["speed_mbps"],
        )
    )

    return servers[:MAX_SERVERS]


def write_csv(servers):
    os.makedirs(
        os.path.dirname(
            CSV_OUTPUT
        ),
        exist_ok=True,
    )

    fields = [
        "id",
        "country",
        "country_name",
        "hostname",
        "ip",
        "protocol",
        "port",
        "tcp_port",
        "udp_port",
        "speed_mbps",
        "ping_ms",
        "sessions",
        "uptime_days",
        "score",
        "username",
        "password",
        "profile",
        "last_updated",
    ]

    with open(
        CSV_OUTPUT,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
        )

        writer.writeheader()

        writer.writerows(
            servers
        )


def write_json(servers):
    os.makedirs(
        os.path.dirname(
            JSON_OUTPUT
        ),
        exist_ok=True,
    )

    payload = {
        "version": 1,
        "generated_at": utc_now(),
        "source": "VPN Gate",
        "count": len(servers),
        "servers": servers,
    }

    with open(
        JSON_OUTPUT,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            payload,
            file,
            indent=2,
            ensure_ascii=False,
        )


def main():
    print(
        "================================"
    )

    print(
        "THOCK VPN Gate Updater"
    )

    print(
        "================================"
    )

    source = download_source()

    rows = parse_source(
        source
    )

    print(
        f"Source servers: {len(rows)}"
    )

    servers = process_servers(
        rows
    )

    print(
        f"Valid OpenVPN servers: {len(servers)}"
    )

    if not servers:

        raise RuntimeError(
            "No valid OpenVPN servers found"
        )

    write_csv(
        servers
    )

    write_json(
        servers
    )

    print(
        f"Created: {CSV_OUTPUT}"
    )

    print(
        f"Created: {JSON_OUTPUT}"
    )

    print(
        f"Created profiles: {PROFILE_DIR}"
    )

    print(
        "Update completed successfully."
    )


if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "Cancelled."
        )

        sys.exit(130)

    except Exception as exc:

        print(
            f"ERROR: {exc}"
        )

        sys.exit(1)
