#!/usr/bin/env bash
# Runs the terminal test mode on the Pi. Only one program can use the GPIO pins
# at a time, so this stops the service first and starts it again when you quit.
#
# Usage: sudo /opt/cupboard-door/test-mode.sh
set -euo pipefail

APP_DIR=/opt/cupboard-door
CONFIG=/var/lib/cupboard-door/door_config.json
SERVICE=cupboard-door
SERVICE_USER=cupboard-door

if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

WAS_RUNNING=0
if systemctl is-active --quiet "$SERVICE"; then
  echo "Stopping the $SERVICE service while test mode runs"
  systemctl stop "$SERVICE"
  WAS_RUNNING=1
fi

WORK=$(mktemp -d)
chown "$SERVICE_USER" "$WORK"

finish() {
  rm -rf "$WORK"
  if [[ $WAS_RUNNING == 1 ]]; then
    echo "Starting the $SERVICE service again"
    systemctl start "$SERVICE"
  fi
}
trap finish EXIT

# Run as the service user so the settings file keeps the right owner.
sudo -u "$SERVICE_USER" env LG_WD="$WORK" PYTHONPATH="$APP_DIR" \
  sh -c 'cd "$1" && shift && exec "$@"' sh "$WORK" \
  "$APP_DIR/venv/bin/python" -m cupboard_door --test --config "$CONFIG" "$@"
