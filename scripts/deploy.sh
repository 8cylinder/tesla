#!/usr/bin/env bash
# Build the private tesla-app image here, ship it to the server as a gzipped
# tar, load it there, and (re)start the compose service.
#
# Requirements: docker, ssh, scp, gzip, uv, git.
#
# Usage:
#   TESLA_SERVER=mini scripts/deploy.sh
#
# Env:
#   TESLA_SERVER  ssh host of the server (required)
#   TESLA_DIR     deployment directory under the server home (default: tesla)
set -euo pipefail

TESLA_SERVER="${TESLA_SERVER:?set TESLA_SERVER to the ssh host, e.g. TESLA_SERVER=mini}"
TESLA_DIR="${TESLA_DIR:-tesla}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VERSION="$(cd "$REPO" && uv version --short)"
SHA="$(git -C "$REPO" rev-parse --short HEAD)"
TAG="$VERSION-$SHA"
IMAGE="tesla-app:$TAG"

cd "$REPO"

echo "==> building $IMAGE"
docker build -t "$IMAGE" .

mkdir -p dist
TAR="dist/tesla-app-$TAG.tar.gz"
echo "==> saving $TAR"
docker save "$IMAGE" | gzip > "$TAR"

echo "==> uploading to $TESLA_SERVER:~/$TESLA_DIR"
ssh "$TESLA_SERVER" "mkdir -p ~/$TESLA_DIR/images ~/$TESLA_DIR/config/tesla ~/$TESLA_DIR/backups && chmod 700 ~/$TESLA_DIR/config ~/$TESLA_DIR/config/tesla"
scp "$REPO/deploy/compose.yaml" "$TESLA_SERVER:$TESLA_DIR/compose.yaml"
scp "$REPO/deploy/backup.sh" "$TESLA_SERVER:$TESLA_DIR/backup.sh"
scp "$TAR" "$TESLA_SERVER:$TESLA_DIR/images/"

OLD_TAG="$(ssh "$TESLA_SERVER" "sed -n 's/^TESLA_TAG=//p' ~/$TESLA_DIR/.env 2>/dev/null" || true)"
RUID="$(ssh "$TESLA_SERVER" id -u)"
RGID="$(ssh "$TESLA_SERVER" id -g)"
TZ_VALUE="$(ssh "$TESLA_SERVER" "timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC")"

ssh "$TESLA_SERVER" "cat > ~/$TESLA_DIR/.env" <<EOF
TESLA_TAG=$TAG
TESLA_UID=$RUID
TESLA_GID=$RGID
TZ=$TZ_VALUE
EOF

echo "==> loading and starting $IMAGE"
ssh "$TESLA_SERVER" "chmod +x ~/$TESLA_DIR/backup.sh && cd ~/$TESLA_DIR && docker load < images/tesla-app-$TAG.tar.gz && docker compose up -d"

# Drop superseded images and tars, keeping the previous tag for rollback.
if [ -n "$OLD_TAG" ] && [ "$OLD_TAG" != "$TAG" ]; then
  echo "==> pruning images/tars older than $OLD_TAG"
  ssh "$TESLA_SERVER" "
    for t in \$(docker images tesla-app --format '{{.Tag}}' | grep -vxE '(${TAG}|${OLD_TAG})'); do
      docker image rm \"tesla-app:\$t\" || true
    done
    for f in ~/$TESLA_DIR/images/tesla-app-*.tar.gz; do
      case \"\$f\" in
        *${TAG}*|*${OLD_TAG}*) ;;
        *) rm -f \"\$f\" ;;
      esac
    done
  "
fi

sleep 2
ssh "$TESLA_SERVER" "docker inspect -f 'container status: {{.State.Status}}' tesla-app" || true

echo
echo "deployed tesla-app:$TAG"
echo "logs:     ssh $TESLA_SERVER 'docker logs -f tesla-app'"
echo "doctor:   ssh $TESLA_SERVER 'cd ~/$TESLA_DIR && docker compose exec tesla tesla doctor'"
if [ -n "$OLD_TAG" ]; then
  echo "rollback: ssh $TESLA_SERVER \"sed -i 's/^TESLA_TAG=.*/TESLA_TAG=$OLD_TAG/' ~/$TESLA_DIR/.env && cd ~/$TESLA_DIR && docker compose up -d\""
fi
