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

| Field | Value |
|---|---|
| Registration type | Just for me (personal account) |
| Application name | anything, e.g. `HomeCLI` |
| OAuth grant type | **Authorization Code and Machine-to-Machine** |
| Allowed origin | `https://your-domain.example` (scheme + host only, no path) |
| Allowed redirect URI | `http://localhost:8085/callback` (localhost is exempt from Tesla's HTTPS rule) |
| Scopes | `user_data`, `vehicle_device_data`, `vehicle_cmds`, `vehicle_charging_cmds`, `energy_device_data` (`openid`/`offline_access` are implicit) |

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

| Option | How it works | Pros | Cons |
|---|---|---|---|
| systemd timer + `--once` (recommended) | systemd launches `tesla auto-charge --once` every 5 min | No resident process; journald logs; survives reboot; cadence easy to change; no restart-semantics trap | Up to one interval of detection latency; process starts and refreshes the token each tick |
| systemd resident service | systemd runs `tesla auto-charge` continuously | Lowest latency; token refreshed only ~every 8 h; one process to watch | Must use `Restart=always` to re-arm after a trigger; long-lived process; re-checks after success |
| cron | cron runs `tesla auto-charge --once` on a schedule | Universally available; no systemd/linger; trivial to set up | Minimal environment; DIY log redirection; no journald or hardening; no catch-up after downtime |

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

| Command | Purpose |
|---|---|
| `tesla auth login [--listen]` | Headless OAuth; saves the refresh token |
| `tesla auth status` | Token validity; refreshes and persists rotation |
| `tesla auth logout` | Delete the local token (does not revoke it) |
| `tesla products` | List vehicles and energy sites with IDs |
| `tesla powerwall status [--watch]` | Battery %, reserve, grid status |
| `tesla car status` | Battery, range, charging state, plug, charge limit |
| `tesla car charge start` / `stop` | Start/stop charging (wakes the car) |
| `tesla car charge limit <pct>` | Set the charge limit (0–100) |
| `tesla auto-charge [flags]` | Watch the Powerwall and trigger once |
| `tesla key generate` / `register` / `status` | EC key and registration |
| `tesla config show` / `set` | View/update configuration |
| `tesla doctor` | Check config, credentials, auth, IDs, connectivity |
| `tesla --version` | Print the version |

Global flags: `-v`/`-vv` for INFO/DEBUG logging, `--log-file FILE` to log
to a file instead of stderr.

## Auto-charge semantics

`tesla auto-charge` acquires a single-instance lock, refreshes the
token, then polls the Powerwall every `poll_interval_seconds`. Below the
threshold it only reads the Powerwall. At the threshold it reads the car
and acts on this table:

| Powerwall | Vehicle | `require_plugged` | Action |
|---|---|---|---|
| >= threshold | Charging / Complete | any | done, exit 0 |
| >= threshold | Disconnected | true | wait for plug |
| >= threshold | Stopped / NoPower | any | wake, set limit, `charge_start` |
| >= threshold | asleep (offline) | true | wait, do not wake |
| >= threshold | asleep (offline) | false | wake and start |
| < threshold | any | any | sleep |

After a start it verifies `Charging` with a follow-up read and exits 0
(it never stays resident to stop charging later). Transient API errors
back off exponentially (5 s, x2, cap 300 s, ±20% jitter) and give up
after 5 consecutive failures. Exit codes: `0` triggered/nothing to do,
`2` config/auth error, `3` API/vehicle error.

Flags: `--threshold N`, `--interval S`, `--dry-run` (never wakes or
commands), `--once` (one evaluation pass, no sleeping).

## Troubleshooting

- `412 Precondition Failed` on `products`: the app has not completed
  partner registration — do step 4.
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
