# Cupboard Door

This program runs a motorised AC cupboard door on a Raspberry Pi 5. It opens and closes the door with a stepper motor. Pulling a book on the shelf toggles the door, and a fast temperature change opens it. You control it from a web page on your home network. It starts by itself when the Pi powers on and is built to recover safely after a power cut.

## Contents

1. [Parts and wiring](#1-parts-and-wiring)
2. [Set the A4988 current (Vref) before the first run](#2-set-the-a4988-current-vref-before-the-first-run)
3. [Install](#3-install)
4. [First-run checks with the motor disconnected](#4-first-run-checks-with-the-motor-disconnected)
5. [Connect the motor and tune it in test mode](#5-connect-the-motor-and-tune-it-in-test-mode)
6. [The web page](#6-the-web-page)
7. [How the door decides what to do](#7-how-the-door-decides-what-to-do)
8. [Settings](#8-settings)
9. [JSON API and Home Assistant](#9-json-api-and-home-assistant)
10. [Logs](#10-logs)
11. [The watchdog](#11-the-watchdog)
12. [Power loss test](#12-power-loss-test)
13. [SD card hardening (optional)](#13-sd-card-hardening-optional)
14. [Troubleshooting](#14-troubleshooting)
15. [Why gpiozero with lgpio](#15-why-gpiozero-with-lgpio)
16. [Try it on a laptop, run the tests](#16-try-it-on-a-laptop-run-the-tests)
17. [Adding a password later](#17-adding-a-password-later)
18. [Uninstall](#18-uninstall)
19. [Files](#19-files)

---

## 1. Parts and wiring

Parts:

- Raspberry Pi 5 running Raspberry Pi OS (Bookworm)
- NEMA 17 stepper motor (SparkFun ROB-09238)
- Pololu A4988 stepper driver carrier, Black Edition
- Door-closed limit switch (pressed when the door is fully closed)
- Book lever limit switch (pressed while the book rests against it)
- DS18B20 temperature sensor, with the 4.7k pull-up already fitted
- 12 V supply for the driver, and a DIN rail DC-DC converter that gives the Pi 5 V

Pin numbers below are BCM GPIO numbers. The "Pi pin" column is the physical pin on the 40-pin header.

| What | Connects to | GPIO (BCM) | Pi pin |
|---|---|---|---|
| A4988 STEP | Pi | GPIO20 | 38 |
| A4988 DIR | Pi | GPIO21 | 40 |
| A4988 ENABLE | Pi | GPIO16 | 36 |
| **10k resistor** | between A4988 ENABLE and Pi 3.3 V | | 1 or 17 |
| A4988 VDD (logic power) | Pi 3.3 V | | 1 or 17 |
| A4988 GND (logic side) | Pi GND | | 39 (or any GND) |
| A4988 SLEEP and RESET | joined to each other with a short wire | | |
| A4988 MS1, MS2, MS3 | leave unconnected (full steps) | | |
| A4988 VMOT and GND | 12 V supply + and - | | |
| A4988 1A, 1B, 2A, 2B | motor coils | | |
| Door-closed switch COM | Pi GND | | 14 |
| Door-closed switch NO | Pi | GPIO23 | 16 |
| Book lever switch COM | Pi GND | | 20 |
| Book lever switch NO | Pi | GPIO24 | 18 |
| DS18B20 data (DQ) | Pi, with 4.7k to 3.3 V | GPIO4 | 7 |
| DS18B20 VDD | Pi 3.3 V | | 1 or 17 |
| DS18B20 GND | Pi GND | | 9 |

Notes:

- **The 10k resistor from ENABLE to 3.3 V matters.** The A4988 ENABLE pin turns the driver on when it is low. While the Pi boots, and before this program starts, GPIO16 is not driven, so without the resistor the driver could switch on and energise the motor. The resistor holds ENABLE high (driver off) until the program takes over. The program then keeps it high whenever the motor is not moving.
- All grounds must be joined: the Pi, the A4988 logic GND, the A4988 motor GND, the 12 V supply and the DC-DC converter.
- Fit a 47 uF (or larger) electrolytic capacitor across VMOT and GND, close to the A4988. Pololu recommends this to protect the driver from voltage spikes.
- On the Pololu board, RESET is not pulled up. Joining SLEEP to RESET keeps the driver awake. Without this the motor will not move.
- Wire the switches to the NO (normally open) terminal, so a pressed switch connects the GPIO to GND. The program turns on the Pi's internal pull-up, so a pressed switch reads low.
- **Never connect or disconnect the motor while the 12 V supply is on.** It can destroy the A4988.
- The pin numbers are kept in one block at the top of `cupboard_door/config.py`. To change one, add it under `"pins"` in `door_config.json` (for example `"pins": {"step": 12}`) and restart the service.

## 2. Set the A4988 current (Vref) before the first run

The A4988 limits the current through the motor. You set the limit with the small screw (trimpot) on the board. Set it before you run the motor for the first time, or the motor and driver can overheat.

1. Find your motor's rated current per coil. It is on the label or the datasheet. SparkFun lists the ROB-09238 at about 0.33 A, but check yours.
2. Find the value of the two small current sense resistors next to the A4988 chip. They are marked with a code: `R050` means 0.050 ohms, `R068` means 0.068 ohms, `R100` means 0.100 ohms.
3. Work out the target voltage:

   **Vref = rated current x 8 x sense resistor value**

   Examples for a 0.33 A motor:
   - R068 resistors: 0.33 x 8 x 0.068 = **0.18 V**
   - R050 resistors: 0.33 x 8 x 0.050 = **0.13 V**

4. With the Pi on (so the A4988 has 3.3 V logic power) and the motor **disconnected**, put the multimeter's black probe on GND and the red probe on the metal top of the trimpot. Turn the trimpot gently (clockwise usually raises it) until the meter shows your target.
5. Use a plastic or ceramic screwdriver if you have one. A metal one can short nearby pins.

The program drives the motor in full steps. In full-step mode each coil gets about 70% of the limit you set, so the motor runs a little cooler than its rating. If the motor is too weak to move the door, first try a lower speed and more ramp steps. Do not set Vref above the value for the rated current. The driver needs a heat sink if it gets too hot to touch.

## 3. Install

Copy this folder to the Pi (for example with `git clone` or `scp`), then:

```bash
cd cupboard-door
sudo ./install.sh
```

The installer:

- installs the system packages it needs (`python3-venv`, `python3-lgpio`, `python3-gpiozero`)
- turns on the 1-Wire interface by adding `dtoverlay=w1-gpio,gpiopin=4` to `/boot/firmware/config.txt`, only if it is not there already
- creates a user called `cupboard-door` with no login and no password, in the `gpio` group
- copies the program to `/opt/cupboard-door` and makes a Python virtual environment there
- creates the settings file `/var/lib/cupboard-door/door_config.json` (an existing one is kept)
- installs and enables the `cupboard-door` systemd service, so it starts on every boot with no login
- prints the address of the web page

It is safe to run again, for example after updating the code. Your settings are kept.

If the installer just turned on 1-Wire, reboot once with `sudo reboot` so the temperature sensor appears.

## 4. First-run checks with the motor disconnected

Do these with the 12 V supply **off** and the motor **unplugged** from the A4988.

1. Check the service is running:
   ```bash
   systemctl status cupboard-door
   ```
   It should say `active (running)`.
2. Open the web page (the installer printed the address, usually `http://<pi-ip-address>:8080/`).
3. Press the door-closed switch by hand. The page should show "Closed switch: Pressed", and "Not pressed" when you let go.
4. Lift the book off the lever. The page should show "Book lever: Released", and "Book in place" when you put it back.
5. Check the temperature shows a sensible number. "No sensor" means the sensor was not found (see [Troubleshooting](#14-troubleshooting)).
6. With a multimeter, check the A4988 ENABLE pin reads about 3.3 V (driver off) while nothing is moving.
7. Now turn the 12 V supply on (motor still unplugged) and set Vref as described in [section 2](#2-set-the-a4988-current-vref-before-the-first-run).
8. Press OPEN on the web page. "Motor driver" should show ON while the move runs, then Off again. The ENABLE pin should drop to near 0 V during the move and return to 3.3 V afterwards.
9. Press CLOSE. With no motor the door cannot reach the switch, so after the step limit the page should show a **fault**. That is the fault check working. Press the door-closed switch by hand, then press "Reset fault". The door should show "closed".

Turn the 12 V supply off, plug the motor in, then turn the 12 V back on.

## 5. Connect the motor and tune it in test mode

Test mode lets you drive the door with the keyboard, find the right open position and try speeds. The book lever and temperature trigger are switched off while it runs.

```bash
sudo /opt/cupboard-door/test-mode.sh
```

This stops the service (only one program can use the pins at a time), runs test mode, and starts the service again when you quit.

Keys:

| Key | Action |
|---|---|
| `d` / `a` | jog 50 steps open / closed |
| `D` / `A` | jog 200 steps open / closed |
| `c` / `z` | jog 10 steps open / closed |
| `h` | close: drive to the closed switch (position becomes 0) |
| `o` | open to the saved open position |
| `p` | set the open position here (steps_to_open = current position) |
| `+` / `-` | speed up / down by 50 steps per second |
| `]` / `[` | more / fewer ramp steps |
| `f` | flip the opening direction |
| `x` | reset a fault |
| space | stop the current move |
| `s` | save to the settings file |
| `q` | quit |

The status line shows the state, position, speed, ramp, direction, both switches and the temperature.

Tuning steps:

1. Press `h` to close the door. Position should show 0 and the closed switch PRESSED.
2. Press `c` once. If the door moves towards closed instead of open, press `f` to flip the direction, then `h` again.
3. Jog open with `d` and `D` until the door is where you want "fully open". Press `p` to save that as the open position.
4. Press `h` to close, then `o` to open. Watch and listen.
5. If the motor stalls or skips (buzzes, jerks, or the door ends up short), lower the speed with `-` or add ramp steps with `]`. If it is smooth, you can try more speed with `+`.
6. Press `s` to save, then `q` to quit.

Good starting values for a full-step NEMA 17: speed 600 to 1000 steps per second, ramp 150 to 300 steps. A full turn of the motor is 200 steps.

## 6. The web page

Open `http://<pi-ip-address>:8080/` (or `http://<hostname>.local:8080/`) on any phone or computer on the same network. The page works only on your local network; nothing goes to the internet.

It shows:

- the door state: open, closed, unknown, or opening / closing / jogging while it moves
- the temperature
- whether there is a fault, and why
- the last thing that happened
- both switches, the position in steps, and whether the motor driver is on

Buttons: OPEN, CLOSE, TOGGLE, jog -200, -50, +50, +200 steps, and Reset fault when there is a fault.

The settings form changes steps_to_open, speed, ramp steps, opening direction, temperature change, temperature window and book lockout. Saved settings take effect straight away, without a restart.

## 7. How the door decides what to do

**Opening** moves the motor `steps_to_open` steps in the opening direction.

**Closing** drives towards the closed switch and stops the moment it is pressed. If the switch has not been pressed after `steps_to_open + close_margin_steps` steps, the motor stops, a warning goes in the log, and the door enters a **fault**. Nothing moves again until you press "Reset fault" (on the page, `x` in test mode, or the API). This protects the mechanism if something is stuck or the switch has failed. Opening also checks the switch: if the closed switch is still pressed after a full open, the switch must be stuck or shorted, so that is a fault too. Without this check, the next toggle would think the door was closed and drive it past its open stop.

**Book lever:** pulling the book (the lever is released) toggles the door. The signal is debounced (50 ms), and one pull gives one toggle. After a toggle, further pulls are ignored for the book lockout time (3 s by default). Leaving the book out does not keep toggling.

**Temperature:** if the temperature rises or falls by `temp_delta_c` (3.0 C) within `temp_window_s` (300 s), the door opens if it is closed. The history is then cleared, so one change gives one trigger. If the sensor is missing or gives bad readings, the door keeps working without it.

**Only one move at a time:** a single lock covers every move. A request that arrives while the door is moving (from the book, temperature, web page or API) is refused, not queued.

**The motor is only powered while moving.** The driver is switched off (ENABLE high) at all other times so the motor stays cool.

**At start-up** (including after a power cut) the program never moves the door by itself:

- If the closed switch is pressed, the door is closed. Nothing else happens.
- If it is not pressed, the power may have been cut part way through a move, so the program cannot know where the door is. The state is **unknown**, the motor stays off, and the web page shows a yellow notice. The next book pull, temperature trigger, or press of Open, Close or Toggle will **drive the door closed** (with the normal step limit and fault check). It never opens from an unknown position, because an open move counts steps from "closed" and could drive the door past its fully open point. Once the switch is found, the position is known again and everything works as normal. The jog buttons still work in this state, for moving the door under your own control.

## 8. Settings

The settings live in `/var/lib/cupboard-door/door_config.json`. Change them on the web page, in test mode, or by editing the file and running `sudo systemctl restart cupboard-door`. `door_config.example.json` shows every setting.

| Setting | Default | Meaning |
|---|---|---|
| `steps_to_open` | 1600 | Steps from fully closed to fully open |
| `speed_pps` | 800 | Cruising speed, steps per second |
| `ramp_steps` | 200 | Steps used to speed up at the start, and to slow down at the end |
| `start_pps` | 150 | Speed at the very start and end of a move, and while creeping onto the closed switch |
| `open_dir` | 1 | DIR pin level that opens the door (1 = high, 0 = low) |
| `close_margin_steps` | 300 | Extra steps allowed when closing before it counts as a fault |
| `temp_delta_c` | 3.0 | Temperature change (C) that opens the door |
| `temp_window_s` | 300 | Time window (seconds) for that change |
| `temp_poll_s` | 10 | How often to read the sensor (seconds) |
| `book_debounce_ms` | 50 | How long the lever must stay released to count |
| `book_lockout_s` | 3.0 | Time after a toggle when more pulls are ignored |
| `web_port` | 8080 | Web page port (restart needed) |
| `sensor_wait_s` | 60 | How long to wait for the sensor at start-up before carrying on without it |
| `pins` | see wiring | GPIO numbers (restart needed) |

The settings file is written safely: the new version goes to a temporary file, is flushed to the card, and then replaces the old file in one step. A power cut while saving leaves either the old file or the new one, never a broken one. Apart from settings you save, the program writes nothing to the SD card. If the file is damaged anyway, the program logs an error and runs on the defaults rather than refusing to start.

## 9. JSON API and Home Assistant

All endpoints are under `/api/`. Moves start in the background and return straight away.

| Method and path | What it does |
|---|---|
| `GET /api/status` | door state, position, fault, temperature, switches, last event |
| `POST /api/open` | open (closes instead if the position is unknown) |
| `POST /api/close` | close |
| `POST /api/toggle` | toggle |
| `POST /api/jog` with `{"steps": 50}` | jog; positive is the opening direction |
| `GET /api/config` | current settings and allowed ranges |
| `POST /api/config` with `{"speed_pps": 900}` | change and save settings (any subset) |
| `POST /api/reset_fault` | clear a fault |

Replies are JSON like `{"ok": true, "message": "Opening", "started": true}`. HTTP codes: 202 a move started, 200 done or nothing to do, 400 bad input, 409 refused (busy or fault).

Examples:

```bash
curl http://<pi-ip-address>:8080/api/status
curl -X POST http://<pi-ip-address>:8080/api/open
curl -X POST -H 'Content-Type: application/json' -d '{"steps": -50}' http://<pi-ip-address>:8080/api/jog
```

Home Assistant (`configuration.yaml`):

```yaml
rest_command:
  cupboard_door_open:
    url: http://192.168.1.50:8080/api/open
    method: POST
  cupboard_door_close:
    url: http://192.168.1.50:8080/api/close
    method: POST

sensor:
  - platform: rest
    name: Cupboard door
    resource: http://192.168.1.50:8080/api/status
    value_template: "{{ value_json.state }}"
    json_attributes: [temperature_c, fault, last_event]
    scan_interval: 10
```

Give the Pi a fixed IP address (a DHCP reservation in your router) so these addresses do not change.

## 10. Logs

The program logs only to the systemd journal. It writes no log files of its own.

```bash
journalctl -u cupboard-door -f              # follow live
journalctl -u cupboard-door -b              # everything since the last boot
journalctl -u cupboard-door -p warning      # only warnings and errors (faults, sensor problems)
journalctl -u cupboard-door --since "1 hour ago"
systemctl status cupboard-door              # running or not, plus a one-line door status
```

**Cap the journal size** so it cannot fill the SD card:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=50M\n' | sudo tee /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
```

To write nothing to the card at all, use `Storage=volatile` instead of (or as well as) `SystemMaxUse`. Logs are then kept in memory and lost on reboot.

## 11. The watchdog

The service uses the systemd watchdog. While everything is healthy, the program checks in with systemd every 15 seconds. If a part of the program hangs (the book monitor, temperature monitor, web server, or a motor move stalls), it stops checking in, and after 30 seconds systemd restarts it. After a restart the start-up rules in [section 7](#7-how-the-door-decides-what-to-do) apply.

To turn the watchdog off while testing or debugging:

```bash
sudo systemctl edit cupboard-door
```

Add these lines in the space provided, then save:

```ini
[Service]
WatchdogSec=0
```

```bash
sudo systemctl restart cupboard-door
```

To turn it back on: `sudo systemctl revert cupboard-door` and then `sudo systemctl restart cupboard-door`.

## 12. Power loss test

Do this once everything is tuned, to prove the door recovers by itself.

1. **Normal reboot.** Run `sudo reboot`. When the Pi is back, log in and run `systemctl status cupboard-door`. It should be `active (running)` and enabled. The web page should load without anyone logging in.
2. **Power cut with the door closed.** Close the door. Pull the Pi's power (and the 12 V) for a few seconds, then put it back. After about a minute:
   - the web page shows **closed**
   - the motor did not move or hum during boot
   - `journalctl -u cupboard-door -b` shows "door-closed switch is pressed, so the door is closed"
3. **Power cut with the door open.** Open the door. Pull the power for a few seconds, then put it back. After about a minute:
   - the web page shows **unknown** with the yellow notice
   - the door did not move by itself, and the motor is not powered (it turns freely by hand, and "Motor driver" shows Off)
   - the log shows "door-closed switch is NOT pressed, so the door position is unknown"
   - pull the book (or press Open, Close or Toggle): the door **closes** and the state becomes closed. It does not open.
4. **Optional: power cut mid-move.** Start a move and pull the power part way. On restart you should see the same result as step 3.

## 13. SD card hardening (optional)

Sudden power loss can damage an SD card that is being written to. This program already keeps writes to a minimum. For extra safety:

- **Use a high-endurance SD card** (sold for dash cams and security cameras). They handle sudden power loss and constant writes far better than ordinary cards.
- **Cap the journal** as shown in [section 10](#10-logs), or keep it in memory with `Storage=volatile`.
- **Turn on the read-only overlay file system** once everything works and is tuned. Run `sudo raspi-config`, choose *Performance Options*, then *Overlay File System*, and enable it (say yes to making the boot partition read-only too). Reboot. All writes then go to memory and the card is never written to, so power cuts cannot damage it. While the overlay is on, **settings changed on the web page are lost at the next reboot**, and so are logs. To change settings permanently, turn the overlay off in raspi-config, reboot, make the changes, then turn it back on.

## 14. Troubleshooting

**The motor runs the wrong way** (Open closes, Close opens). Change `open_dir` from 1 to 0 (or 0 to 1) on the web page, or press `f` in test mode. You can also swap the two wires of one motor coil (for example swap 1A and 1B), with the 12 V turned off. If the direction is wrong, closing drives away from the switch and ends in a fault, so you will see the fault before anything is damaged.

**The door never reaches the closed switch** (fault after closing).
- Watch it close. If it stops short, `steps_to_open` plus `close_margin_steps` is too small. Re-tune the open position in test mode or raise `close_margin_steps`.
- If it moves the wrong way, see above.
- If the motor buzzes or jerks without turning properly, it is stalling. Lower `speed_pps`, raise `ramp_steps`, or check Vref is set and the 12 V supply is on.
- Check the switch: press it by hand and watch "Closed switch" on the web page. If it never changes, check the wiring (COM to GND, NO to GPIO23).
- Check the switch is actually pressed when the door is fully shut. Adjust its position or lever.
- After fixing the cause, press "Reset fault".

**Temperature shows "No sensor".**
- Check `/boot/firmware/config.txt` has `dtoverlay=w1-gpio,gpiopin=4` and that you rebooted after it was added.
- Run `ls /sys/bus/w1/devices/`. You should see a folder starting with `28-`. If not, check the wiring: data to GPIO4 (pin 7), 4.7k between data and 3.3 V, VDD to 3.3 V, GND to GND.
- Run `cat /sys/bus/w1/devices/28-*/w1_slave`. The first line should end in `YES` and the second should have `t=` followed by the temperature in thousandths of a degree.
- The program keeps checking every 30 seconds, so a sensor that is fixed later is picked up without a restart. Read errors and CRC failures are logged and skipped.

**The web page does not load.**
- Run `systemctl status cupboard-door`. If it is not running, see why with `journalctl -u cupboard-door -b`.
- Check you are using the right address: `hostname -I` on the Pi prints its IP address. Use `http://`, not `https://`, and port 8080.
- Check your phone or computer is on the same network as the Pi (not a guest network).
- If you run a firewall on the Pi (for example `ufw`), allow port 8080: `sudo ufw allow 8080/tcp`.
- If the log says the port is in use, another program is using 8080. Stop it, or change `web_port` in the settings file and restart.

**"Could not set up the GPIO pins".** Another program is using the pins. Usually this means the service is still running while you start test mode by hand. Use `test-mode.sh`, which stops the service for you.

## 15. Why gpiozero with lgpio

The Pi 5 moved its GPIO pins to a new chip (the RP1), and the old `RPi.GPIO` library does not work with it. The two options that do are `gpiozero` with the `lgpio` pin factory, and `rpi-lgpio`.

`rpi-lgpio` is a compatibility layer that copies the old `RPi.GPIO` commands on top of `lgpio`. It exists to help old code run. It adds a layer and brings no timing benefit.

This program uses **gpiozero with the lgpio pin factory**. It is the stack Raspberry Pi recommend and ship with Raspberry Pi OS, it talks to the pins through the standard Linux GPIO interface, and gpiozero handles the switch pull-ups and the clean release of pins on exit.

For step timing, what matters most is how the program spaces the pulses, not the library:

- Each STEP pulse writes straight to the lgpio pin, skipping gpiozero's extra checks, so a pulse costs only a few microseconds.
- Pulse times are planned against a fixed clock (a running deadline), so small delays do not add up over a move.
- The program sleeps for most of each gap (leaving the CPU free for the web server) and then waits actively for the last fraction of a millisecond, which gives timing accurate to a few microseconds on the Pi 5.
- Linux is not a real-time system, so now and then a step may be a little late. When that happens, the program makes up the time gradually and never sends a burst of fast steps that could make the motor stall.

At the speeds a cupboard door needs (a few hundred to about a thousand steps per second) this is smooth and reliable. A hardware pulse generator would only be needed for much higher speeds.

## 16. Try it on a laptop, run the tests

You can run everything, including the web page and test mode, on a laptop with no hardware. `--simulate` uses gpiozero's mock pins, a pretend door with a closed switch, and a fake temperature sensor.

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements-dev.txt

python -m cupboard_door --simulate                      # web page on http://localhost:8080/
python -m cupboard_door --simulate --sim-start-open     # start part open, as after a power cut
python -m cupboard_door --simulate --test               # test mode in the terminal
```

In simulation the web page has an extra panel to pull the book, change the temperature, unplug the sensor and break the closed switch.

Run the tests:

```bash
pytest
```

The tests cover the speed ramp and step timing, the temperature window, the book debounce and lockout, the fault state, the start-up state (switch pressed, switch not pressed, sensor missing), settings files, and the API.

## 17. Adding a password later

The web page and API have no password, so anyone on your home network can use them. Keep the Pi off guest networks and do not forward port 8080 on your router. If you want a password later, the simplest options are:

- **A shared token for the API and Home Assistant.** Add this to `create_app` in `cupboard_door/web.py`, and set the token in the service with `sudo systemctl edit cupboard-door` (`[Service]` then `Environment=DOOR_TOKEN=some-long-random-text`):

  ```python
  import hmac, os

  token = os.environ.get("DOOR_TOKEN")

  @app.before_request
  def check_token():
      if token and not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}"):
          return jsonify({"ok": False, "message": "Not allowed"}), 401
  ```

  In Home Assistant add `headers: {Authorization: "Bearer some-long-random-text"}` to each `rest_command`. The web page would also need to send the header.
- **A password prompt in the browser.** Put a small web server such as Caddy or nginx in front with basic authentication, and change `host` in the `WebServer` class in `cupboard_door/web.py` to `127.0.0.1` so the program can only be reached through that server.

## 18. Uninstall

```bash
sudo systemctl disable --now cupboard-door
sudo rm /etc/systemd/system/cupboard-door.service
sudo systemctl daemon-reload
sudo rm -rf /opt/cupboard-door
sudo rm -rf /var/lib/cupboard-door      # deletes your settings
sudo userdel cupboard-door
```

To turn 1-Wire off as well, remove the `dtoverlay=w1-gpio,gpiopin=4` line (and the comment above it) from `/boot/firmware/config.txt` and reboot. The installer saved the original as `config.txt.before-cupboard-door`.

## 19. Files

```
cupboard_door/
  config.py       settings, pin numbers, safe saving of door_config.json
  hardware.py     GPIO pins (real and simulated)
  motor.py        speed ramp and step timing
  door.py         door state, move lock, closed switch, faults, start-up rules
  sensors.py      book lever (debounce, lockout) and DS18B20 temperature
  web.py          web page and JSON API
  main.py         start-up, shutdown, systemd watchdog, command line
  testmode.py     terminal test mode
  static/index.html
tests/            pytest tests, all using the simulator
install.sh        installer, safe to run more than once
test-mode.sh      runs test mode on the Pi (stops and restarts the service)
cupboard-door.service
door_config.example.json
requirements.txt, requirements-dev.txt, pyproject.toml
```

Code on the Pi lives in `/opt/cupboard-door`. Settings live in `/var/lib/cupboard-door/door_config.json`.
