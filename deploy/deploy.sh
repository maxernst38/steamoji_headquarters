#!/usr/bin/env bash
# Pull the latest main and restart the site. Run it when you decide the live
# site should change - not on every merge, because a bad merge during a
# competition is the worst possible time to find out.
set -euo pipefail

cd /srv/vex-tracker
echo "==> fetching"
git fetch --quiet origin
git checkout --quiet main
git pull --quiet --ff-only origin main

echo "==> dependencies"
.venv/bin/pip install --quiet --upgrade -r requirements-web.txt

echo "==> restarting"
sudo systemctl restart vex-tracker
sleep 2
systemctl is-active --quiet vex-tracker && echo "==> live: $(git rev-parse --short HEAD)"
