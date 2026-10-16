#!/bin/sh
# Run any insider-tracker command with the settings in .env, exactly as the nightly job does:
#
#   sudo -u insider sh deploy/run.sh alerts --dry-run
#
# Invoked through `sh` everywhere so it works without the executable bit (which a copy from
# Windows loses).
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then
    set -a
    eval "$(tr -d '\r' < .env)"   # same as sourcing it, minus Windows line endings
    set +a
fi
exec .venv/bin/python -m insider_tracker "$@"
