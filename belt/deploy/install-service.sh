#!/bin/sh
# Make the Raspberry Pi start the Nudge runtime by itself at power-up.
#
#   sh deploy/install-service.sh            install and start (asks for the sudo password)
#   sh deploy/install-service.sh --print    only show the unit that would be installed
#
# The Python used is, in order: $NUDGE_PYTHON, the checkout's .venv, the
# virtualenv that is active when this is run, then python3. So with the
# packages in ~/nudge:
#
#   source ~/nudge/bin/activate && sh deploy/install-service.sh
#
# NUDGE_ARGS adds options to the runtime, for example NUDGE_ARGS=--go to
# calibrate and start with no operator (see README).
set -eu

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_AS="${SUDO_USER:-$(id -un)}"
ARGS="${NUDGE_ARGS:-}"

PY="${NUDGE_PYTHON:-}"
if [ -z "$PY" ] && [ -x "$DIR/.venv/bin/python" ]; then PY="$DIR/.venv/bin/python"; fi
if [ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then PY="$VIRTUAL_ENV/bin/python"; fi
if [ -z "$PY" ]; then PY="$(command -v python3 || true)"; fi
if [ -z "$PY" ]; then
    echo "No Python found. Set NUDGE_PYTHON to the interpreter to use." >&2
    exit 1
fi

if ! (cd "$DIR" && "$PY" -c "import nudge.main, aiohttp" 2>/dev/null); then
    echo "$PY cannot run the runtime: it needs aiohttp, and to be run from $DIR." >&2
    echo "Activate the virtualenv that has aiohttp, or set NUDGE_PYTHON, and run this again." >&2
    exit 1
fi

UNIT="$(sed -e "s|__USER__|$RUN_AS|g" -e "s|__DIR__|$DIR|g" -e "s|__PYTHON__|$PY|g" -e "s|__ARGS__|$ARGS|g" \
    "$DIR/deploy/nudge.service")"

if [ "${1:-}" = "--print" ]; then
    printf '%s\n' "$UNIT"
    exit 0
fi

printf '%s\n' "$UNIT" | sudo tee /etc/systemd/system/nudge.service >/dev/null
# A copy started by hand holds the ports and the servo board: stop it first.
pkill -f "nudge.main --tls" 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable nudge.service
sudo systemctl restart nudge.service
sleep 3
systemctl --no-pager --lines=12 status nudge.service || true
echo
echo "The runtime now starts at power-up. Follow its log with:  journalctl -u nudge -f"
echo "Remove with:  sudo systemctl disable --now nudge && sudo rm /etc/systemd/system/nudge.service"
