#!/usr/bin/env bash
# AI Power Grid fresh-host bootstrap (Ubuntu 24.04).
#
# Required:
#   GRID_CORE_COMMIT=<reviewed full SHA> sudo -E bash deploy/bootstrap.sh
#
# This installs only the Grid-native FastAPI coordinator. The retired Flask
# fleet and Horde polling API are deliberately not installed.

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "run as root" >&2
    exit 1
fi

REPO_URL="${REPO_URL:-https://github.com/AIPowerGrid/grid-core.git}"
COMMIT="${GRID_CORE_COMMIT:-}"
ENV_FILE=/etc/aipg/grid.env

if [[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "GRID_CORE_COMMIT must be a reviewed full 40-character commit SHA" >&2
    exit 1
fi

RELEASE="/home/aipg/releases/grid-core-${COMMIT:0:12}"

echo "── [1/6] packages ──"
apt-get update -qq
apt-get install -y -qq \
    curl git python3 python3-venv python3-dev build-essential \
    postgresql postgresql-contrib redis-server nginx \
    libpq-dev openssl

echo "── [2/6] service account + immutable release ──"
id aipg &>/dev/null || useradd -m -s /bin/bash aipg
install -d -o aipg -g aipg /home/aipg/releases
if [[ ! -d "$RELEASE/.git" ]]; then
    sudo -H -u aipg git clone "$REPO_URL" "$RELEASE"
fi
sudo -H -u aipg git -C "$RELEASE" fetch --quiet origin "$COMMIT"
sudo -H -u aipg git -C "$RELEASE" checkout --detach "$COMMIT"
test "$(sudo -H -u aipg git -C "$RELEASE" rev-parse HEAD)" = "$COMMIT"
test -z "$(sudo -H -u aipg git -C "$RELEASE" status --porcelain)"

sudo -H -u aipg python3 -m venv "$RELEASE/.venv"
sudo -H -u aipg "$RELEASE/.venv/bin/pip" install --quiet --upgrade pip
sudo -H -u aipg "$RELEASE/.venv/bin/pip" install --quiet -r "$RELEASE/requirements-grid.txt"
sudo -H -u aipg "$RELEASE/.venv/bin/pip" check

echo "── [3/6] PostgreSQL ──"
DB_PASS="$(openssl rand -hex 24)"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='aipg'" | grep -q 1; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE ROLE aipg LOGIN PASSWORD '$DB_PASS';"
elif [[ ! -f "$ENV_FILE" ]]; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER ROLE aipg PASSWORD '$DB_PASS';"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='aipg_grid'" | grep -q 1; then
    sudo -u postgres createdb -O aipg aipg_grid
fi

echo "── [4/6] environment + migrations ──"
install -d -m 0750 -o root -g aipg /etc/aipg
if [[ ! -f "$ENV_FILE" ]]; then
    GRID_SALT="$(openssl rand -hex 32)"
    USER_TOKEN_KEY="$(openssl rand -hex 32)"
    sed \
        -e "s|^GRID_SALT=.*|GRID_SALT=$GRID_SALT|" \
        -e "s|^POSTGRES_PASS=.*|POSTGRES_PASS=$DB_PASS|" \
        -e "s|^GRID_USER_TOKEN_SIGNING_KEY=.*|GRID_USER_TOKEN_SIGNING_KEY=$USER_TOKEN_KEY|" \
        "$RELEASE/deploy/env.template" > "$ENV_FILE"
    chown root:aipg "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
else
    echo "$ENV_FILE already exists; secrets left untouched"
fi

sudo -H -u aipg bash -c \
    "set -a; source '$ENV_FILE'; set +a; cd '$RELEASE'; .venv/bin/alembic upgrade head"

echo "── [5/6] service + nginx ──"
install -m 0644 "$RELEASE/deploy/systemd/aipg-gridapi.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deploy/nginx/aipg-api.conf" /etc/nginx/sites-available/aipg-api.conf
ln -sfn /etc/nginx/sites-available/aipg-api.conf /etc/nginx/sites-enabled/aipg-api.conf
ln -sfn "$RELEASE" /home/aipg/.current.next
mv -Tf /home/aipg/.current.next /home/aipg/current

systemctl daemon-reload
systemctl enable --now redis-server postgresql aipg-gridapi
nginx -t
systemctl reload nginx

echo "── [6/6] verification ──"
curl --fail --silent --show-error http://127.0.0.1:7010/health >/dev/null
curl --fail --silent --show-error http://127.0.0.1:7010/v1/models >/dev/null
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
    -H 'Host: api.aipowergrid.io' http://127.0.0.1/api/v2/status/models)" = "410"

echo "Grid-native bootstrap complete at $COMMIT"
