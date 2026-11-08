# Setup

The instructions below go from a bare machine to a working CLI. For a
long-running deployment, continue with {doc}`deployment`.

## 1. Get the code

```bash
git clone https://github.com/8cylinder/tesla.git tesla
cd tesla
```

Or download and unpack the repository archive. Nothing is published to
PyPI; this is a personal tool built from source.

## 2. Build

Install [uv](https://docs.astral.sh/uv/), which manages Python 3.13 for
the project:

```bash
make sync
uv run tesla --version
```

`make sync` creates `.venv` and installs pinned dependencies from
`uv.lock`. To install the command, bash completions, and man page for the
current user:

```bash
make install
# installs ~/.local/bin/tesla
```

Every command below can be run as either `uv run tesla ...` (from the
repo) or `tesla ...` (after `make install`).

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

| Field                | Value                                                                                                                               |
|----------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| Registration type    | Just for me (personal account)                                                                                                      |
| Application name     | anything, e.g. `HomeCLI`                                                                                                            |
| OAuth grant type     | **Authorization Code and Machine-to-Machine**                                                                                       |
| Allowed origin       | `https://your-domain.example` (scheme + host only, no path)                                                                         |
| Allowed redirect URI | `http://localhost:8085/callback` (localhost is exempt from Tesla's HTTPS rule)                                                      |
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

1. Generate the EC P-256 key in the config directory and print the public
   half:

   ```sh
   uv run tesla key generate --pem-only > /tmp/tesla-public-key.pem
   ```

   The private key is written beside `config.toml`; run
   `tesla --config` to see the exact directory.

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

`.env` is gitignored and read from the current directory or the config
directory (`tesla --config` prints the path, for example
`~/.config/tesla/` on Linux). Then write `config.toml` with
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
service = "ntfy"               # or "pushover"
priority = 3                   # default notification priority (1-5)
tags = "tesla"                 # comma-separated, ntfy only
min_priority = 1               # drop stages with a lower priority
verbose = false                # true = ignore cooldowns (observation mode)
on_start = true                # report the current state on start/restart

[notify.ntfy]
url = "https://ntfy.sh"        # or a self-hosted server

[notify.pushover]
url = "https://api.pushover.net/1/messages.json"
device = ""                    # optional device name; empty = all devices
sound = ""                     # optional sound name; empty = user's default
# Credentials are never stored here; use .env:
#   ntfy:     TESLA_NTFY_TOPIC, TESLA_NTFY_TOKEN, TESLA_NTFY_URL
#   pushover: TESLA_PUSHOVER_TOKEN, TESLA_PUSHOVER_USER, TESLA_PUSHOVER_URL

[server]
url = "beelink.local"          # ssh destination for `tesla server`
```

Set `server.url` to the home server's SSH destination to inspect the
Docker deployment with `tesla server info` and `tesla server logs`.

`tesla products` lists the VIN and energy site ID to use here. Unknown
keys produce a warning and are ignored; wrong types are rejected.
`tesla config show` prints the effective configuration and its path, and
`tesla --config` prints the file path.

## 6. Authorize (headless)

The server has SSH only, so the OAuth flow uses a manual code paste:

```bash
uv run tesla auth login
```

Open the printed URL on any phone or laptop, authorize, then copy the
full callback URL from the address bar (the page fails to load — that is
expected) and paste it back. Codes are short-lived; paste promptly.
`--listen` instead waits for the callback on `localhost:8085`, for users
who tunnel with `ssh -L 8085:localhost:8085 server`.

Verify everything end to end:

```bash
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
