#!/bin/bash
# Redeploy the latest commit on the running instance.  Usage (over SSH):  sudo bash /opt/zeno/deploy/update.sh
#
# WHY this exists as a script: a redeploy under interview pressure should be one command, not three
# remembered ones. In a real setup this is what a GitHub Actions job would run.
set -euo pipefail
cd /opt/zeno
git pull --ff-only
# --build rebuilds the image; the pgdata volume is untouched, so bookings survive the redeploy.
docker compose up -d --build
docker compose ps
