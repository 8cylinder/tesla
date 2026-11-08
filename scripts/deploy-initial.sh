#!/usr/bin/env bash
# One-time bootstrap for a fresh home-server deployment.
#
# Runs the read-only preflight checks, validates the local Tesla state,
# copies config/tokens/key and a minimal app .env, then calls deploy.sh.
#
# Usage:
#   TESLA_SERVER="<DEPLOY-SERVER>" scripts/deploy-initial.sh
#   scripts/deploy-initial.sh [user@host]
#
# Env:
#   TESLA_SERVER       destination ssh host (default: <DEPLOY-SERVER>)
#   TESLA_DIR          deployment directory under the server home (default: tesla)
#   XDG_CONFIG_HOME    config root on Linux (default: ~/.config)
#
# Flags:
#   --force            replace existing server config/tokens
#   --yes, -y          do not prompt (required with --force when non-interactive)
#   --dry-run          show what would happen; make no changes
#   --no-test-notify   skip the remote ntfy test
#   --skip-preflight   skip deploy-check.sh
#   --server HOST      destination ssh host
#   -h, --help

set -euo pipefail
umask 077

DEFAULT_SERVER="<DEPLOY-SERVER>"
SERVER="${TESLA_SERVER:-$DEFAULT_SERVER}"
TESLA_DIR="${TESLA_DIR:-tesla}"
if [ "$(uname -s)" = "Darwin" ]; then
  CONFIG_DIR="$HOME/Library/Application Support/tesla"
else
  CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tesla"
fi
CONFIG_FILE="$CONFIG_DIR/config.toml"
TOKENS_FILE="$CONFIG_DIR/tokens.json"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE=0
ASSUME_YES=0
DRY_RUN=0
TEST_NOTIFY=1
SKIP_PREFLIGHT=0
SERVER_STOPPED=0

usage() {
  cat <<'EOF'
One-time bootstrap for a fresh home-server deployment.

Usage:
  TESLA_SERVER="<DEPLOY-SERVER>" scripts/deploy-initial.sh
  scripts/deploy-initial.sh [user@host]

Env:
  TESLA_SERVER       destination ssh host (default: <DEPLOY-SERVER>)
  TESLA_DIR          deployment directory under the server home (default: tesla)
  XDG_CONFIG_HOME    config root on Linux (default: ~/.config)

Flags:
  --force            replace existing server config/tokens
  --yes, -y          do not prompt (required with --force when non-interactive)
  --dry-run          show what would happen; make no changes
  --no-test-notify   skip the remote ntfy test
  --skip-preflight   skip deploy-check.sh
  --server HOST      destination ssh host
  -h, --help         show this help
EOF
}

if [ -t 1 ]; then
  RED=$'\033[31m'
  GREEN=$'\033[32m'
  YELLOW=$'\033[33m'
  BOLD=$'\033[1m'
  RESET=$'\033[0m'
else
  RED=""
  GREEN=""
  YELLOW=""
  BOLD=""
  RESET=""
fi

say() {
  printf '\n%s==>%s %s\n' "$BOLD" "$RESET" "$*"
}

info() {
  printf '    %s\n' "$*"
}

warn() {
  printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$*"
}

die() {
  printf '\n%s[ERROR]%s %s\n' "$RED" "$RESET" "$*" >&2
  if [ "$SERVER_STOPPED" -eq 1 ]; then
    printf 'The server container was stopped; restart it with:\n' >&2
    printf '  ssh %s "cd ~/%s && docker compose up -d"\n' "$SERVER" "$TESLA_DIR" >&2
  fi
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force)
      FORCE=1
      ;;
    --yes|-y)
      ASSUME_YES=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --no-test-notify)
      TEST_NOTIFY=0
      ;;
    --skip-preflight)
      SKIP_PREFLIGHT=1
      ;;
    --server)
      SERVER="${2:?--server needs a value}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      SERVER="$1"
      ;;
  esac
  shift
done

SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=yes
  -o UpdateHostKeys=no
  -o ControlMaster=no
  -o ControlPath=none
)
SCP_OPTS=(
  -q
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=yes
  -o UpdateHostKeys=no
)

file_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1" 2>/dev/null || true
  fi
}

env_get() {
  local key="$1" line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
  [ -n "$line" ] || return 1
  line="${line#*=}"
  line="${line%$'\r'}"
  line="${line#\"}"
  line="${line%\"}"
  line="${line#\'}"
  line="${line%\'}"
  printf '%s\n' "$line"
}

for cmd in ssh scp gzip docker uv git; do
  command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required (see README step 7)"
done

[ -f "$REPO/scripts/deploy.sh" ] || die "missing $REPO/scripts/deploy.sh"
[ -f "$REPO/scripts/deploy-check.sh" ] || die "missing $REPO/scripts/deploy-check.sh"
[ -x "$REPO/scripts/deploy.sh" ] || die "scripts/deploy.sh is not executable"

if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
  say "Preflight checks (read-only)"
  if ! (cd "$REPO" && bash scripts/deploy-check.sh "$SERVER"); then
    die "preflight failed; fix the FAIL items above, or re-run with --skip-preflight"
  fi
else
  warn "skipping preflight checks"
fi

say "Validating local Tesla state"

[ -f "$CONFIG_FILE" ] || die "missing $CONFIG_FILE; run 'uv run tesla config show' to inspect the active config"
[ -f "$TOKENS_FILE" ] || die "missing $TOKENS_FILE; run 'uv run tesla auth login'"
info "config: $CONFIG_FILE"
info "tokens: $TOKENS_FILE"

TOKENS_MODE="$(file_mode "$TOKENS_FILE")"
[ -n "$TOKENS_MODE" ] || die "cannot read the mode of $TOKENS_FILE"
if [ $((8#$TOKENS_MODE & 077)) -ne 0 ]; then
  die "$TOKENS_FILE is mode $TOKENS_MODE; run: chmod 600 $TOKENS_FILE"
fi
info "tokens mode: $TOKENS_MODE"

PYTHON=()
if command -v python3 >/dev/null 2>&1 && python3 -c 'import tomllib' >/dev/null 2>&1; then
  PYTHON=(python3)
elif command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run --project "$REPO" --quiet python)
fi
[ "${#PYTHON[@]}" -gt 0 ] || die "Python 3.11+ or uv is required to read config.toml"

CONFIG_INFO="$("${PYTHON[@]}" - "$CONFIG_FILE" 2>/dev/null <<'PY'
import sys

try:
    import tomllib
except Exception:
    print("error=tomllib unavailable")
    raise SystemExit(0)

try:
    with open(sys.argv[1], "rb") as handle:
        cfg = tomllib.load(handle)
except Exception as exc:
    print(f"error={exc}")
    raise SystemExit(0)

def table(name):
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}

def emit(key, value):
    print(f"{key}={'' if value is None else value}")

powerwall = table("powerwall")
notify = table("notify")
vehicle = table("vehicle")

emit("region", cfg.get("region", "na"))
emit("domain", cfg.get("domain"))
emit("site_id", powerwall.get("energy_site_id"))
emit("notify_enabled", "true" if notify.get("enabled") is True else "false")
emit("notify_url", notify.get("url", "https://ntfy.sh"))
emit("priority", notify.get("priority", 3))
emit("min_priority", notify.get("min_priority", 1))
emit("key_path", vehicle.get("key_path"))
PY
)" || true

[ -n "$CONFIG_INFO" ] || die "could not read $CONFIG_FILE"
if printf '%s\n' "$CONFIG_INFO" | grep -q '^error='; then
  reason="$(printf '%s\n' "$CONFIG_INFO" | grep -m1 '^error=' | cut -d= -f2- || true)"
  die "cannot parse $CONFIG_FILE: $reason"
fi

cfg_get() {
  printf '%s\n' "$CONFIG_INFO" | grep -m1 "^$1=" | cut -d= -f2- || true
}

SITE_ID="$(cfg_get site_id)"
[ -n "$SITE_ID" ] || die "powerwall.energy_site_id is not set in $CONFIG_FILE; run:
  uv run tesla products
  uv run tesla config set powerwall.energy_site_id <id>"

NOTIFY_ENABLED="$(cfg_get notify_enabled)"
[ "$NOTIFY_ENABLED" = true ] || die "notify.enabled is not true in $CONFIG_FILE; run: uv run tesla config set notify.enabled true"

REGION="$(cfg_get region)"
[ -n "$REGION" ] || REGION="na"
case "$REGION" in
  na|eu|cn) ;;
  *) die "region must be one of na, eu, cn (got '$REGION')" ;;
esac

DOMAIN="$(cfg_get domain)"
if [ -z "$DOMAIN" ]; then
  warn "domain is not set in $CONFIG_FILE; key registration and vehicle commands may fail"
fi

PRIORITY="$(cfg_get priority)"
case "$PRIORITY" in
  1|2|3|4|5) ;;
  *) die "notify.priority must be 1-5 (got '$PRIORITY')" ;;
esac

MIN_PRIORITY="$(cfg_get min_priority)"
case "$MIN_PRIORITY" in
  1|2|3|4|5) ;;
  *) die "notify.min_priority must be 1-5 (got '$MIN_PRIORITY')" ;;
esac

NOTIFY_URL="$(cfg_get notify_url)"
[ -n "$NOTIFY_URL" ] || die "notify.url must not be empty"

KEY_PATH="$(cfg_get key_path)"
if [ -n "$KEY_PATH" ]; then
  KEY_PATH="${KEY_PATH/#\~/$HOME}"
  case "$KEY_PATH" in
    /*) ;;
    *) KEY_PATH="$CONFIG_DIR/$KEY_PATH" ;;
  esac
else
  KEY_PATH="$CONFIG_DIR/private_key.pem"
fi

info "region: $REGION"
info "energy site: $SITE_ID"
info "notify: enabled (priority $PRIORITY, min $MIN_PRIORITY)"

procs="$(ps ax -o pid=,command= 2>/dev/null | grep -E '[t]esla.*(powerwall notify|auto-charge)' || true)"
if [ -n "$procs" ]; then
  die "a local tesla watcher is running; stop it before bootstrapping:
$procs"
fi

ENV_FILE=""
for candidate in "$REPO/.env" "$CONFIG_DIR/.env"; do
  if [ -f "$candidate" ]; then
    ENV_FILE="$candidate"
    break
  fi
done
[ -n "$ENV_FILE" ] || die "no app .env found (looked in $REPO/.env and $CONFIG_DIR/.env)"
info "app env: $ENV_FILE"

for key in TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REDIRECT_URI TESLA_NTFY_TOPIC; do
  value="$(env_get "$key" || true)"
  [ -n "$value" ] || die "$key is missing or empty in $ENV_FILE"
done

case "$(env_get TESLA_CLIENT_ID || true)" in
  your-*|*your-client*|*changeme*)
    die "TESLA_CLIENT_ID still looks like a placeholder"
    ;;
esac
case "$(env_get TESLA_CLIENT_SECRET || true)" in
  your-*|*your-secret*|*changeme*)
    die "TESLA_CLIENT_SECRET still looks like a placeholder"
    ;;
esac

topic="$(env_get TESLA_NTFY_TOPIC || true)"
case "$topic" in
  *'<unguessable>'*)
    die "TESLA_NTFY_TOPIC still has the example placeholder"
    ;;
esac
if [ "${#topic}" -lt 12 ]; then
  warn "TESLA_NTFY_TOPIC is short; it acts as a password on public ntfy.sh"
fi

if [ -n "$(env_get TESLA_NTFY_TOKEN || true)" ]; then
  info "ntfy token: set"
fi
if [ -n "$(env_get TESLA_NTFY_URL || true)" ]; then
  info "ntfy url: $(env_get TESLA_NTFY_URL || true)"
fi
if [ -n "$(env_get SSH || true)" ] || [ -n "$(env_get SSH_SUDO || true)" ]; then
  warn "$ENV_FILE contains SSH/SSH_SUDO; only TESLA_* and NTFY_* keys will be copied"
fi

HAVE_KEY=0
if [ -f "$KEY_PATH" ]; then
  HAVE_KEY=1
  info "private key: $KEY_PATH"
else
  warn "private key not found at $KEY_PATH; skipping it (powerwall notify does not need it)"
fi

TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/tesla-app-env.XXXXXX")"
cleanup() {
  rm -f "${TMP_ENV:-}"
}
trap cleanup EXIT

for key in TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REDIRECT_URI TESLA_NTFY_TOPIC TESLA_NTFY_TOKEN TESLA_NTFY_URL; do
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
  if [ -n "$line" ]; then
    printf '%s\n' "${line%$'\r'}" >> "$TMP_ENV"
  fi
done
for key in TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REDIRECT_URI TESLA_NTFY_TOPIC; do
  grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$TMP_ENV" \
    || die "could not build the app .env: $key missing"
done
chmod 600 "$TMP_ENV"

say "Checking server state"
if ! ssh "${SSH_OPTS[@]}" "$SERVER" true >/dev/null 2>&1; then
  die "cannot SSH non-interactively to $SERVER; accept the host key once (ssh $SERVER) and check key auth"
fi

REMOTE_DIR="\$HOME/$TESLA_DIR"
existing="$(ssh "${SSH_OPTS[@]}" "$SERVER" \
  "for f in config.toml tokens.json .env; do [ -e \"$REMOTE_DIR/config/tesla/\$f\" ] && echo \"\$f\"; done" \
  2>/dev/null || true)"

if [ -n "$existing" ]; then
  warn "server already has: $(printf '%s' "$existing" | tr '\n' ' ')"
  if [ "$FORCE" -ne 1 ]; then
    die "refusing to overwrite existing state; re-run with --force"
  fi
  if [ "$ASSUME_YES" -ne 1 ] && [ "$DRY_RUN" -ne 1 ]; then
    if [ -t 0 ]; then
      printf 'Overwrite server config/tokens? [y/N] '
      answer=""
      read -r answer || answer=""
      case "$answer" in
        y|Y|yes|YES) ;;
        *) die "aborted" ;;
      esac
    else
      die "refusing to overwrite without --yes"
    fi
  fi
  if [ "$DRY_RUN" -eq 0 ]; then
    info "stopping the existing container before replacing state"
    if ssh "${SSH_OPTS[@]}" "$SERVER" \
      "cd ~/$TESLA_DIR 2>/dev/null && docker compose stop"; then
      SERVER_STOPPED=1
    else
      warn "could not stop the existing container; continuing"
    fi
    info "backing up the existing server config"
    if ! ssh "${SSH_OPTS[@]}" "$SERVER" \
      "cd ~/$TESLA_DIR && TESLA_DIR=\$HOME/$TESLA_DIR bash ./backup.sh"; then
      warn "could not back up the existing config; continuing"
    fi
  else
    info "dry-run: would stop the existing container and back up its config"
  fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
  info "dry-run: would run 'uv run tesla powerwall status' locally (refreshes tokens first)"
  info "dry-run: would create ~/$TESLA_DIR/config/tesla and copy:"
  info "  $CONFIG_FILE -> $SERVER:~/$TESLA_DIR/config/tesla/config.toml"
  info "  $TOKENS_FILE -> $SERVER:~/$TESLA_DIR/config/tesla/tokens.json"
  if [ "$HAVE_KEY" -eq 1 ]; then
    info "  $KEY_PATH -> $SERVER:~/$TESLA_DIR/config/tesla/private_key.pem"
  fi
  info "  minimal app .env -> $SERVER:~/$TESLA_DIR/config/tesla/.env"
  info "dry-run: would run: TESLA_SERVER=$SERVER TESLA_DIR=$TESLA_DIR scripts/deploy.sh"
  say "Dry run complete; nothing changed"
  exit 0
fi

say "Checking Powerwall API locally"
if ! (cd "$REPO" && uv run --quiet tesla powerwall status); then
  die "local powerwall status failed; fix auth/config before deploying"
fi

say "Copying state to $SERVER"
if ! ssh "${SSH_OPTS[@]}" "$SERVER" \
  "mkdir -p ~/$TESLA_DIR/config/tesla ~/$TESLA_DIR/images ~/$TESLA_DIR/backups && chmod 700 ~/$TESLA_DIR/config ~/$TESLA_DIR/config/tesla"; then
  die "could not create ~/$TESLA_DIR/config/tesla on $SERVER"
fi

if ! scp "${SCP_OPTS[@]}" "$CONFIG_FILE" "$TOKENS_FILE" "$SERVER:$TESLA_DIR/config/tesla/"; then
  die "could not copy config.toml/tokens.json to $SERVER"
fi
if [ "$HAVE_KEY" -eq 1 ]; then
  if [ "$KEY_PATH" != "$CONFIG_DIR/private_key.pem" ]; then
    warn "config.toml uses vehicle.key_path=$KEY_PATH; it is copied as private_key.pem on the server"
  fi
  if ! scp "${SCP_OPTS[@]}" "$KEY_PATH" "$SERVER:$TESLA_DIR/config/tesla/private_key.pem"; then
    die "could not copy the private key to $SERVER"
  fi
fi
if ! scp "${SCP_OPTS[@]}" "$TMP_ENV" "$SERVER:$TESLA_DIR/config/tesla/.env"; then
  die "could not copy the app .env to $SERVER"
fi

remote_files="~/$TESLA_DIR/config/tesla/config.toml ~/$TESLA_DIR/config/tesla/tokens.json ~/$TESLA_DIR/config/tesla/.env"
if [ "$HAVE_KEY" -eq 1 ]; then
  remote_files="$remote_files ~/$TESLA_DIR/config/tesla/private_key.pem"
fi
if ! ssh "${SSH_OPTS[@]}" "$SERVER" \
  "chown -R \$(id -u):\$(id -g) ~/$TESLA_DIR/config && chmod 700 ~/$TESLA_DIR/config ~/$TESLA_DIR/config/tesla && chmod 600 $remote_files"; then
  die "could not fix ownership/permissions on $SERVER"
fi

say "Running deploy.sh"
if ! TESLA_SERVER="$SERVER" TESLA_DIR="$TESLA_DIR" "$REPO/scripts/deploy.sh"; then
  die "deploy.sh failed"
fi
SERVER_STOPPED=0

say "Waiting for the watcher"
last_logs=""
started=0
for ((i = 0; i < 12; i++)); do
  last_logs="$(ssh "${SSH_OPTS[@]}" "$SERVER" \
    "started=\$(docker inspect -f '{{.State.StartedAt}}' tesla-app 2>/dev/null || true); if [ -n \"\$started\" ]; then docker logs --since \"\$started\" --tail 50 tesla-app 2>&1; else docker logs --tail 50 tesla-app 2>&1; fi" \
    2>/dev/null || true)"
  if printf '%s\n' "$last_logs" | grep -qiE 'error:|Traceback'; then
    printf '%s\n' "$last_logs" | tail -n 30 >&2
    die "the watcher reported an error; see logs above"
  fi
  if printf '%s\n' "$last_logs" | grep -q "Powerwall"; then
    started=1
    break
  fi
  sleep 5
done

if [ "$started" -ne 1 ]; then
  printf '%s\n' "$last_logs" | tail -n 30 >&2
  die "no Powerwall log line after 60s; check: ssh $SERVER 'docker logs -f tesla-app'"
fi

printf '%s\n' "$last_logs" | tail -n 20

if [ "$TEST_NOTIFY" -eq 1 ]; then
  say "Sending a remote ntfy test"
  if ! ssh "${SSH_OPTS[@]}" "$SERVER" \
    "cd ~/$TESLA_DIR && docker compose exec -T tesla tesla notify test"; then
    die "remote notify test failed; check TESLA_NTFY_TOPIC/URL in ~/$TESLA_DIR/config/tesla/.env"
  fi
fi

say "Deployment complete"
cat <<EOF
Server:   $SERVER
State:    ~/$TESLA_DIR/config/tesla
Logs:     ssh $SERVER 'docker logs -f tesla-app'
Update:   TESLA_SERVER=$SERVER TESLA_DIR=$TESLA_DIR scripts/deploy.sh
Backups:  add '0 4 * * 0 \$HOME/$TESLA_DIR/backup.sh' to the server crontab

Notes:
- powerwall notify sends no startup message; the first push is at the configured level.
- Stop the CLI locally while the server watcher is running (shared refresh token).
- Re-run this script only for a fresh server; use deploy.sh for updates.
EOF
