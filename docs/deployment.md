# Deployment

The primary deployment is a private Docker image. It is built on the dev
machine, shipped to the server as a compressed tar, and run with Compose;
state and secrets stay in a host directory, so the image contains only
code and dependencies and nothing is ever published to a registry. The
systemd + uv alternative is described below.

## Docker deployment

The image is tagged `tesla-app:<version>-<git-sha>`. `scripts/deploy.sh`
builds it, saves it to `dist/`, copies it to the server, loads it, and
restarts the service. The server needs only Docker with Compose and the
host needs `docker`, `ssh`, `scp`, `gzip`, `uv`, and `git` (on Windows,
run the script from WSL or Git Bash).

On the server the deployment directory looks like:

```text
~/tesla/
├── compose.yaml        # copied by the deploy script
├── .env                # TESLA_TAG, TESLA_UID/GID, TZ (written by the script)
├── backup.sh           # copied by the deploy script
├── config/             # host config root (mode 700)
│   └── tesla/          # app state and secrets (files 600)
├── images/             # shipped image tars (current + previous kept)
└── backups/            # state archives from backup.sh
```

The container sets `XDG_CONFIG_HOME=/config`, so `platformdirs` resolves
the app config directory to `/config/tesla`, backed by
`~/tesla/config/tesla/` on the host.

### First deploy (bootstrap script)

On the authenticated development machine, `scripts/deploy-initial.sh`
automates the first-time setup: it runs the read-only preflight checks,
validates `config.toml`, copies `config.toml`, `tokens.json`, the optional
private key, and a minimal app `.env`, fixes ownership and modes, then
calls `scripts/deploy.sh`.

```bash
TESLA_SERVER="<DEPLOY-SERVER>" scripts/deploy-initial.sh
```

It refuses to overwrite existing server state unless `--force` is given,
supports `--dry-run`, and sends one remote notification test unless
`--no-test-notify` is used. Use `scripts/deploy.sh` for later updates.

Run `scripts/deploy-check.sh` first if you are unsure which path applies:
its summary reports whether the server is not bootstrapped, partially
bootstrapped, or already bootstrapped, and recommends `deploy-initial.sh`
or `deploy.sh` accordingly.

### Manual first deploy

1. Ship the image and create the directories:

   ```sh
   TESLA_SERVER=<ssh-host> scripts/deploy.sh
   ```

   The container starts but exits until `config/tesla/` has credentials;
   that is expected.

2. Copy state from the machine where you authorized:

   ```sh
   state=$(dirname "$(uv run tesla --config)")
   scp "$state/config.toml" "$state/tokens.json" \
       "$state/private_key.pem" <ssh-host>:tesla/config/tesla/

   # Build a minimal app .env; do not copy SSH/SSH_SUDO keys.
   grep -E '^(TESLA_CLIENT_ID|TESLA_CLIENT_SECRET|TESLA_REDIRECT_URI|TESLA_NTFY_TOPIC|TESLA_NTFY_TOKEN|TESLA_NTFY_URL|TESLA_PUSHOVER_TOKEN|TESLA_PUSHOVER_USER|TESLA_PUSHOVER_URL)=' .env \
       > /tmp/tesla-app.env
   scp /tmp/tesla-app.env <ssh-host>:tesla/config/tesla/.env
   rm /tmp/tesla-app.env

   ssh <ssh-host> 'chmod 600 ~/tesla/config/tesla/*'
   ```

3. Restart and verify:

   ```sh
   ssh <ssh-host> 'cd ~/tesla && docker compose restart'
   ssh <ssh-host> 'cd ~/tesla && docker compose exec tesla tesla doctor'
   ssh <ssh-host> 'docker logs -f tesla-app'
   ```

   The log should show the `Powerwall at N%; waiting for N%` lines,
   including the ETA once there are two readings.

### Update and roll back

```bash
TESLA_SERVER=<ssh-host> scripts/deploy.sh
```

Each deploy keeps the previous image and tar; the script prints the
one-line rollback (point `TESLA_TAG` back at the previous tag in
`~/tesla/.env` and `docker compose up -d`).

### Logs

`powerwall notify` logs to stdout/stderr, and Docker captures it with the
`json-file` driver. Follow the output live:

```bash
ssh <ssh-host> 'docker logs -f --tail 50 tesla-app'
```

Or use Compose from the deployment directory:

```bash
ssh <ssh-host> 'cd ~/tesla && docker compose logs -f --tail 50 tesla'
```

View recent history without following:

```bash
ssh <ssh-host> 'docker logs --tail 200 tesla-app'
ssh <ssh-host> 'docker logs --since 24h tesla-app'
ssh <ssh-host> 'docker logs --since 2026-11-07T00:00:00 tesla-app'
```

Add `--timestamps` for Docker's timestamps in addition to the app's own
timestamps, and filter as usual:

```bash
ssh <ssh-host> 'docker logs --timestamps --tail 50 tesla-app'
ssh <ssh-host> 'docker logs tesla-app 2>&1 | grep "Powerwall at"'
```

The watcher logs one `Powerwall at N%; waiting for N%` line per poll
(about every five minutes by default), plus stage changes and errors.

Docker rotates the logs using `max-size: 5m` and `max-file: 3` from
`compose.yaml`: at most three files, about 15 MB total. The oldest file is
deleted when a new one is created; rotated files are not gzipped.
`docker logs` reads the retained files, so `--since`/`--tail` work until
data falls off the oldest one.

Logs belong to the container, not the mounted `config/` volume. A
`docker compose restart` keeps them, but `deploy.sh` recreates the
container for a new image and the old logs are lost. To keep more
history, raise `max-size`/`max-file` in `deploy/compose.yaml`, or use
`--log-file /config/tesla/tesla.log` in the Compose command plus host
`logrotate`.

### Remote inspection

From a machine with SSH access to the server, point the CLI at the
deployment once:

```bash
tesla config set server.url beelink.local
```

Then inspect the container or read its logs without logging in to the
server:

```bash
tesla server info
tesla server logs --tail 100
tesla server logs -f
```

`info` reports the container state, image and tag, configured command
and arguments, mounts, and uptime by running `docker inspect tesla-app`
over SSH. It deliberately does not print the container environment.
`logs` wraps `docker logs`; `--follow` streams until interrupted and
`--tail` defaults to 200 lines. Pass `--url` to override the configured
host:

```bash
tesla server --url=other-host info
```

SSH host aliases from `~/.ssh/config` work as usual.

### Backups

`deploy/backup.sh` archives `config/` into `backups/` and keeps the
newest eight. Add it to the server crontab:

```text
0 4 * * 0 $HOME/tesla/backup.sh
```

The private key is the irreplaceable file; `tokens.json` can be re-fetched
with `tesla auth login`. To restore: `docker compose down`, unpack an
archive into `~/tesla/`, restore the file modes, and `docker compose up
-d`.

### Notes

- Secrets are never baked into the image (`XDG_CONFIG_HOME=/config`
  points at the host directory), so the tar holds only private code.
- The tar is architecture-specific (x86_64 today).
- When the web UI lands, uncomment `ports:` in `compose.yaml` and pick a
  free port following the server's `servername.local:port` convention.

## Alternative: systemd + uv

The CLI and its state are small: install uv, copy the project and state,
install the CLI, then schedule it.

### Install uv on the server

uv is a single static binary; the installer needs no root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# the installer prints how to put ~/.local/bin on PATH, e.g.
export PATH="$HOME/.local/bin:$PATH"
```

### Copy the project and state

```bash
# on the server
git clone https://github.com/8cylinder/tesla.git ~/tesla
cd ~/tesla
make sync
make install
uv run tesla --version

# from the machine where you authorized
scp -r ~/.config/tesla server:~/.config/tesla
# then on the server
chmod 600 ~/.config/tesla/tokens.json ~/.config/tesla/private_key.pem
# and place .env (client id/secret) in ~/tesla or ~/.config/tesla, mode 600
```

`make install` provides `~/.local/bin/tesla`, which the systemd unit
below uses. The hosted public key stays on the web host; only the
private key and tokens move to the server.

### Choose a scheduler

All three options run the same command; the lock file keeps them from
overlapping, so pick one and don't mix.

| Option                                 | How it works                                            | Pros                                                                                                   | Cons                                                                                             |
|----------------------------------------|---------------------------------------------------------|--------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| systemd timer + `--once` (recommended) | systemd launches `tesla auto-charge --once` every 5 min | No resident process; journald logs; survives reboot; cadence easy to change; no restart-semantics trap | Up to one interval of detection latency; process starts and refreshes the token each tick        |
| systemd resident service               | systemd runs `tesla auto-charge` continuously           | Lowest latency; token refreshed only ~every 8 h; one process to watch                                  | Must use `Restart=always` to re-arm after a trigger; long-lived process; re-checks after success |
| cron                                   | cron runs `tesla auto-charge --once` on a schedule      | Universally available; no systemd/linger; trivial to set up                                            | Minimal environment; DIY log redirection; no journald or hardening; no catch-up after downtime   |

### Schedule it with systemd (recommended)

The loop triggers once and exits 0, so a timer plus a oneshot service is
the cleanest fit:

```ini
# ~/.config/systemd/user/tesla-auto-charge.service
[Unit]
Description=Powerwall threshold charge check

[Service]
Type=oneshot
WorkingDirectory=%h/tesla
ExecStart=%h/.local/bin/tesla auto-charge --once
```

```ini
# ~/.config/systemd/user/tesla-auto-charge.timer
[Unit]
Description=Run the Powerwall charge check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now tesla-auto-charge.timer
loginctl enable-linger "$USER"          # keep running without login
journalctl --user -u tesla-auto-charge -f
```

The lock file prevents overlapping runs. Because access tokens are not
persisted, every run refreshes the token; if you want less rotation,
lengthen `OnUnitActiveSec` (e.g. 15–60 min).

### Or use cron

```
*/5 * * * * cd $HOME/tesla && $HOME/.local/bin/tesla auto-charge --once >> $HOME/.local/state/tesla/auto-charge.log 2>&1
```

### Optional: a resident service

If you prefer a long-running process (checks every
`poll_interval_seconds`), use `ExecStart=%h/.local/bin/tesla auto-charge
--log-file %h/.local/state/tesla/auto-charge.log` with
`Restart=on-failure`, `RestartSec=60`, `StartLimitIntervalSec=300`,
`StartLimitBurst=3`, and hardening (`NoNewPrivileges=`, `PrivateTmp=`,
`ProtectSystem=strict=`, `ReadWritePaths=%h/.config/tesla`). Note it
exits 0 after a trigger, so `Restart=on-failure` will not re-arm it;
use `Restart=always` with `RestartSec=300` or prefer the timer.
