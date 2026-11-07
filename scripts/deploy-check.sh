#!/usr/bin/env bash
# Pre-flight checks for deploying tesla-app from this build machine to the
# home server. It only gathers information and prints a report.
#
# It makes no changes: it does not build, copy, chmod, write config, or
# start/stop/restart anything on either machine. The summary recommends
# deploy-initial.sh for a fresh server or deploy.sh for an existing one.
#
# Usage:
#   scripts/deploy-check.sh [user@host]
#   scripts/deploy-check.sh --local-only
#   scripts/deploy-check.sh --remote-only
#
# Env:
#   TESLA_SERVER       destination ssh host (default: sm@beelink.local)
#   TESLA_CONFIG_DIR   app state directory (default: ~/.config/tesla)
#   TESLA_NTFY_URL     override ntfy URL for the reachability check
#   TESLA_FLEET_URL    override Fleet API URL for the reachability check
#
# Exit status: 0 when there are no FAIL items (warnings are allowed).

set -uo pipefail

# Inspection commands must not take optional locks or populate caches.
export GIT_OPTIONAL_LOCKS=0
export UV_NO_CACHE=1

DEFAULT_SERVER="sm@beelink.local"
SERVER="${TESLA_SERVER:-$DEFAULT_SERVER}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${TESLA_CONFIG_DIR:-$HOME/.config/tesla}"
CONFIG_FILE="$CONFIG_DIR/config.toml"

DO_LOCAL=1
DO_REMOTE=1
REMOTE_CHECKED=0
BOOTSTRAP_STATE="unknown"

usage() {
  cat <<'EOF'
Pre-flight checks for deploying tesla-app from this build machine to the
home server. It only gathers information and prints a report.

Usage:
  scripts/deploy-check.sh [user@host]
  scripts/deploy-check.sh --local-only
  scripts/deploy-check.sh --remote-only

Env:
  TESLA_SERVER       destination ssh host (default: sm@beelink.local)
  TESLA_CONFIG_DIR   app state directory (default: ~/.config/tesla)
  TESLA_NTFY_URL     override ntfy URL for the reachability check
  TESLA_FLEET_URL    override Fleet API URL for the reachability check
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --local-only)
      DO_REMOTE=0
      ;;
    --remote-only)
      DO_LOCAL=0
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
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      SERVER="$1"
      ;;
  esac
  shift
done

# StrictHostKeyChecking=yes also prevents ssh from writing known_hosts.
SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=yes
  -o UpdateHostKeys=no
  -o ControlMaster=no
  -o ControlPath=none
)

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

passes=0
warns=0
fails=0

pass() {
  printf '%s[PASS]%s %s\n' "$GREEN" "$RESET" "$*"
  passes=$((passes + 1))
}

warn() {
  printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$*"
  warns=$((warns + 1))
}

fail() {
  printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$*"
  fails=$((fails + 1))
}

info() {
  printf '       %s\n' "$*"
}

section() {
  printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RESET"
}

require_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "$cmd installed: $(command -v "$cmd")"
  else
    fail "$cmd is not installed"
  fi
}

file_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1" 2>/dev/null || true
  fi
}

check_state_file() {
  local path="$1" label="$2" severity="$3"
  if [ ! -e "$path" ]; then
    if [ "$severity" = fail ]; then
      fail "$label missing: $path"
    else
      warn "$label missing: $path"
    fi
    return
  fi
  pass "$label present: $path"

  local mode num
  mode="$(file_mode "$path")"
  if [ -z "$mode" ]; then
    warn "$label mode could not be read: $path"
    return
  fi
  num=$((8#$mode))
  if [ $((num & 077)) -eq 0 ]; then
    pass "$label mode $mode"
  else
    if [ "$severity" = fail ]; then
      fail "$label is group/other-accessible (mode $mode); run: chmod 600 $path"
    else
      warn "$label is group/other-accessible (mode $mode); run: chmod 600 $path"
    fi
  fi
}

norm_arch() {
  case "${1:-}" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    armv7l|armhf) echo armv7 ;;
    i386|i686) echo 386 ;;
    *) echo "${1:-unknown}" ;;
  esac
}

human_kb() {
  awk -v k="${1:-0}" 'BEGIN { printf "%.1f GiB", k / 1048576 }'
}

http_status() {
  local url="$1"
  if ! command -v curl >/dev/null 2>&1; then
    echo "no-curl"
    return
  fi
  curl -q -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null || echo "err"
}

check_http() {
  local label="$1" url="$2" required="$3" code
  code="$(http_status "$url")"
  case "$code" in
    2*|3*|401|403|404|405)
      pass "$label reachable (HTTP $code)"
      ;;
    no-curl)
      warn "$label not checked (curl not installed)"
      ;;
    *)
      if [ "$required" = required ]; then
        fail "$label unreachable: $url (HTTP ${code:-none})"
      else
        warn "$label unreachable: $url (HTTP ${code:-none})"
      fi
      ;;
  esac
}

check_remote_http() {
  local label="$1" code="$2" url="$3"
  case "$code" in
    2*|3*|401|403|404|405)
      pass "$label reachable from server (HTTP $code)"
      ;;
    no-curl)
      warn "$label not checked (curl missing on server)"
      ;;
    ''|err|000)
      fail "$label unreachable from server: $url"
      ;;
    *)
      fail "$label returned HTTP $code from server: $url"
      ;;
  esac
}

# --- local app state / env helpers -----------------------------------------

ENV_FILE=""
for candidate in "$PWD/.env" "$REPO/.env" "$CONFIG_DIR/.env"; do
  if [ -f "$candidate" ]; then
    ENV_FILE="$candidate"
    break
  fi
done

env_get() {
  local key="$1"
  [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || return 1
  local line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n1)"
  [ -n "$line" ] || return 1
  line="${line#*=}"
  line="${line%$'\r'}"
  line="${line#\"}"
  line="${line%\"}"
  line="${line#\'}"
  line="${line%\'}"
  printf '%s\n' "$line"
}

# --- config.toml parsing ----------------------------------------------------

# Use an already-installed Python only; never let uv download or install one.
PYTHON=()
if command -v python3 >/dev/null 2>&1 && python3 -c 'import tomllib' >/dev/null 2>&1; then
  PYTHON=(python3)
fi

CFG_RAW=""
CFG_OK=0
CFG_REGION="na"
CFG_DOMAIN=""
CFG_SITE_ID=""
CFG_NOTIFY_ENABLED="false"
CFG_NOTIFY_URL="https://ntfy.sh"
CFG_NOTIFY_PRIORITY="3"
CFG_NOTIFY_MIN_PRIORITY="1"
CFG_KEY_PATH=""

cfg_get() {
  printf '%s\n' "${CFG_RAW:-}" | grep -m1 "^$1=" | cut -d= -f2-
}

load_config_values() {
  [ -f "$CONFIG_FILE" ] || return
  [ "${#PYTHON[@]}" -gt 0 ] || return
  CFG_RAW="$("${PYTHON[@]}" - "$CONFIG_FILE" 2>/dev/null <<'PY'
import sys

try:
    import tomllib
except Exception:
    print("parse_error=tomllib unavailable")
    raise SystemExit(0)

try:
    with open(sys.argv[1], "rb") as handle:
        cfg = tomllib.load(handle)
except Exception as exc:
    print(f"parse_error={exc}")
    raise SystemExit(0)

def table(name):
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}

def emit(key, value):
    text = "" if value is None else str(value)
    print(f"{key}={text}")

powerwall = table("powerwall")
notify = table("notify")
vehicle = table("vehicle")

emit("region", cfg.get("region", "na"))
emit("domain", cfg.get("domain"))
emit("site_id", powerwall.get("energy_site_id"))
emit("notify_enabled", "true" if notify.get("enabled") is True else "false")
emit("notify_url", notify.get("url", "https://ntfy.sh"))
emit("notify_priority", notify.get("priority", 3))
emit("notify_min_priority", notify.get("min_priority", 1))
emit("key_path", vehicle.get("key_path"))
PY
)"

  if printf '%s\n' "$CFG_RAW" | grep -q '^parse_error='; then
    return
  fi

  CFG_OK=1
  CFG_REGION="$(cfg_get region)"
  CFG_DOMAIN="$(cfg_get domain)"
  CFG_SITE_ID="$(cfg_get site_id)"
  CFG_NOTIFY_ENABLED="$(cfg_get notify_enabled)"
  CFG_NOTIFY_URL="$(cfg_get notify_url)"
  CFG_NOTIFY_PRIORITY="$(cfg_get notify_priority)"
  CFG_NOTIFY_MIN_PRIORITY="$(cfg_get notify_min_priority)"
  CFG_KEY_PATH="$(cfg_get key_path)"
}

load_config_values

NTFY_URL="${TESLA_NTFY_URL:-}"
[ -n "$NTFY_URL" ] || NTFY_URL="$CFG_NOTIFY_URL"
[ -n "$NTFY_URL" ] || NTFY_URL="https://ntfy.sh"

FLEET_URL="${TESLA_FLEET_URL:-}"
if [ -z "$FLEET_URL" ]; then
  case "$CFG_REGION" in
    eu) FLEET_URL="https://fleet-api.prd.eu.vn.cloud.tesla.com" ;;
    cn) FLEET_URL="https://fleet-api.prd.cn.vn.cloud.tesla.com" ;;
    *) FLEET_URL="https://fleet-api.prd.na.vn.cloud.tesla.com" ;;
  esac
fi

# --- build machine ----------------------------------------------------------

BUILD_ARCH_RAW="$(uname -m)"

if [ "$DO_LOCAL" -eq 1 ]; then
  section "Build machine ($(hostname 2>/dev/null || echo unknown))"

  for cmd in docker ssh scp gzip uv git; do
    require_cmd "$cmd"
  done

  if [ "${#PYTHON[@]}" -gt 0 ]; then
    pass "Python with tomllib available: ${PYTHON[*]}"
  else
    warn "no Python 3.11+ found; config.toml will not be validated (nothing was downloaded)"
  fi

  if command -v curl >/dev/null 2>&1; then
    pass "curl installed: $(command -v curl)"
  else
    warn "curl is not installed; network reachability checks will be skipped"
  fi

  for file in Dockerfile pyproject.toml uv.lock deploy/compose.yaml deploy/backup.sh scripts/deploy.sh; do
    if [ -f "$REPO/$file" ]; then
      pass "repo file present: $file"
    else
      fail "repo file missing: $file"
    fi
  done

  if [ -x "$REPO/scripts/deploy.sh" ]; then
    pass "scripts/deploy.sh is executable"
  else
    fail "scripts/deploy.sh is not executable; run: chmod +x scripts/deploy.sh"
  fi

  BUILD_ARCH_RAW=""
  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      pass "Docker daemon reachable"
      BUILD_ARCH_RAW="$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
      pass "Docker server version: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
    else
      fail "Docker daemon not reachable; start Docker and retry"
    fi
  fi
  BUILD_ARCH_RAW="${BUILD_ARCH_RAW:-$(uname -m)}"
  pass "build architecture: $(norm_arch "$BUILD_ARCH_RAW") ($BUILD_ARCH_RAW)"

  if command -v uv >/dev/null 2>&1; then
    if (cd "$REPO" && uv version --short >/dev/null 2>&1); then
      pass "uv project version: $(cd "$REPO" && uv version --short 2>/dev/null)"
    else
      fail "uv cannot read the project version; run from the repo root"
    fi
  fi

  if command -v git >/dev/null 2>&1; then
    if git -C "$REPO" rev-parse --short HEAD >/dev/null 2>&1; then
      pass "git HEAD: $(git -C "$REPO" rev-parse --short HEAD)"
      if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
        warn "git working tree has uncommitted changes; they will be included in the image"
      else
        pass "git working tree is clean"
      fi
    else
      fail "$REPO is not a git checkout"
    fi
  fi

  free_kb="$(df -Pk "$REPO" 2>/dev/null | awk 'NR==2{print $4}')"
  case "$free_kb" in
    ''|*[!0-9]*)
      warn "could not determine free disk space for $REPO"
      ;;
    *)
      if [ "$free_kb" -lt 1048576 ]; then
        fail "less than 1 GiB free for $REPO ($(human_kb "$free_kb"))"
      elif [ "$free_kb" -lt 2097152 ]; then
        warn "low disk space for $REPO ($(human_kb "$free_kb"))"
      else
        pass "disk free for $REPO: $(human_kb "$free_kb")"
      fi
      ;;
  esac

  check_state_file "$CONFIG_FILE" "config.toml" warn
  check_state_file "$CONFIG_DIR/tokens.json" "tokens.json" fail

  if [ -n "$CFG_KEY_PATH" ]; then
    KEY_PATH="$CFG_KEY_PATH"
    KEY_PATH="${KEY_PATH/#\~/$HOME}"
  else
    KEY_PATH="$CONFIG_DIR/private_key.pem"
  fi
  check_state_file "$KEY_PATH" "private key" warn

  if [ -f "$CONFIG_FILE" ]; then
    if [ "${#PYTHON[@]}" -eq 0 ]; then
      warn "config.toml exists but cannot be parsed without Python 3.11+"
    elif [ "$CFG_OK" -ne 1 ]; then
      fail "config.toml could not be parsed: $(cfg_get parse_error)"
    else
      pass "config.toml parses as TOML"

      case "$CFG_REGION" in
        na|eu|cn) pass "region=$CFG_REGION" ;;
        *) fail "region must be one of na, eu, cn (got '${CFG_REGION:-unset}')" ;;
      esac

      if [ -n "$CFG_DOMAIN" ]; then
        pass "domain=$CFG_DOMAIN"
      else
        warn "domain is not set; it is required for key registration and vehicle commands"
      fi

      if [ -n "$CFG_SITE_ID" ]; then
        pass "powerwall.energy_site_id=$CFG_SITE_ID"
      else
        fail "powerwall.energy_site_id is not set; run: tesla config set powerwall.energy_site_id <id>"
      fi

      if [ "$CFG_NOTIFY_ENABLED" = true ]; then
        pass "notify.enabled=true"
      else
        fail "notify.enabled is not true; 'powerwall notify' will exit; run: tesla config set notify.enabled true"
      fi

      case "$CFG_NOTIFY_PRIORITY" in
        1|2|3|4|5) pass "notify.priority=$CFG_NOTIFY_PRIORITY" ;;
        *) fail "notify.priority must be 1-5 (got '${CFG_NOTIFY_PRIORITY:-unset}')" ;;
      esac

      case "$CFG_NOTIFY_MIN_PRIORITY" in
        1|2|3|4|5) pass "notify.min_priority=$CFG_NOTIFY_MIN_PRIORITY" ;;
        *) fail "notify.min_priority must be 1-5 (got '${CFG_NOTIFY_MIN_PRIORITY:-unset}')" ;;
      esac

      if [ -n "$CFG_NOTIFY_URL" ]; then
        pass "notify.url=$CFG_NOTIFY_URL"
      else
        fail "notify.url must not be empty"
      fi
    fi
  fi

  if [ -z "$ENV_FILE" ]; then
    fail "no app .env found (looked in $PWD/.env, $REPO/.env and $CONFIG_DIR/.env)"
  else
    check_state_file "$ENV_FILE" "app .env" warn

    if [ "$ENV_FILE" = "$REPO/.env" ] && git -C "$REPO" ls-files --error-unmatch .env >/dev/null 2>&1; then
      fail ".env is tracked by git; remove it from the index and history"
    fi

    for key in TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REDIRECT_URI TESLA_NTFY_TOPIC; do
      value="$(env_get "$key" || true)"
      if [ -z "$value" ]; then
        fail "$key is missing or empty in $ENV_FILE"
      else
        pass "$key is set"
      fi
    done

    client_id="$(env_get TESLA_CLIENT_ID || true)"
    client_secret="$(env_get TESLA_CLIENT_SECRET || true)"
    for pair in "TESLA_CLIENT_ID:$client_id" "TESLA_CLIENT_SECRET:$client_secret"; do
      key="${pair%%:*}"
      value="${pair#*:}"
      case "$value" in
        '' ) ;;
        your-*|*your-client*|*your-secret*|*changeme*|*example*)
          fail "$key still looks like a placeholder"
          ;;
      esac
    done

    topic="$(env_get TESLA_NTFY_TOPIC || true)"
    case "$topic" in
      '') ;;
      *'<unguessable>'*)
        fail "TESLA_NTFY_TOPIC still has the example placeholder"
        ;;
      *)
        if [ "${#topic}" -lt 12 ]; then
          warn "TESLA_NTFY_TOPIC is short; use a hard-to-guess topic (it acts as a password)"
        else
          pass "TESLA_NTFY_TOPIC looks non-trivial"
        fi
        ;;
    esac

    if [ -n "$(env_get TESLA_NTFY_TOKEN || true)" ]; then
      pass "TESLA_NTFY_TOKEN is set (protected/self-hosted ntfy)"
    fi
    if [ -n "$(env_get TESLA_NTFY_URL || true)" ]; then
      pass "TESLA_NTFY_URL is set: $(env_get TESLA_NTFY_URL || true)"
    fi

    if [ -n "$(env_get SSH || true)" ] || [ -n "$(env_get SSH_SUDO || true)" ]; then
      warn "app .env contains SSH/SSH_SUDO; do not copy it verbatim to the server"
      info "create a minimal ~/tesla/config/.env with only TESLA_* and NTFY_* keys"
    fi

    redirect_uri="$(env_get TESLA_REDIRECT_URI || true)"
    if [ -n "$redirect_uri" ] && [ "$redirect_uri" != "http://localhost:8085/callback" ]; then
      warn "TESLA_REDIRECT_URI is $redirect_uri; verify it matches the Tesla developer app"
    fi
  fi

  procs="$(ps ax -o pid=,command= 2>/dev/null | grep -E '[t]esla.*(powerwall notify|auto-charge)' || true)"
  if [ -n "$procs" ]; then
    warn "a local tesla watcher is running; stop it before sharing tokens.json"
    info "$procs"
    info "both processes rotate the same refresh token and will invalidate each other"
  else
    pass "no local tesla watcher is running"
  fi

  check_http "ntfy" "$NTFY_URL" optional
  check_http "Tesla Fleet API" "$FLEET_URL" optional
  check_http "ghcr.io (Docker base image)" "https://ghcr.io/v2/" optional
fi

# --- home server ------------------------------------------------------------

if [ "$DO_REMOTE" -eq 1 ]; then
  section "Home server ($SERVER)"

  remote_ok=0
  if ssh "${SSH_OPTS[@]}" "$SERVER" true >/dev/null 2>&1; then
    pass "SSH key auth to $SERVER works"
    remote_ok=1
  else
    fail "cannot SSH non-interactively to $SERVER"
    info "check that $SERVER resolves, accept the host key once (ssh $SERVER),"
    info "and install your key (ssh-copy-id $SERVER)"
  fi

  if [ "$remote_ok" -eq 1 ]; then
    remote_raw="$(ssh "${SSH_OPTS[@]}" "$SERVER" \
      env NTFY_URL="$NTFY_URL" FLEET_URL="$FLEET_URL" bash -s <<'REMOTE' 2>/dev/null
echo "host=$(hostname 2>/dev/null || echo unknown)"
echo "arch=$(uname -m 2>/dev/null || echo unknown)"
echo "docker_ok=$(docker info >/dev/null 2>&1 && echo yes || echo no)"
echo "docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
echo "docker_arch=$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
echo "compose_ok=$(docker compose version >/dev/null 2>&1 && echo yes || echo no)"
echo "compose_version=$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null | head -1 || true)"
echo "docker_group=$(id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker && echo yes || echo no)"
echo "user=$(id -un 2>/dev/null || echo unknown)"
echo "uid=$(id -u 2>/dev/null || echo unknown)"
echo "gid=$(id -g 2>/dev/null || echo unknown)"
echo "tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)"
echo "disk_free_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
echo "home_writable=$(test -w "$HOME" && echo yes || echo no)"
echo "tesla_dir=$(test -d "$HOME/tesla" && echo yes || echo no)"
echo "tesla_dir_writable=$(test -d "$HOME/tesla" && (test -w "$HOME/tesla" && echo yes || echo no) || echo missing)"
echo "config_dir=$(test -d "$HOME/tesla/config" && echo yes || echo no)"
echo "config_dir_mode=$(stat -c %a "$HOME/tesla/config" 2>/dev/null || echo missing)"
echo "docker_enabled=$(systemctl is-enabled docker 2>/dev/null || echo unknown)"
echo "docker_active=$(systemctl is-active docker 2>/dev/null || echo unknown)"
echo "container_status=$(docker inspect -f '{{.State.Status}}' tesla-app 2>/dev/null || echo absent)"
echo "curl_ok=$(command -v curl >/dev/null 2>&1 && echo yes || echo no)"
if command -v curl >/dev/null 2>&1; then
  echo "ntfy_http=$(curl -q -sS -o /dev/null -w '%{http_code}' --max-time 10 "$NTFY_URL" 2>/dev/null || echo err)"
  echo "fleet_http=$(curl -q -sS -o /dev/null -w '%{http_code}' --max-time 10 "$FLEET_URL" 2>/dev/null || echo err)"
else
  echo "ntfy_http=no-curl"
  echo "fleet_http=no-curl"
fi
echo "tar_ok=$(command -v tar >/dev/null 2>&1 && echo yes || echo no)"
echo "gzip_ok=$(command -v gzip >/dev/null 2>&1 && echo yes || echo no)"
for f in config.toml tokens.json private_key.pem .env; do
  p="$HOME/tesla/config/$f"
  if [ -e "$p" ]; then
    echo "cfg_${f//./_}_mode=$(stat -c %a "$p" 2>/dev/null || echo unknown)"
  else
    echo "cfg_${f//./_}_mode=missing"
  fi
done
REMOTE
)" || true

    rget() {
      printf '%s\n' "$remote_raw" | grep -m1 "^$1=" | cut -d= -f2-
    }

    if [ -z "$remote_raw" ]; then
      fail "remote checks returned no output from $SERVER"
    else
      REMOTE_CHECKED=1
      present=0
      for key in cfg_config_toml_mode cfg_tokens_json_mode cfg__env_mode; do
        value="$(rget "$key")"
        if [ -n "$value" ] && [ "$value" != missing ]; then
          present=$((present + 1))
        fi
      done
      case "$present" in
        0) BOOTSTRAP_STATE="missing" ;;
        3) BOOTSTRAP_STATE="complete" ;;
        *) BOOTSTRAP_STATE="partial" ;;
      esac

      r_host="$(rget host)"
      if [ -n "$r_host" ]; then
        pass "hostname: $r_host"
      else
        warn "could not read the server hostname"
      fi

      r_user="$(rget user)"
      r_uid="$(rget uid)"
      r_gid="$(rget gid)"
      pass "ssh user: ${r_user:-unknown} (uid ${r_uid:-?}, gid ${r_gid:-?})"

      if [ "$(rget docker_ok)" = yes ]; then
        pass "Docker daemon reachable (server $(rget docker_version))"
      else
        fail "Docker daemon not reachable as ${r_user:-the ssh user}"
        if [ "$(rget docker_group)" != yes ]; then
          info "add the user to the docker group: sudo usermod -aG docker ${r_user:-<user>}"
          info "then log out and back in (or run: newgrp docker)"
        fi
      fi

      if [ "$(rget compose_ok)" = yes ]; then
        pass "docker compose v2: $(rget compose_version)"
      else
        fail "docker compose v2 is not available; install the Compose plugin"
      fi

      server_arch_raw="$(rget docker_arch)"
      [ -n "$server_arch_raw" ] || server_arch_raw="$(rget arch)"
      build_arch="$(norm_arch "$BUILD_ARCH_RAW")"
      server_arch="$(norm_arch "$server_arch_raw")"
      if [ "$server_arch" = unknown ]; then
        warn "could not determine the server architecture"
      elif [ "$build_arch" = "$server_arch" ]; then
        pass "architectures match: $build_arch"
      else
        fail "architecture mismatch: build $build_arch, server $server_arch"
        info "the shipped image tar is architecture-specific; build on the server or use docker buildx"
      fi

      r_free_kb="$(rget disk_free_kb)"
      case "$r_free_kb" in
        ''|*[!0-9]*)
          warn "could not determine free space on the server"
          ;;
        *)
          if [ "$r_free_kb" -lt 1048576 ]; then
            fail "less than 1 GiB free on the server ($(human_kb "$r_free_kb"))"
          elif [ "$r_free_kb" -lt 2097152 ]; then
            warn "low disk space on the server ($(human_kb "$r_free_kb"))"
          else
            pass "disk free on server: $(human_kb "$r_free_kb")"
          fi
          ;;
      esac

      if [ "$(rget home_writable)" = yes ]; then
        pass "home directory is writable"
      else
        fail "home directory is not writable by ${r_user:-the ssh user}"
      fi

      case "$(rget tesla_dir_writable)" in
        yes|missing)
          pass "~/tesla is writable or will be created"
          ;;
        no)
          fail "~/tesla exists but is not writable by ${r_user:-the ssh user}"
          ;;
        *)
          warn "could not determine whether ~/tesla is writable"
          ;;
      esac

      case "$(rget config_dir_mode)" in
        missing)
          pass "~/tesla/config does not exist yet (created by the first deploy)"
          ;;
        700)
          pass "~/tesla/config mode 700"
          ;;
        *)
          warn "~/tesla/config mode is $(rget config_dir_mode); expected 700"
          ;;
      esac

      r_tz="$(rget tz)"
      if [ -n "$r_tz" ]; then
        pass "timezone: $r_tz"
      else
        warn "timezone not detected; deploy.sh will fall back to UTC"
      fi

      case "$(rget docker_enabled)" in
        enabled|enabled-runtime)
          pass "Docker is enabled at boot"
          ;;
        disabled)
          warn "Docker is not enabled at boot; run: sudo systemctl enable docker"
          ;;
        *)
          warn "could not determine whether Docker starts at boot"
          ;;
      esac

      if [ "$(rget docker_active)" = active ]; then
        pass "Docker service is active"
      else
        warn "Docker service is $(rget docker_active)"
      fi

      case "$(rget container_status)" in
        absent)
          pass "no existing tesla-app container"
          ;;
        running)
          pass "existing tesla-app container is running (update path)"
          ;;
        *)
          warn "existing tesla-app container state: $(rget container_status)"
          ;;
      esac

      check_remote_mode() {
        local key="$1" label="$2" severity="$3" mode num
        mode="$(rget "$key")"
        case "$mode" in
          missing)
            pass "$label not present yet (first deploy)"
            ;;
          ''|unknown)
            warn "$label mode could not be read"
            ;;
          *)
            num=$((8#$mode))
            if [ $((num & 077)) -eq 0 ]; then
              pass "$label mode $mode"
            elif [ "$severity" = fail ]; then
              fail "$label is group/other-accessible (mode $mode); run: chmod 600 ~/tesla/config/$label"
            else
              warn "$label is group/other-accessible (mode $mode); run: chmod 600 ~/tesla/config/$label"
            fi
            ;;
        esac
      }

      check_remote_mode cfg_config_toml_mode "config.toml" warn
      check_remote_mode cfg_tokens_json_mode "tokens.json" fail
      check_remote_mode cfg_private_key_pem_mode "private_key.pem" fail
      check_remote_mode cfg__env_mode ".env" warn

      if [ "$(rget tar_ok)" = yes ]; then
        pass "tar installed (backups)"
      else
        warn "tar is missing; deploy/backup.sh will not work"
      fi
      if [ "$(rget gzip_ok)" = yes ]; then
        pass "gzip installed (backups)"
      else
        warn "gzip is missing; deploy/backup.sh will not work"
      fi

      if [ "$(rget curl_ok)" = yes ]; then
        pass "curl installed (network checks)"
      else
        warn "curl is missing on the server; network reachability was not checked"
      fi

      check_remote_http "ntfy" "$(rget ntfy_http)" "$NTFY_URL"
      check_remote_http "Tesla Fleet API" "$(rget fleet_http)" "$FLEET_URL"
    fi
  fi
fi

# --- summary ----------------------------------------------------------------

section "Summary"
printf 'passed: %d   warnings: %d   failures: %d\n' "$passes" "$warns" "$fails"

if [ "$DO_REMOTE" -eq 0 ]; then
  printf '\nServer state: not checked (--local-only).\n'
elif [ "$REMOTE_CHECKED" -ne 1 ]; then
  printf '\nServer state: unknown (remote checks failed).\n'
else
  case "$BOOTSTRAP_STATE" in
    missing)
      printf '\nServer state: not bootstrapped (no config.toml, tokens.json, or app .env).\n'
      ;;
    partial)
      printf '\nServer state: partially bootstrapped (one or more of config.toml, tokens.json, app .env is missing).\n'
      ;;
    complete)
      printf '\nServer state: already bootstrapped (config.toml, tokens.json, and app .env are present).\n'
      ;;
  esac
fi

if [ "$fails" -gt 0 ]; then
  printf '\n%sNot ready to deploy.%s Fix the FAIL items above and re-run.\n' "$RED" "$RESET"
  if [ "$DO_REMOTE" -eq 1 ] && [ "$REMOTE_CHECKED" -eq 1 ]; then
    case "$BOOTSTRAP_STATE" in
      missing)
        printf 'Then run: TESLA_SERVER=%s scripts/deploy-initial.sh\n' "$SERVER"
        ;;
      partial)
        printf 'Then run: TESLA_SERVER=%s scripts/deploy-initial.sh --force\n' "$SERVER"
        ;;
      complete)
        printf 'Then run: TESLA_SERVER=%s scripts/deploy.sh\n' "$SERVER"
        ;;
    esac
  fi
  exit 1
fi

if [ "$DO_LOCAL" -eq 0 ]; then
  printf '\n%sServer checks passed%s (local build-machine checks were skipped).\n' "$GREEN" "$RESET"
elif [ "$DO_REMOTE" -eq 0 ]; then
  printf '\n%sLocal checks passed%s (remote checks were skipped).\n' "$GREEN" "$RESET"
else
  printf '\n%sReady to deploy.%s\n' "$GREEN" "$RESET"
fi

if [ "$DO_REMOTE" -eq 0 ]; then
  printf '\nServer bootstrap state was not checked. Re-run without --local-only to decide between deploy-initial.sh and deploy.sh.\n'
elif [ "$REMOTE_CHECKED" -ne 1 ]; then
  printf '\nCannot determine server bootstrap state because remote checks failed. Fix the server/SSH FAILs above and re-run.\n'
else
  case "$BOOTSTRAP_STATE" in
    missing)
      printf '\nThis looks like a first deploy. Run:\n  TESLA_SERVER=%s scripts/deploy-initial.sh\n' "$SERVER"
      ;;
    partial)
      printf '\nServer state is incomplete. Run:\n  TESLA_SERVER=%s scripts/deploy-initial.sh --force\n' "$SERVER"
      printf '(--force stops the service, backs up config/, then replaces state.)\n'
      ;;
    complete)
      printf '\nServer is already bootstrapped. Use deploy.sh for updates:\n  TESLA_SERVER=%s scripts/deploy.sh\n' "$SERVER"
      ;;
  esac
fi
exit 0
