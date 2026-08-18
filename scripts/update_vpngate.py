#!/usr/bin/env python3

import base64
import csv
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

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

        return float(
            value.replace(",", "")
        )

    except (ValueError, TypeError):
        return 0.0


def to_int(value):
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


# ============================================================
# VPN Gate API
# ============================================================

def download_source():
    print("Downloading VPN Gate server list...")

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
            "VPN Gate API returned an unexpected response."
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
            "VPN Gate CSV header was not found."
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


# ============================================================
# OpenVPN Profile
# ============================================================

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
            encoded,
            validate=False,
        )

        profile = decoded.decode(
            "utf-8",
            errors="replace",
        )

        if "client" not in profile:
            return None

        if "remote " not in profile:
            return None

        return profile.strip() + "\n"

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

    return all(
        item in profile
        for item in required
    )


# ============================================================
# File names
# ============================================================

def safe_filename(value):
    value = clean(value)

    value = re.sub(
        r"[^a-zA-Z0-9._-]+",
        "_",
        value,
    )

    value = value.strip("._-")

    if not value:
        value = "server"

    return value[:100]


# ============================================================
# Server score
# ============================================================

def calculate_score(
    speed_mbps,
    ping_ms,
    uptime_days,
    sessions,
):
    """
    Score range: approximately 0-1000.

    Weight:
      Speed    = 45%
      Ping     = 30%
      Uptime   = 15%
      Load     = 10%
    """

    # 1000 Mbps or higher gets full speed score.
    speed_score = min(
        speed_mbps / 1000.0,
        1.0,
    )

    # Lower ping is better.
    if ping_ms <= 0:
        ping_score = 0.5
    else:
        ping_score = max(
            0.0,
            1.0 - (
                min(
                    ping_ms,
                    MAX_PING_MS,
                )
                / MAX_PING_MS
            ),
        )

    # 30 days or more gets full uptime score.
    uptime_score = min(
        uptime_days / 30.0,
        1.0,
    )

    # Lower active sessions = better.
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


# ============================================================
# Server processing
# ============================================================

def process_servers(rows):

    # Remove old profiles so dead/removed servers do not remain.
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(
            PROFILE_DIR
        )

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

        # ----------------------------------------------------
        # VPN Gate units
        #
        # Speed  = bits per second
        # Uptime = seconds
        # ----------------------------------------------------

        speed_bps = to_float(
            row.get("Speed")
        )

        speed_mbps = (
            speed_bps / 1_000_000.0
        )

        ping_ms = to_float(
            row.get("Ping")
        )

        uptime_seconds = to_float(
            row.get("Uptime")
        )

        uptime_days = (
            uptime_seconds / 86400.0
        )

        # ----------------------------------------------------
        # Basic quality filter
        # ----------------------------------------------------

        if speed_mbps < MIN_SPEED_MBPS:
            continue

        if ping_ms <= 0:
            continue

        if ping_ms > MAX_PING_MS:
            continue

        # ----------------------------------------------------
        # OpenVPN profile
        # ----------------------------------------------------

        profile = decode_profile(
            row
        )

        if not validate_profile(
            profile
        ):
            continue

        remote = get_remote(
            profile
        )

        if not remote:
            continue

        protocol = get_protocol(
            profile
        )

        if not protocol:
            continue

        # Normalize tcp-client to tcp.
        if protocol == "tcp-client":
            protocol = "tcp"

        # ----------------------------------------------------
        # Server identity
        # ----------------------------------------------------

        server_id = (
            f"{ip or hostname}:"
            f"{remote['port']}:"
            f"{protocol}"
        )

        if server_id in seen:
            continue

        seen.add(
            server_id
        )

        # ----------------------------------------------------
        # Sessions
        # ----------------------------------------------------

        sessions = to_int(
            row.get(
                "NumVpnSessions"
            )
        )

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = calculate_score(
            speed_mbps=speed_mbps,
            ping_ms=ping_ms,
            uptime_days=uptime_days,
            sessions=sessions,
        )

        # ----------------------------------------------------
        # Profile filename
        # ----------------------------------------------------

        filename = (
            safe_filename(
                hostname
                or ip
            )
            + "_"
            + str(
                remote["port"]
            )
            + "_"
            + protocol
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

            file.write(
                profile
            )

        # ----------------------------------------------------
        # Country
        #
        # VPN Gate API uses CountryLong / CountryShort.
        # ----------------------------------------------------

        country_short = clean(
            row.get(
                "CountryShort"
            )
        )

        country_long = clean(
            row.get(
                "CountryLong"
            )
        )

        # ----------------------------------------------------
        # Server object
        # ----------------------------------------------------

        server = {
            "id": server_id,

            "country": country_short,

            "country_name": country_long,

            "hostname": hostname,

            "ip": ip,

            "protocol": protocol,

            "port": remote["port"],

            "tcp_port": to_int(
                row.get("TcpPort")
            ),

            "udp_port": to_int(
                row.get("UdpPort")
            ),

            "speed_mbps": round(
                speed_mbps,
                2,
            ),

            "ping_ms": round(
                ping_ms,
                2,
            ),

            "sessions": sessions,

            "uptime_days": round(
                uptime_days,
                2,
            ),

            "score": score,

            "username": VPN_USERNAME,

            "password": VPN_PASSWORD,

            "profile": (
                "profiles/"
                + filename
            ),

            "last_updated": generated_at,
        }

        servers.append(
            server
        )

    # --------------------------------------------------------
    # Best servers first
    # --------------------------------------------------------

    servers.sort(
        key=lambda item: (
            -item["score"],

            item["ping_ms"],

            -item["speed_mbps"],

            item["sessions"],
        )
    )

    return servers[
        :MAX_SERVERS
    ]


# ============================================================
# CSV
# ============================================================

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


# ============================================================
# JSON
# ============================================================

def write_json(servers):

    os.makedirs(
        os.path.dirname(
            JSON_OUTPUT
        ),
        exist_ok=True,
    )

    payload = {
        "version": 2,

        "generated_at": utc_now(),

        "source": "VPN Gate",

        "count": len(
            servers
        ),

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

        file.write(
            "\n"
        )


# ============================================================
# Main
# ============================================================

def main():

    print(
        "========================================"
    )

    print(
        "THOCK VPN Gate Updater v2"
    )

    print(
        "========================================"
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
            "No valid OpenVPN servers found."
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
