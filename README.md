# thock-vpngate
# THOCK VPN Gate Server Aggregator

Automatically updated VPN Gate OpenVPN server directory for THOCK VPN.

This repository collects public VPN Gate server information, validates
OpenVPN configurations, ranks available servers, and publishes the latest
server list for Android clients.

---

## Features

- Automatic VPN Gate server discovery
- Automatic update every hour
- OpenVPN server filtering
- OpenVPN profile validation
- Server duplicate removal
- Speed-based ranking
- Ping-based ranking
- Session/load consideration
- Automatic quality score
- CSV output
- JSON output
- OpenVPN profile output
- GitHub Actions automation
- Android-friendly API
- Manual workflow execution
- Server fallback support

---

## Architecture

```text
                    VPN Gate
                       │
                       │ Public server list
                       ▼
              GitHub Actions
                       │
                       │ Every 1 hour
                       ▼
              Python Updater
                       │
          ┌────────────┼────────────┐
          │            │            │
          ▼            ▼            ▼
       Validate      Rank        Filter
       OpenVPN      Servers      Servers
          │            │            │
          └────────────┼────────────┘
                       │
                       ▼
                  Generated Data
                       │
             ┌─────────┼─────────┐
             │         │         │
             ▼         ▼         ▼
        servers.csv  servers.json profiles/
             │         │         │
             └─────────┼─────────┘
                       │
                       ▼
                  THOCK Android
                       │
                       ▼
             ServerRepository
                       │
                       ▼
               ServerSelector
                       │
                       ▼
                OpenVPN Profile
                       │
                       ▼
        vpnprotocols-openvpn:3.0.4
                       │
                       ▼
                 Android VPN
