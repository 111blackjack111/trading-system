# Server Access & Security

## VPS Info
- **IP**: 204.168.165.150
- **Provider**: Hetzner (ubuntu-4gb-hel1-1)
- **SSH**: `ssh root@204.168.165.150`

## SSH Key (authorized on server)
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEe+SnrXXXBvZfPzSwX7+8d8jN0adqX9f10I/zy0QbRn
```

## Host Key Fingerprint
```
SHA256:4NQlWm27BDv85uxNJ14opZQ8+V2iZRPrrxXb5AQoJVQ (ED25519)
```

## Local SSH Key Location
```
~/.ssh/id_ed25519 (private)
~/.ssh/id_ed25519.pub (public — matches authorized_keys on server)
```

## Security Setup (2026-03-25)
- **fail2ban**: installed, bans after 5 failed attempts for 1h
- **UFW firewall**: enabled, only ports 22 (SSH) + 8080 (dashboard)
- **SSH**: password auth DISABLED, key-only
- **Dashboard**: basic auth enabled (port 8080)

## Emergency Access
If locked out of SSH:
1. Use Hetzner console (web UI) — password auth still works on console
2. Or boot into rescue mode from Hetzner panel

## Ports
| Port | Service | Access |
|------|---------|--------|
| 22 | SSH | Key-only |
| 8080 | Dashboard | Basic auth |
