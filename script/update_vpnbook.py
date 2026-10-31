#!/usr/bin/env python3
"""Update VPNBook's public OpenVPN profiles and server metadata.

Uses only Python's standard library. Existing data is preserved if fetching,
parsing, or validation fails.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PROFILE_DIR = DATA_DIR / "profiles" / "vpnbook"
JSON_FILE = DATA_DIR / "vpnbook.json"
AUTH_FILE = PROFILE_DIR / "auth.txt"

SOURCE_URL = "https://www.vpnbook.com/freevpn/openvpn"
HOME_URL = "https://www.vpnbook.com/"
USER_AGENT = "Mozilla/5.0 (compatible; thock-vpnbook-updater/1.0)"
TIMEOUT = 30
MAX_DOWNLOAD = 25 * 1024 * 1024

COUNTRIES = {
    "us": "United States",
    "ca": "Canada",
    "uk": "United Kingdom",
    "de": "Germany",
    "fr": "France",
}

# VPNBook's documented public OpenVPN transport ports.
PROTOCOLS = [
    {"name": "tcp443", "proto": "tcp-client", "port": 443},
    {"name": "tcp80", "proto": "tcp-client", "port": 80},
    {"name": "udp53", "proto": "udp", "port": 53},
    {"name": "udp25000", "proto": "udp", "port": 25000},
]

HOST_RE = re.compile(
    r"\b((?:us|ca|uk|de|fr)\d+)\.vpnbook\.com\b",
    re.IGNORECASE,
)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        data = response.read(MAX_DOWNLOAD + 1)

    if len(data) > MAX_DOWNLOAD:
        raise RuntimeError(f"Download exceeds size limit: {url}")
    return data


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(html.unescape(href))

    def handle_data(self, data):
        self.text_parts.append(data)


def get_page(url: str) -> tuple[str, list[str]]:
    raw = fetch(url).decode("utf-8", errors="replace")
    parser = PageParser()
    parser.feed(raw)
    return " ".join(parser.text_parts), parser.links


def parse_credentials(text: str) -> tuple[str, str]:
    # Do not hardcode a password: it changes periodically.
    username_match = re.search(
        r"Username\s+([A-Za-z0-9_.-]+)", text, re.IGNORECASE
    )
    password_match = re.search(
        r"Password\s+([A-Za-z0-9!@#$%^&*()_+=.,?-]+)",
        text,
        re.IGNORECASE,
    )

    if not username_match or not password_match:
        raise RuntimeError(
            "Could not parse credentials from the official page; "
            "refusing to publish stale or guessed credentials."
        )

    username = username_match.group(1).strip()
    password = password_match.group(1).strip()

    if not username or not password:
        raise RuntimeError("Empty VPNBook credentials.")
    return username, password


def discover_zip_urls(links: list[str]) -> list[str]:
    found = set()

    for link in links:
        url = urllib.parse.urljoin(SOURCE_URL, link)
        parsed = urllib.parse.urlparse(url)

        if parsed.scheme != "https":
            continue
        if parsed.hostname not in {"vpnbook.com", "www.vpnbook.com"}:
            continue
        if not parsed.path.lower().endswith(".zip"):
            continue

        found.add(url)

    return sorted(found)


def parse_profile(content: str, source_name: str):
    # Profiles are data, not executable commands. We only accept files
    # with a valid OpenVPN client header and at least one recognized server.
    if not re.search(r"(?m)^\s*client\s*$", content):
        return None

    hosts = sorted({
        m.group(1).lower() + ".vpnbook.com"
        for m in HOST_RE.finditer(content)
    })
    if not hosts:
        return None

    # Only accept known public VPNBook hostnames.
    host = hosts[0]
    server_code = host.split(".")[0]
    prefix = re.match(r"([a-z]+)", server_code).group(1)
    country = COUNTRIES.get(prefix)
    if not country:
        return None

    return host, server_code, country, content


def rewrite_profile(
    content: str,
    host: str,
    proto: str,
    port: int,
    auth_filename: str,
) -> str:
    lines = content.splitlines()
    output = []
    remote_written = False
    proto_written = False
    auth_written = False

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()

        # Replace existing remotes with one selected server and port.
        if lower.startswith("remote "):
            if not remote_written:
                output.append(f"remote {host} {port}")
                remote_written = True
            continue

        if lower.startswith("proto "):
            if not proto_written:
                output.append(f"proto {proto}")
                proto_written = True
            continue

        if lower.startswith("auth-user-pass"):
            if not auth_written:
                output.append(f"auth-user-pass {auth_filename}")
                auth_written = True
            continue

        output.append(line)

    if not remote_written:
        # Do not invent a profile if it lacks a remote directive.
        raise RuntimeError(f"Template has no remote directive for {host}")

    if not proto_written:
        output.insert(1, f"proto {proto}")

    if not auth_written:
        # Add credentials using a separate file instead of embedding
        # the shared password in every .ovpn profile.
        output.append(f"auth-user-pass {auth_filename}")

    result = "\n".join(output).rstrip() + "\n"

    # Basic validation of the generated configuration.
    if "BEGIN CERTIFICATE" not in result and "ca " not in result:
        if "<ca>" not in result:
            raise RuntimeError(
                f"Template for {host} does not contain a recognizable CA "
                "certificate/reference."
            )

    return result


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching official VPNBook page: {SOURCE_URL}")
    page_text, page_links = get_page(SOURCE_URL)

    # Some site versions publish credentials on the home page.
    home_text, home_links = get_page(HOME_URL)
    all_text = page_text + " " + home_text
    all_links = page_links + home_links

    username, password = parse_credentials(all_text)
    zip_urls = discover_zip_urls(all_links)

    if not zip_urls:
        raise RuntimeError(
            "No official .zip profile links were found on VPNBook's pages. "
            "The site may have changed its download interface. No files "
            "were replaced; inspect the current official download page."
        )

    print(f"Found {len(zip_urls)} official profile archive(s).")
    profiles: dict[str, str] = {}
    servers: dict[str, dict] = {}

    for zip_url in zip_urls:
        print(f"Downloading {zip_url}")
        archive_data = fetch(zip_url)

        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_data))
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"Invalid ZIP archive: {zip_url}") from exc

        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".ovpn"):
                continue
            if info.file_size > 2 * 1024 * 1024:
                print(f"Skipping oversized profile: {info.filename}")
                continue

            raw = archive.read(info)
            content = raw.decode("utf-8", errors="replace")
            parsed = parse_profile(content, info.filename)
            if not parsed:
                continue

            host, server_code, country, template = parsed

            for transport in PROTOCOLS:
                filename = (
                    f"{server_code}-{transport['name']}.ovpn"
                )
                try:
                    rendered = rewrite_profile(
                        template,
                        host=host,
                        proto=transport["proto"],
                        port=transport["port"],
                        auth_filename="auth.txt",
                    )
                except RuntimeError as exc:
                    print(f"Skipping {filename}: {exc}")
                    continue

                profiles[filename] = rendered
                server = servers.setdefault(
                    server_code,
                    {
                        "id": server_code,
                        "provider": "vpnbook",
                        "hostname": host,
                        "country": country,
                        "country_code": server_code[:2].upper(),
                        "protocols": [],
                        "profiles": [],
                    },
                )
                if transport["name"] not in server["protocols"]:
                    server["protocols"].append(transport["name"])
                if filename not in server["profiles"]:
                    server["profiles"].append(filename)

    if not profiles or not servers:
        raise RuntimeError(
            "No valid OpenVPN profiles could be generated. "
            "Existing data was not changed."
        )

    # Stage all output first. Never replace working data with an empty result.
    with tempfile.TemporaryDirectory(prefix="vpnbook-") as tmp:
        staging = Path(tmp)
        staged_profiles = staging / "profiles"
        staged_profiles.mkdir()

        for filename, content in profiles.items():
            (staged_profiles / filename).write_text(
                content, encoding="utf-8"
            )

        (staging / "auth.txt").write_text(
            f"{username}\n{password}\n", encoding="utf-8"
        )

        document = {
            "provider": "vpnbook",
            "source": SOURCE_URL,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "username": username,
            "server_count": len(servers),
            "profile_count": len(profiles),
            "servers": sorted(
                servers.values(), key=lambda item: item["id"]
            ),
        }
        (staging / "vpnbook.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # Install profiles only after the full set has passed validation.
        for old_file in PROFILE_DIR.glob("*.ovpn"):
            old_file.unlink()

        for filename in sorted(profiles):
            shutil.copy2(staged_profiles / filename, PROFILE_DIR / filename)

        shutil.copy2(staging / "auth.txt", AUTH_FILE)
        shutil.copy2(staging / "vpnbook.json", JSON_FILE)

    print(
        f"Updated {len(servers)} servers and {len(profiles)} profiles."
    )
    print(f"JSON: {JSON_FILE.relative_to(ROOT)}")
    print(f"Profiles: {PROFILE_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
