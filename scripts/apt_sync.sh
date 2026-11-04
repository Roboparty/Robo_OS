#!/bin/bash
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 wentywenty
#
# Pull the latest GitHub Releases and inject them into the local APT repo.
# Intended to run from roboparty-apt-sync.service (User=ubuntu).

set -euo pipefail

REPO_DIR=${REPO_DIR:-/home/robo/roboparty_repo}
INCOMING_DIR=${INCOMING_DIR:-/srv/apt-incoming}
LOCK_FILE=${APT_SYNC_LOCK:-/run/lock/roboparty-apt-sync.lock}

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=${HOME:-/home/ubuntu}

cd "$REPO_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "roboparty-apt-sync: another sync is already running"
    exit 0
fi

echo "roboparty-apt-sync: start $(date -Is)"
python3 scripts/route_debs.py --strict routing.yaml "$INCOMING_DIR"
bash scripts/bot_inject.sh
python3 scripts/verify_apt.py "$INCOMING_DIR/manifest.json"
echo "roboparty-apt-sync: done $(date -Is)"
