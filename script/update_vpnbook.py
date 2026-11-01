
#!/usr/bin/env python3
"""Update VPNBook OpenVPN profiles from the official VPNBook website.

Outputs:
    data/vpnbook.json
    data/profiles/vpnbook/*.ovpn
    data/profiles/vpnbook/auth.txt

Only Python's standard library is required.
Existing files are preserved if discovery, download, or validation fails.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

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
API_URL = "https://www.vpnbook.com/api/openvpn"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
)
TIMEOUT = 30
MAX_DOWNLOAD = 5 * 1024 * 1024

COUNTRIES = {
    "us": "United States",
    "ca": "Canada",
    "uk": "United Kingdom",
    "de": "Germany",
    "fr": "France",
}

PROTOCOLS = (
    "tcp443",
    "tcp80",
    "udp53",
    "udp25000",
)

HOST_RE = re.compile(
    r"\b((?:us|ca|uk|de|fr)\d+)\.vpnbook\.com\b",
    re.IGNORECASE,
)


class PageParser(HTMLParser):
    """Extract visible text and links from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() != "a":
            return

        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")

        if href:
            self.links.append(html.unescape(href))

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)


def fetch(url: str) -> bytes:
    """Fetch a URL with a size limit."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")

        content_type = response.headers.get("Content-Type", "")
        data = response.read(MAX_DOWNLOAD + 1)

    if len(data) > MAX_DOWNLOAD:
        raise RuntimeError(f"Response exceeds size limit: {url}")

    # Don't reject profiles based on MIME type alone: the API may
    # return text/plain or application/octet-stream.
    if "text/html" in content_type.lower():
        text = data.decode("utf-8", errors="replace").lower()

        if "<html" in text and "client" not in text:
            raise RuntimeError(
                f"Expected profile data but received HTML: {url}"
            )

    return data


def get_page(url: str) -> tuple[str, list[str]]:
    raw = fetch(url).decode("utf-8", errors="replace")
    parser = PageParser()
    parser.feed(raw)

    print(f"Page inspected: {url}")
    print(f"HTML links: {len(parser.links)}")

    return " ".join(parser.text_parts), parser.links


def discover_hosts(*sources: str) -> list[str]:
    """Discover supported VPNBook hostnames without guessing IPs."""
    hosts: set[str] = set()

    for source in sources:
        for match in HOST_RE.finditer(source):
            hosts.add(match.group(1).lower() + ".vpnbook.com")

    return sorted(hosts)


def parse_credentials(text: str) -> tuple[str, str]:
    """Parse the current public VPNBook credentials."""
    username_match = re.search(
        r"\bUsername\s*[:：]?\s*([A-Za-z0-9_.-]+)",
        text,
        re.IGNORECASE,
    )
    password_match = re.search(
        r"\bPassword\s*[:：]?\s*([A-Za-z0-9!@#$%^&*()_+=.,?-]+)",
        text,
        re.IGNORECASE,
    )

    if not username_match or not password_match:
        raise RuntimeError(
            "Could not parse current credentials from official pages. "
            "No existing files were replaced."
        )

    username = username_match.group(1).strip()
    password = password_match.group(1).strip()

    if not username or not password:
        raise RuntimeError("Empty VPNBook credentials.")

    return username, password


def download_profile(host: str, protocol: str) -> str:
    """Download and validate one official OpenVPN profile."""
    query = urllib.parse.urlencode({
        "hostname": host,
        "protocol": protocol,
    })
    url = f"{API_URL}?{query}"

    raw = fetch(url).decode("utf-8-sig", errors="replace")
    content = raw.strip()

    if not content:
        raise RuntimeError(f"Empty API response for {host}/{protocol}")

    if not re.search(r"(?m)^\s*client\s*$", content):
        raise RuntimeError(
            f"API did not return an OpenVPN client profile for "
            f"{host}/{protocol}"
        )

    if not re.search(r"(?m)^\s*remote\s+\S+", content):
        raise RuntimeError(
            f"Profile has no remote directive for {host}/{protocol}"
        )

    # Require a real CA certificate or a CA reference.
    has_ca = (
        "<ca>" in content.lower()
        or "-----BEGIN CERTIFICATE-----" in content
        or bool(re.search(r"(?m)^\s*ca\s+\S+", content))
    )
    if not has_ca:
        raise RuntimeError(
            f"Profile has no recognizable CA certificate for "
            f"{host}/{protocol}"
        )

    # Replace auth-user-pass directives with a relative auth file.
    # Preserve all other official profile directives.
    output: list[str] = []
    auth_written = False

    for line in content.splitlines():
        if line.strip().lower().startswith("auth-user-pass"):
            if not auth_written:
                output.append("auth-user-pass auth.txt")
                auth_written = True
            continue

        output.append(line)

    if not auth_written:
        output.append("auth-user-pass auth.txt")

    result = "\n".join(output).rstrip() + "\n"

    # Final sanity check after rewriting.
    if not re.search(r"(?m)^\s*remote\s+\S+", result):
        raise RuntimeError("Profile validation failed after rewriting.")

    return result


def write_json(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    print(f"Fetching official VPNBook page: {SOURCE_URL}")

    page_text, _ = get_page(SOURCE_URL)
    home_text, _ = get_page(HOME_URL)

    all_text = page_text + "\n" + home_text

    username, password = parse_credentials(all_text)
    hosts = discover_hosts(all_text)

    if not hosts:
        raise RuntimeError(
            "No supported server hostnames were found on official pages. "
            "The website may have changed; existing files were preserved."
        )

    print(f"Discovered {len(hosts)} server(s).")

    profiles: dict[str, str] = {}
    servers: dict[str, dict] = {}
    errors: list[str] = []

    for host in hosts:
        server_code = host.split(".", 1)[0]
        prefix_match = re.match(r"([a-z]+)", server_code)

        if not prefix_match:
            continue

        country_code = prefix_match.group(1)
        country = COUNTRIES.get(country_code)

        if country is None:
            continue

        server_profiles: list[str] = []

        for protocol in PROTOCOLS:
            filename = f"{server_code}-{protocol}.ovpn"

            try:
                profile = download_profile(host, protocol)
            except (
                OSError,
                ValueError,
                RuntimeError,
                urllib.error.URLError,
            ) as exc:
                errors.append(f"{filename}: {exc}")
                continue

            profiles[filename] = profile
            server_profiles.append(filename)

        if server_profiles:
            servers[server_code] = {
                "id": server_code,
                "provider": "vpnbook",
                "hostname": host,
                "country": country,
                "country_code": country_code.upper(),
                "protocols": [
                    protocol
                    for protocol in PROTOCOLS
                    if f"{server_code}-{protocol}.ovpn" in profiles
                ],
                "profiles": server_profiles,
            }

    # Fail closed: don't publish a partial set of server profiles.
    expected_count = len(hosts) * len(PROTOCOLS)

    if errors or len(profiles) != expected_count:
        for error in errors:
            # Avoid logging credentials or profile contents.
            print(f"PROFILE ERROR: {error}", file=sys.stderr)

        raise RuntimeError(
            f"Profile update incomplete: {len(profiles)}/{expected_count} "
            "profiles downloaded and validated. Existing files were "
            "preserved."
        )

    if not servers or not profiles:
        raise RuntimeError(
            "No valid profiles were generated. Existing files were preserved."
        )

    document = {
        "provider": "vpnbook",
        "source": SOURCE_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "username": username,
        "server_count": len(servers),
        "profile_count": len(profiles),
        "servers": sorted(
            servers.values(),
            key=lambda server: server["id"],
        ),
    }

    # Stage all files before installation.
    with tempfile.TemporaryDirectory(prefix="vpnbook-") as tmp:
        staging = Path(tmp)
        staged_profiles = staging / "profiles"
        staged_profiles.mkdir()

        for filename, content in profiles.items():
            (staged_profiles / filename).write_text(
                content,
                encoding="utf-8",
            )

        (staging / "auth.txt").write_text(
            f"{username}\n{password}\n",
            encoding="utf-8",
        )

        write_json(staging / "vpnbook.json", document)

        # Keep a backup so an installation error can be rolled back.
        backup = staging / "backup"
        backup.mkdir()
        backup_profiles = backup / "profiles"
        backup_profiles.mkdir()

        old_files = list(PROFILE_DIR.glob("*.ovpn"))

        for old_file in old_files:
            shutil.copy2(old_file, backup_profiles / old_file.name)

        for old_path in (AUTH_FILE, JSON_FILE):
            if old_path.exists():
                shutil.copy2(old_path, backup / old_path.name)

        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            DATA_DIR.mkdir(parents=True, exist_ok=True)

            # Remove old profiles only after the new set is validated.
            for old_file in PROFILE_DIR.glob("*.ovpn"):
                old_file.unlink()

            for filename in sorted(profiles):
                shutil.copy2(
                    staged_profiles / filename,
                    PROFILE_DIR / filename,
                )

            shutil.copy2(staging / "auth.txt", AUTH_FILE)
            shutil.copy2(staging / "vpnbook.json", JSON_FILE)

        except Exception:
            # Restore the previous profile set and metadata on failure.
            for current in PROFILE_DIR.glob("*.ovpn"):
                current.unlink()

            for saved in backup_profiles.glob("*.ovpn"):
                shutil.copy2(saved, PROFILE_DIR / saved.name)

            for old_name, destination in (
                ("auth.txt", AUTH_FILE),
                ("vpnbook.json", JSON_FILE),
            ):
                saved = backup / old_name
                if saved.exists():
                    shutil.copy2(saved, destination)

            raise

    print(
        f"Updated {len(servers)} servers and {len(profiles)} profiles."
    )
    print(f"JSON: {JSON_FILE.relative_to(ROOT)}")
    print(f"Profiles: {PROFILE_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        RuntimeError,
        urllib.error.URLError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
