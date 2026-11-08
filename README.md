# tesla

Personal CLI for monitoring a Tesla vehicle and Powerwall through the
[Tesla Fleet API](https://developer.tesla.com/docs/fleet-api), built on
[python-tesla-fleet-api](https://github.com/Teslemetry/python-tesla-fleet-api).

Primary use case: watch the Powerwall and start car charging once it
reaches a threshold. It can also read status, send manual charging
commands, and check key/registration health.

[![CI](https://github.com/8cylinder/tesla/actions/workflows/ci.yml/badge.svg)](https://github.com/8cylinder/tesla/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-8cylinder.github.io%2Ftesla-blue.svg)](https://8cylinder.github.io/tesla/)
[![License: GPL-2.0-only](https://img.shields.io/badge/license-GPL--2.0--only-blue.svg)](https://github.com/8cylinder/tesla/blob/main/LICENSE)

Repository: <https://github.com/8cylinder/tesla> ·
Issues: <https://github.com/8cylinder/tesla/issues> ·
Docs: <https://8cylinder.github.io/tesla/>

## Requirements

- Python >= 3.13.
- [uv](https://docs.astral.sh/uv/) for dependency management.
- bash >= 4.4 for bash completions.
- bash-completion >= 2.18 for automatic completion loading.
- Linux or macOS. Windows is supported through WSL or Git Bash for the
  deployment scripts.

## Installation

The project is not published to PyPI; install it from source:

```bash
git clone https://github.com/8cylinder/tesla.git
cd tesla
make sync
make install
```

`make install` installs the `tesla` command, bash completions, and the
man page for the current user. It does not use `sudo`. Alternatively:

```bash
uv tool install --editable .
```

`uv tool install` installs only the command; run `tesla --completions`
and `make install-man` separately for completions and the man page. Every
command below can be run as `uv run tesla ...` from the repository or as
`tesla ...` after installation.

## Quick start

```bash
# List vehicles and energy sites to find the VIN and site ID.
tesla products

# Read the Powerwall and vehicle state.
tesla powerwall status
tesla car status

# Run one auto-charge evaluation without staying resident.
tesla auto-charge --once --dry-run
```

Full onboarding — creating the Tesla application, hosting the public
key, configuring credentials, and authorizing — is in the
[setup guide](https://8cylinder.github.io/tesla/setup.html). Deployment
options are in the
[deployment guide](https://8cylinder.github.io/tesla/deployment.html).

## Commands

| Command                                      | Purpose                                                              |
|----------------------------------------------|----------------------------------------------------------------------|
| `tesla auth login [--listen]`                | Headless OAuth; saves the refresh token                              |
| `tesla auth status`                          | Token validity; refreshes and persists rotation                      |
| `tesla auth logout`                          | Delete the local token (does not revoke it)                          |
| `tesla products`                             | List vehicles and energy sites with IDs                              |
| `tesla powerwall status [--watch]`           | Battery %, reserve, grid status                                      |
| `tesla powerwall notify [--level N]`         | Notify once per cycle when the battery reaches N%                    |
| `tesla car status`                           | Battery, range, charging state, plug, charge limit (wakes if asleep) |
| `tesla car charge start` / `stop`            | Start/stop charging (wakes the car)                                  |
| `tesla car charge limit <pct>`               | Set the charge limit (0–100)                                         |
| `tesla auto-charge [flags]`                  | Watch the Powerwall and trigger once                                 |
| `tesla key generate` / `register` / `status` | EC key and registration                                              |
| `tesla config show` / `set`                  | View/update configuration                                            |
| `tesla notify test` / `reset`                | Send a test notification / clear cooldowns                           |
| `tesla server info` / `logs`                 | Inspect the home-server Docker deployment over SSH                    |
| `tesla doctor`                               | Check config, credentials, auth, IDs, connectivity                   |

Global options: `-h`/`--help`, `--version`, `--config`, `--completions`,
`--color [auto|always|never]`, `-v`/`-vv` for logging, and `--log-file
FILE`. The full command reference is in the
[CLI documentation](https://8cylinder.github.io/tesla/cli.html); use
`tesla --help` or `tesla <command> --help` for the authoritative option
list.

## Configuration

The configuration lives in `config.toml` in the platform per-user config
directory. `tesla --config` prints the exact path; typical paths are:

| Platform | Default path                                  |
|----------|-----------------------------------------------|
| Linux    | `~/.config/tesla/config.toml`                 |
| macOS    | `~/Library/Application Support/tesla/config.toml` |
| Windows  | `%LOCALAPPDATA%\tesla\config.toml`            |

A missing file means built-in defaults. Example:

```toml
region = "na"                  # na, eu, or cn
domain = "your-domain.example"

[vehicle]
vin = "5YJ..."
command_mode = "signed"        # or "fleet" (unsigned fallback)

[powerwall]
energy_site_id = 123456789

[auto_charge]
threshold = 100.0
poll_interval_seconds = 300
require_plugged = true
wake_timeout_seconds = 90
set_charge_limit = 0           # 0 = leave unchanged
max_runtime_hours = 0          # 0 = unlimited
charge_on_solar = false        # true = CoS gate mode
stop_margin = 5.0              # latch charging off below threshold - margin

[notify]
enabled = false
service = "ntfy"               # or "pushover"
priority = 3
tags = "tesla"                 # ntfy only
min_priority = 1
verbose = false
on_start = true

[notify.ntfy]
url = "https://ntfy.sh"

[notify.pushover]
url = "https://api.pushover.net/1/messages.json"
device = ""
sound = ""

[server]
url = "beelink.local"          # ssh destination for `tesla server`
```

Write values with `tesla config set <dotted.key> <value>` and inspect the
effective result and its path with `tesla config show`. Precedence is
command-line option > config value > code default. Unknown keys produce a
warning and are ignored; wrong types are rejected with an error naming the
file.
Relative filesystem paths such as `vehicle.key_path` resolve against the
config file's directory, not the current working directory. Secrets stay
in the environment or `.env`; see the setup guide.

## Bash completions

Generate and install the static completion script:

```bash
tesla --completions
```

It prints the path (for example
`~/.local/share/bash-completion/completions/tesla.bash`) to stdout and a
`source` instruction to stderr:

```bash
source ~/.local/share/bash-completion/completions/tesla.bash
```

The script requires bash >= 4.4 and does not run Python or the app at tab
time. It is static: rerun `tesla --completions` after changing commands or
options. Automatic loading requires bash-completion >= 2.18 and the `.bash`
suffix. On macOS, `/bin/bash` is 3.2; install a newer bash (for example
`brew install bash`). zsh is not supported. `make install` runs
`tesla --completions` for you.

## Man page

`make install` builds and installs `tesla.1` with Sphinx:

```bash
man tesla
```

If the page is not found, add the install directory to `MANPATH`:

```bash
export MANPATH="${XDG_DATA_HOME:-$HOME/.local/share}/man:$MANPATH"
```

Installing from PyPI-style tools that only install the command does not
install the man page; use `make install` from a source checkout.

## Documentation website

The published documentation is at
<https://8cylinder.github.io/tesla/>. It includes the setup guide,
deployment notes, auto-charge semantics, notification details, and the
generated CLI reference.

Build the HTML site locally with:

```bash
make docs
```

Run the live preview server with:

```bash
make docs-serve
```

`README.md` remains the manually written landing page; it is included in
the Sphinx site rather than generated from it.

## Development

```bash
make sync      # install or update dependencies
make fmt       # format with ruff
make lint      # lint and apply safe fixes
make type      # type-check with ty
make test      # run pytest with branch coverage
make check     # fmt-check, lint-check, type, test, docs (no source changes)
make ci        # locked install, then check
```

Run one test with:

```bash
uv run pytest tests/test_cli.py::test_version
```

`make build` writes the wheel and sdist to `dist/`. Tests never touch the
real home directory, network, or system config; they use `tmp_path` and
`monkeypatch`.

## License

GPL-2.0-only. See
[LICENSE](https://github.com/8cylinder/tesla/blob/main/LICENSE).

Copyright (C) 2026 Sheldon McGrandle.

## Contributing and issues

Report bugs and request features at
<https://github.com/8cylinder/tesla/issues>. See
[CONTRIBUTING.md](https://github.com/8cylinder/tesla/blob/main/CONTRIBUTING.md)
for development notes and [SECURITY.md](https://github.com/8cylinder/tesla/blob/main/SECURITY.md)
for private vulnerability reports. Pull requests are welcome; run
`make check` before opening one.
