# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Add `tesla server` to inspect the remote Docker deployment over SSH:
  container state, image and tag, command, mounts, uptime, and logs.
- Show the config file, `.env`, token, and key paths in `tesla doctor`,
  and include the config path in `tesla config show`.

## [0.1.0] - 2026-11-07

- Tesla Fleet API CLI for Powerwall-aware vehicle charging.
- Headless OAuth, signed and fleet command modes, and EC key management.
- Powerwall status, level notifications, and auto-charge with an optional
  Charge on Solar gate.
- Configuration via platform per-user `config.toml`, plus `--config`,
  `--color`, and static bash completion generation.
