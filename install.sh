#!/usr/bin/env bash
# Installs the cupboard door controller as a service that starts on every boot.
# Safe to run more than once: it updates the code and keeps your settings.
#
# Usage: sudo ./install.sh
set -euo pipefail

APP_DIR=/opt/cupboard-door
STATE_DIR=/var/lib/cupboard-door
SERVICE_USER=cupboard-door
SERVICE=cupboard-door
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo ./install.sh"
  exit 1
fi

step() { echo; echo "==> $*"; }

step "Installing system packages"
apt-get update
apt-get install -y python3 python3-venv python3-pip python3-lgpio python3-gpiozero

step "Turning on the 1-Wire interface (DS18B20 on GPIO4)"
CONFIG_TXT=/boot/firmware/config.txt
[[ -f $CONFIG_TXT ]] || CONFIG_TXT=/boot/config.txt
REBOOT_NEEDED=0
if grep -qE '^[[:space:]]*dtoverlay=w1-gpio' "$CONFIG_TXT"; then
  echo "Already set in $CONFIG_TXT"
else
  cp "$CONFIG_TXT" "$CONFIG_TXT.before-cupboard-door"
  # [all] makes sure the line is not caught inside a model-specific section.
  printf '\n# Added by cupboard-door install.sh: DS18B20 temperature sensor on GPIO4\n[all]\ndtoverlay=w1-gpio,gpiopin=4\n' >> "$CONFIG_TXT"
  echo "Added dtoverlay=w1-gpio,gpiopin=4 to $CONFIG_TXT (backup saved next to it)"
  REBOOT_NEEDED=1
fi

step "Creating the service user '$SERVICE_USER'"
getent group gpio >/dev/null || groupadd --system gpio
if id "$SERVICE_USER" &>/dev/null; then
  echo "User already exists"
else
  useradd --system --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -a -G gpio "$SERVICE_USER"

step "Copying files to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/cupboard_door"
cp -r "$SRC_DIR/cupboard_door" "$APP_DIR/"
for f in requirements.txt door_config.example.json cupboard-door.service test-mode.sh README.md; do
  cp "$SRC_DIR/$f" "$APP_DIR/"
done
find "$APP_DIR/cupboard_door" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$APP_DIR/test-mode.sh"
chown -R root:root "$APP_DIR"

step "Setting up the Python environment"
# --system-site-packages lets the venv use lgpio and gpiozero from apt.
if [[ ! -x $APP_DIR/venv/bin/python ]]; then
  python3 -m venv --system-site-packages "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" -c "import lgpio, gpiozero, flask; print('Python packages OK')"

step "Settings file"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 755 "$STATE_DIR"
if [[ -f $STATE_DIR/door_config.json ]]; then
  echo "Keeping your existing $STATE_DIR/door_config.json"
else
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 644 "$SRC_DIR/door_config.example.json" "$STATE_DIR/door_config.json"
  echo "Created $STATE_DIR/door_config.json"
fi

step "Installing and enabling the systemd service"
install -m 644 "$SRC_DIR/cupboard-door.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE.service"
# Do not stop the installer if the service fails to start: everything else is
# in place, and the log lines below show why it failed.
START_OK=1
systemctl restart "$SERVICE.service" || START_OK=0
sleep 2
systemctl is-active --quiet "$SERVICE.service" || START_OK=0
systemctl --no-pager --lines=0 status "$SERVICE.service" || true
if [[ $START_OK == 0 ]]; then
  echo
  echo "!! The service did not start. Its last log lines:"
  journalctl -u "$SERVICE.service" -b --no-pager -n 15 -o cat || true
  echo
  echo "!! systemd will keep retrying every 5 seconds. See Troubleshooting in README.md."
fi

PORT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('web_port', 8080))" "$STATE_DIR/door_config.json" 2>/dev/null || echo 8080)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
if [[ $START_OK == 1 ]]; then
  echo "Done. The door controller starts by itself on every boot."
else
  echo "Installed, but the service is not running yet (see the log above)."
fi
echo "Web page:  http://${IP:-<pi-ip-address>}:$PORT/"
echo "     or:   http://$(hostname).local:$PORT/"
echo "Logs:      journalctl -u $SERVICE -f"
echo "Test mode: sudo $APP_DIR/test-mode.sh"
if [[ $REBOOT_NEEDED == 1 ]]; then
  echo
  echo "1-Wire was just turned on. Reboot once (sudo reboot) so the temperature sensor"
  echo "appears. The door works without the sensor until then."
fi
