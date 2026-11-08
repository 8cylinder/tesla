# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Add Pushover as an alternative notification service. Select it with
  `notify.service = "pushover"`, configure it under `[notify.pushover]`, and
  set the application token and user key in
  `TESLA_PUSHOVER_TOKEN`/`TESLA_PUSHOVER_USER` (never in `config.toml`).
  The ntfy URL moved to `[notify.ntfy]`; older `notify.url` values are still
  accepted.
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
