# tesla

Personal CLI for monitoring a Tesla vehicle and Powerwall through the
[Tesla Fleet API](https://developer.tesla.com/docs/fleet-api), built on
[python-tesla-fleet-api](https://github.com/Teslemetry/python-tesla-fleet-api).

Primary use case: watch the Powerwall and start car charging once it
reaches a threshold. It can also read status, send manual charging
commands, and check key/registration health.

The instructions below go from a bare machine to a running service on a
home network server.

## 1. Get the code

```sh
git clone <this-repo-url> tesla
cd tesla
```

Or download and unpack the repository archive. Nothing is published to
PyPI; this is a personal tool built from source.

## 2. Build

Install [uv](https://docs.astral.sh/uv/), which manages Python 3.13 for
the project:

```sh
uv sync
uv run tesla --version
```

`uv sync` creates `.venv` and installs pinned dependencies from
`uv.lock`. For a global `tesla` command (used by the systemd unit below),
install the project as a tool:

```sh
uv tool install --editable .
# installs ~/.local/bin/tesla
```

Every command below can be run as either `uv run tesla ...` (from the
repo) or `tesla ...` (after `uv tool install`).

## 3. Create the Tesla developer application

Tesla requires an application registered to the same account that owns
the vehicle and Powerwall.

1. Create a Tesla account with a verified email and multi-factor
   authentication, if you do not have one.
2. Request app access at
   [developer.tesla.com](https://developer.tesla.com) (dashboard:
   `https://developer.tesla.com/dashboard`; request form:
   `https://developer.tesla.com/request`).
3. Use these settings:

| Field                | Value                                                                                                                                      |
|----------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| Registration type    | Just for me (personal account)                                                                                                             |
| Application name     | anything, e.g. `HomeCLI`                                                                                                                   |
| OAuth grant type     | **Authorization Code and Machine-to-Machine**                                                                                              |
| Allowed origin       | `https://your-domain.example` (scheme + host only, no path)                                                                                |
| Allowed redirect URI | `http://localhost:8085/callback` (localhost is exempt from Tesla's HTTPS rule)                                                             |
| Scopes               | `user_data`, `vehicle_device_data`, `vehicle_cmds`, `vehicle_charging_cmds`, `energy_device_data` (`openid`/`offline_access` are implicit) |

4. Billing: personal use includes a $10/month credit; vehicle data and
   command calls draw it down while energy-product APIs are free. A
   payment method is only needed if usage exceeds the credit.
5. Once approved, copy the **Client ID** and **Client Secret** from the
   dashboard (View Details -> Credentials & APIs). Keep the secret
   private.

## 4. Host the developer public key

Tesla requires a public key for the domain you registered as the origin,
and the app must be registered before **any** Fleet API call — reads
included.

1. Generate the EC P-256 key (written to
   `~/.config/tesla/private_key.pem`, mode 0600) and print the public
   half:

   ```sh
   uv run tesla key generate --pem-only > /tmp/tesla-public-key.pem
   ```

2. Upload that file to the domain from step 3, exactly at:

   ```
   https://your-domain.example/.well-known/appspecific/com.tesla.3p.public-key.pem
   ```

   It must be served over HTTPS, without redirects, byte-for-byte. Keep
   it online permanently — registration and vehicle pairing fetch it.
   The public host only serves this file; the private key stays on the
   machine running the CLI.

3. Set the domain and register the app with Tesla (uses the
   client-credentials grant):

   ```sh
   uv run tesla config set domain your-domain.example
   uv run tesla key register
   uv run tesla key status
   ```

   `key status` compares the local, Tesla-registered, and hosted keys. To
   use signed commands, pair the virtual key once per vehicle: open
   `https://tesla.com/_ak/your-domain.example` on a phone with the Tesla
   app.

## 5. Configure the CLI

Copy `.env.example` to `.env` and fill in the credentials from step 3:

```
TESLA_CLIENT_ID=...
TESLA_CLIENT_SECRET=...
TESLA_REDIRECT_URI=http://localhost:8085/callback
```

`.env` is gitignored and read from the current directory or
`~/.config/tesla/`. Then write `~/.config/tesla/config.toml` with
`tesla config set`, or create it by hand:

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
url = "https://ntfy.sh"        # or a self-hosted server
priority = 3                   # default notification priority (1-5)
tags = "tesla"                 # comma-separated, emoji shortcodes allowed
min_priority = 1               # drop stages with a lower priority
verbose = false                # true = ignore cooldowns (observation mode)
on_start = true                # report the current state on start/restart
# The topic and token are credentials and live in the environment:
#   TESLA_NTFY_TOPIC, TESLA_NTFY_TOKEN (optional), TESLA_NTFY_URL (optional)
```

`tesla products` lists the VIN and energy site ID to use here. Unknown
keys and wrong types are rejected; `tesla config show` prints the
effective configuration. The state directory can be overridden with
`TESLA_CONFIG_DIR`.

## 6. Authorize (headless)

The server has SSH only, so the OAuth flow uses a manual code paste:

```sh
uv run tesla auth login
```

Open the printed URL on any phone or laptop, authorize, then copy the
full callback URL from the address bar (the page fails to load — that is
expected) and paste it back. Codes are short-lived; paste promptly.
`--listen` instead waits for the callback on `localhost:8085`, for users
who tunnel with `ssh -L 8085:localhost:8085 server`.

Verify everything end to end:

```sh
uv run tesla auth status
uv run tesla doctor
uv run tesla powerwall status
uv run tesla car status
```

To de-authorize and start over: stop the service/timer, run
`uv run tesla auth logout`, then `uv run tesla auth login` again. Logout
only deletes the local token; to revoke server-side, remove the app's
access from your Tesla account (or change the password). Never run
`auth status` while the service is active — both rotate the refresh
token.

## 7. Deploy to a home network server

The primary deployment is a private Docker image. It is built on the dev
machine, shipped to the server as a compressed tar, and run with Compose;
state and secrets stay in a host directory, so the image contains only
code and dependencies and nothing is ever published to a registry. The
systemd + uv alternative is
[described below](#alternative-systemd--uv).

### Docker deployment

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
├── config/             # state and secrets (mode 700; files 600)
├── images/             # shipped image tars (current + previous kept)
└── backups/            # state archives from backup.sh
```

#### First deploy (bootstrap script)

On the authenticated development machine, `scripts/deploy-initial.sh`
automates the first-time setup: it runs the read-only preflight checks,
validates `config.toml`, copies `config.toml`, `tokens.json`, the optional
private key, and a minimal app `.env`, fixes ownership and modes, then
calls `scripts/deploy.sh`.

```sh
TESLA_SERVER=sm@beelink.local scripts/deploy-initial.sh
```

It refuses to overwrite existing server state unless `--force` is given,
supports `--dry-run`, and sends one remote ntfy test unless
`--no-test-notify` is used. Use `scripts/deploy.sh` for later updates.

Run `scripts/deploy-check.sh` first if you are unsure which path applies:
its summary reports whether the server is not bootstrapped, partially
bootstrapped, or already bootstrapped, and recommends `deploy-initial.sh`
or `deploy.sh` accordingly.

#### Manual first deploy

1. Ship the image and create the directories:

   ```sh
   TESLA_SERVER=<ssh-host> scripts/deploy.sh
   ```

   The container starts but exits until `config/` has credentials; that
   is expected.

2. Copy state from the machine where you authorized:

   ```sh
   scp ~/.config/tesla/config.toml ~/.config/tesla/tokens.json \
       ~/.config/tesla/private_key.pem <ssh-host>:tesla/config/

   # Build a minimal app .env; do not copy SSH/SSH_SUDO keys.
   grep -E '^(TESLA_CLIENT_ID|TESLA_CLIENT_SECRET|TESLA_REDIRECT_URI|TESLA_NTFY_TOPIC|TESLA_NTFY_TOKEN|TESLA_NTFY_URL)=' .env \
       > /tmp/tesla-app.env
   scp /tmp/tesla-app.env <ssh-host>:tesla/config/.env
   rm /tmp/tesla-app.env

   ssh <ssh-host> 'chmod 600 ~/tesla/config/*'
   ```

3. Restart and verify:

   ```sh
   ssh <ssh-host> 'cd ~/tesla && docker compose restart'
   ssh <ssh-host> 'cd ~/tesla && docker compose exec tesla tesla doctor'
   ssh <ssh-host> 'docker logs -f tesla-app'
   ```

   The log should show the `Powerwall at N%; waiting for N%` lines,
   including the ETA once there are two readings.

#### Update and roll back

```sh
TESLA_SERVER=<ssh-host> scripts/deploy.sh
```

Each deploy keeps the previous image and tar; the script prints the
one-line rollback (point `TESLA_TAG` back at the previous tag in
`~/tesla/.env` and `docker compose up -d`).

#### Logs

`powerwall notify` logs to stdout/stderr, and Docker captures it with the
`json-file` driver. Follow the output live:

```sh
ssh <ssh-host> 'docker logs -f --tail 50 tesla-app'
```

Or use Compose from the deployment directory:

```sh
ssh <ssh-host> 'cd ~/tesla && docker compose logs -f --tail 50 tesla'
```

View recent history without following:

```sh
ssh <ssh-host> 'docker logs --tail 200 tesla-app'
ssh <ssh-host> 'docker logs --since 24h tesla-app'
ssh <ssh-host> 'docker logs --since 2026-11-07T00:00:00 tesla-app'
```

Add `--timestamps` for Docker's timestamps in addition to the app's own
timestamps, and filter as usual:

```sh
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
`--log-file /config/tesla.log` in the Compose command plus host
`logrotate`.

#### Backups

`deploy/backup.sh` archives `config/` into `backups/` and keeps the
newest eight. Add it to the server crontab:

```cron
0 4 * * 0 $HOME/tesla/backup.sh
```

The private key is the irreplaceable file; `tokens.json` can be re-fetched
with `tesla auth login`. To restore: `docker compose down`, unpack an
archive into `~/tesla/`, restore the file modes, and `docker compose up
-d`.

#### Notes

- Secrets are never baked into the image (`TESLA_CONFIG_DIR=/config`
  points at the host directory), so the tar holds only private code.
- The tar is architecture-specific (x86_64 today).
- When the web UI lands, uncomment `ports:` in `compose.yaml` and pick a
  free port following the server's `servername.local:port` convention.

### Alternative: systemd + uv

The CLI and its state are small: install uv, copy the project and state,
install the CLI, then schedule it.

### Install uv on the server

uv is a single static binary; the installer needs no root:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
# the installer prints how to put ~/.local/bin on PATH, e.g.
export PATH="$HOME/.local/bin:$PATH"
```

### Copy the project and state

```sh
# on the server
git clone <this-repo-url> ~/tesla
cd ~/tesla
uv sync
uv tool install --editable .
uv run tesla --version

# from the machine where you authorized
scp -r ~/.config/tesla server:~/.config/tesla
# then on the server
chmod 600 ~/.config/tesla/tokens.json ~/.config/tesla/private_key.pem
# and place .env (client id/secret) in ~/tesla or ~/.config/tesla, mode 600
```

`uv tool install --editable .` provides `~/.local/bin/tesla`, which the
systemd unit below uses. The hosted public key stays on the web host;
only the private key and tokens move to the server.

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

```sh
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
| `tesla doctor`                               | Check config, credentials, auth, IDs, connectivity                   |
| `tesla --version`                            | Print the version                                                    |

Global flags: `-v`/`-vv` for INFO/DEBUG logging, `--log-file FILE` to log
to a file instead of stderr.

## Auto-charge semantics

`tesla auto-charge` acquires a single-instance lock, refreshes the
token, then polls the Powerwall every `poll_interval_seconds`. Below the
threshold it only reads the Powerwall. At the threshold it reads the car
and acts on this table:

| Powerwall    | Vehicle             | `require_plugged` | Action                          |
|--------------|---------------------|-------------------|---------------------------------|
| >= threshold | Charging / Complete | any               | done, exit 0                    |
| >= threshold | Disconnected        | true              | wait for plug                   |
| >= threshold | Stopped / NoPower   | any               | wake, set limit, `charge_start` |
| >= threshold | asleep (offline)    | true              | wait, do not wake               |
| >= threshold | asleep (offline)    | false             | wake and start                  |
| < threshold  | any                 | any               | sleep                           |

After a start it verifies `Charging` with a follow-up read and exits 0
(it never stays resident to stop charging later). Transient API errors
back off exponentially (5 s, x2, cap 300 s, ±20% jitter) and give up
after 5 consecutive failures. Exit codes: `0` triggered/nothing to do,
`2` config/auth error, `3` API/vehicle error.

Flags: `--threshold N`, `--interval S`, `--dry-run` (never wakes or
commands), `--once` (one evaluation pass, no sleeping).

### Charge on Solar gate mode

With `auto_charge.charge_on_solar = true`, the loop stops being a
one-shot trigger and becomes a gate around the charge switch while
**Charge on Solar** stays enabled in the Tesla app (the CLI never
changes the CoS setting):

- Start charging at `threshold` (set it to 95% for the Powerwall-first
  behavior) when the car is stopped and its SOC is at or above the CoS
  lower limit.
- Leave CoS alone while it charges on surplus solar, pauses for clouds,
  or reports `POWERWALL_CHARGE_PRIORITY`.
- Below `threshold - stop_margin` (default 5%, so below 90%), latch
  charging off with `charge_stop`; CoS stays enabled but will not resume
  by itself until the loop sends `charge_start` again at the threshold.
- Stop any non-solar charging (`charging_on_anything`) and alert.
- Never start when CoS is disabled or the car SOC is below the CoS
  lower limit (that would allow any-source charging and drain the
  Powerwall).

The decision function is a flat truth table over
`charge_state.managed_charging_state.charge_on_solar_state` and the
Powerwall reading; the executor performs the wake/start/stop. If the
REST `vehicle_data` call does not carry `managed_charging_state` on a
given firmware, the loop falls back to `charging_state`. `tesla car
status` shows CoS enabled/disabled, the live state and reason, and the
lower/upper limits.

```sh
uv run tesla config set auto_charge.charge_on_solar true
uv run tesla config set auto_charge.threshold 95
uv run tesla config set auto_charge.stop_margin 5
```

`--once` performs one evaluation pass (timer mode); a resident
`tesla auto-charge` keeps running and latches/re-arms across the day.
Exit codes and backoff are unchanged.

## Notifications with ntfy

The CLI can push [ntfy](https://ntfy.sh) notifications at each
auto-charge stage and after manual charging commands.

### Get started

1. Install the ntfy app
   ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)) or open the web app
   at <https://ntfy.sh/app>, and subscribe to a topic.

2. Pick a hard-to-guess topic. On public ntfy.sh the topic name is
   effectively a **password** — anyone who knows it can read your
   notifications. The web app includes a topic generator.

3. Put the credentials in `.env` next to the Tesla ones (or export them):

   ```shell
   TESLA_NTFY_TOPIC=tesla-<unguessable>
   # optional, for a protected topic or self-hosted server:
   TESLA_NTFY_TOKEN=tk_...
   TESLA_NTFY_URL=https://ntfy.example.com
   ```

4. Enable it and send a test:

   ```shell
   uv run tesla config set notify.enabled true
   uv run tesla notify test
   ```

### Powerwall level watch

`tesla powerwall notify [--level N]` watches the battery level and sends
one notification each charge cycle when it reaches N% (default 100). It
does not re-arm until the level drops below N, so leaving it running as
a service produces a single push per cycle. `--level`/`-l` accepts a
percentage in (0, 100], and `--interval` (default 300 s) sets the poll
rate. The latch is in memory, so a process restart re-arms it.

While waiting, each poll logs a straight-line projection of when the
level will be reached (`eta: 1:10pm`) fitted over the last five
readings; it is omitted while the level is flat or falling. Treat it as
a rough estimate, since solar charging rarely holds a steady rate.

After the notification, polling slows to once an hour until the level
drops below N again. The normal `--interval` rate then resumes and the
ETA is recalculated from fresh readings, so the next cycle is watched
in full.

```shell
uv run tesla powerwall notify --level 90
```

### What gets sent

| Stage                        | When                                                                                        | Priority |
|------------------------------|---------------------------------------------------------------------------------------------|----------|
| on_start                     | watcher starts/restarts: snapshot of the current Powerwall/situation                        | 2        |
| wait_plug                    | entered "at threshold, waiting": asleep, unplugged with `require_plugged`, or unknown state | 3        |
| charging                     | `charge_start` verified `Charging`                                                          | 4        |
| already_charging             | entered "at threshold, already charging/complete"                                           | 2        |
| charging_paused              | CoS-gate only: paused for insufficient solar, will resume                                   | 3        |
| charging_stopped             | CoS-gate only: latched off below `threshold - stop_margin`                                  | 3        |
| waiting_powerwall            | CoS-gate only: waiting for the Powerwall to reach `threshold`                               | 3        |
| cos_disabled                 | CoS-gate only: Charge on Solar not enabled, not starting                                    | 5        |
| cos_lower_limit              | CoS-gate only: car below the CoS lower limit, not starting                                  | 3        |
| charging_on_grid             | CoS-gate only: grid/Powerwall charging detected, stopped                                    | 4        |
| trigger_failed / poll_failed | wake, command, verification, or poll error                                                  | 4 / 3    |
| gave_up                      | 5 consecutive failures                                                                      | 5        |
| auth_failed                  | token problem; re-run `tesla auth login`                                                    | 5        |
| max_runtime                  | `max_runtime_hours` reached before a trigger                                                | 4        |
| stopped                      | SIGINT/SIGTERM in resident mode                                                             | 3        |
| manual                       | after `car charge start` / `stop` / `limit`                                                 | 3        |

`on_start` sends one snapshot when the watcher starts, so you always know
the current state: on every process start for the resident loop, and for
`--once` timer runs when there is no prior run or the gap since the last
run exceeded two poll intervals (i.e. after a reboot, sleep, or service
stop — not on every 5-minute tick). The situation it reports also
suppresses the immediate duplicate condition message.

Condition notifications (wait_plug, charging, already_charging) are sent
only when the situation **changes**. Below-threshold polls silently
update the remembered situation, so the next threshold crossing notifies
again. The situation and last-run time are persisted in
`~/.config/tesla/notify_state.json`, so it works the same for the
resident loop and for per-run systemd timer invocations — no repeated
pushes while nothing changes. Failure and lifecycle stages are events and
are sent whenever they happen. `--dry-run` sends nothing and does not
change the remembered situation. A failed publish is logged and ignored;
it never affects the watch loop.

### Tuning what you get

- `notify.min_priority` drops lower-value stages. For example,
  `tesla config set notify.min_priority 4` leaves only threshold,
  charging, trigger_failed, max_runtime, and the priority-5 failures.
- `notify.verbose true` ignores both the situation-change check and the
  cooldowns, so every stage is sent every run — handy while learning
  what the service does. Turn it back off for normal operation.
  `tesla notify reset` clears the remembered situation and cooldowns
  (useful to re-see a stage immediately, or to force the `on_start`
  snapshot on the next run).
- `notify.tags` and the per-stage tags render as emoji/text prefixes and
  are the easiest visual classifier (`battery`, `electric_plug`, `zap`,
  `warning`, `rotating_light`).

iPhone note: the ntfy iOS app has no per-priority channels and cannot
pause a subscription (Android can), so every ntfy alert looks and sounds
the same at the iOS level. Tune on the server with the settings above
rather than in the app; Android users additionally get per-priority
notification channels they can customize.

Verbose mode on a 5-minute timer would exceed ntfy.sh's 250 messages/day
(normal mode only sends when something changes) — use manual `--once`
runs while observing, or temporarily lengthen `OnUnitActiveSec`.

## Troubleshooting

- `412 Precondition Failed` on `products`: the app has not completed
  partner registration — do step 4.
- `charge trigger failed: ... Content-Type ... application/json`: a
  known tesla-fleet-api issue where bodyless POSTs (`wake_up`, unsigned
  commands) omit the JSON Content-Type; this CLI patches it in
  `client.py` (`_fix_empty_json`). Remove the patch when upstream fixes
  the default.
- `charge_start was sent but the vehicle is not charging`: the CLI waits
  about 60 s (six reads, 10 s apart) before giving up and logs the state
  it last saw. Common causes: Tesla's **Charge on Solar** is enabled (it
  owns charging while waiting for solar), a scheduled/departure-time
  charge is set, or the battery is already at its charge limit. Turn off
  Charge on Solar if you want this CLI to command charging
  unconditionally, or switch to the CoS gate
  (`auto_charge.charge_on_solar = true`), which expects CoS to be parked
  and does not treat that as a failure.
- `Tesla did not return a refresh token` or other auth errors: re-run
  `tesla auth login`; authorization codes expire quickly.
- `tokens.json is mode 0644`: the CLI refuses to read a token file with
  group/other permissions — `chmod 600`.
- The car asleep with `require_plugged = true` is reported as waiting,
  without waking it; set `auto_charge.require_plugged false` to allow
  charging regardless.
- Never run `tesla auth status` while the service is running: both
  rotate the refresh token, and only `auto-charge` is protected by the
  lock file.
- More detail: `tesla -vv <command>` or `--log-file`.
