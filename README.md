# Therminus

A smart heat pump controller for homes with underfloor heating (UFH). Therminus
runs as a small web server, receives room temperature from a sensor, and controls
the heat pump by writing a fake room-temperature setpoint to it via the ebus
protocol. It serves a mobile-first web UI that shows what's happening and why, in
plain language.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running](#running)
6. [Sending room temperature](#sending-room-temperature)
7. [The web UI](#the-web-ui)
8. [API reference](#api-reference)
9. [Running as a service](#running-as-a-service)
10. [Tuning guide](#tuning-guide)

---

## How it works

### The core idea

Heat pumps with ebus connectivity expose a register called `TargetTempHc` — the
room temperature setpoint for the heating circuit. Normally the pump's own
thermostat writes to this. Therminus takes over by writing its own values, using
the room temperature to decide what to write.

This is sometimes called *room compensation* or writing a *fake setpoint*. The
pump maps whatever setpoint it receives to a water temperature via its internal
heating curve (outdoor reset), so Therminus never needs to think about water
temperatures directly — it just nudges the setpoint up or down, and the pump
handles the rest.

### The two states

Therminus runs a two-state machine:

```
                   room below band
                   + rested                    floor cold + in band
RESTING ───────────────────────────────────────────────────────────> RUNNING
   ^                                                                      |
   |              room warm / compressor stops (heating confirmed)        |
   +──────────────────────────────────────────────────────────────────────+
```

**RESTING** — the pump is told to idle. Therminus writes a very low fake setpoint
(15°C), telling the pump there is no heat demand. The compressor stays off. This
state lasts for a minimum rest period before heating is considered again.

After the rest period, two conditions can trigger a move to RUNNING:

- **Room too cold** — room temperature has dropped below `setpoint − band`. The
  normal heating case.
- **Floor too cold** — the floor circuit flow temperature has dropped below
  `FLOOR_COMFORT_TEMP` (25°C) while the room is still within the band. The floor
  has given up its heat during the rest and needs a top-up even though the room
  itself hasn't cooled enough to trigger the normal threshold.

**RUNNING** — Therminus writes a proportional setpoint based on how far the room
is from the target temperature: `setpoint + Kp * (setpoint − room_temp)`. The
pump responds by heating the floor to whatever water temperature its heating curve
prescribes for that demand level. RUNNING ends when either:
- the room rises above `setpoint + band` (room is warm enough), or
- the pump's compressor stops on its own and the three-way valve confirms it was
  running in heating mode (not domestic hot water).

### Why not a full PID controller?

UFH floors have enormous thermal mass — changes in room temperature lag hours
behind changes in water temperature. An integral term accumulates error over
time, which sounds useful, but in practice it winds up aggressively while the
floor is slow to respond, then overshoots. Testing showed the pump's own outdoor
reset curve already provides the long-term correction that an integral would add,
without the instability. Pure proportional control is simpler, easier to reason
about, and works well here.

### Why bang-bang (on/off) as the outer loop?

Heat pumps are most efficient running long cycles at low intensity. Short
cycling (frequent start/stop) reduces efficiency and wears the compressor. The
bang-bang outer loop enforces minimum rest periods between runs, encouraging
longer and fewer heating cycles. The proportional inner loop then determines how
hard to push during a run.

### The three-way valve guard

The pump's compressor also runs for domestic hot water (DHW). Therminus must
not mistake a DHW compressor stop for the end of a heating run, or it would
incorrectly enter a long rest period mid-day.

The fix: Therminus reads the `vwzio/ThreeWayValve` register every tick during
active states and tracks whether the valve was on `heating circuit` while the
compressor was running. When the compressor stops, this saved flag (not the
current valve position, which may have already switched) tells Therminus whether
it was a heating run or a DHW run. Only heating stops trigger a state transition.

### Ebus communication

Therminus talks to the pump via [ebusd](https://github.com/john30/ebusd) (an
open-source ebus daemon) and [pyebus](https://github.com/ebus/pyebus) (a Python
async library for ebusd). It reads four telemetry values from the pump each
control tick:

| Register                       | Value                              |
|--------------------------------|------------------------------------|
| `hmu/RunDataFlowTemp`          | Floor circuit flow temperature (°C)|
| `hmu/RunDataCompressorSpeed`   | Compressor speed (%, 0 = idle)     |
| `vwzio/ThreeWayValve`          | `heating circuit` or `warm water circuit` |
| `vwzio/OutdoorTemp`            | Outdoor air temperature (°C)       |

And writes one value:

| Register                       | Value                              |
|--------------------------------|------------------------------------|
| `hmu/TargetTempHc`             | Fake room setpoint (°C)            |

### Room temperature input

Therminus does not read a room temperature sensor itself. Instead, an external
source POSTs the current reading to `/roomtemp` every few minutes. This can be
Home Assistant, a cron job, Node-RED, or any HTTP client. The endpoint is
intentionally simple and tolerant of different formats.

### The web UI

A mobile-first single-page app served at port 6790. The front card shows the
room temperature, a badge (Heating / Warming the floor / Resting), and a warm
plain-language sentence explaining what is happening and why. Weather icon and
current outdoor conditions are shown top-right, with day/night-aware icons. A
small info button flips the card to reveal a technical back panel with the raw
status string, last/next write times, and a room temperature history chart.

All updates are pushed from server to browser via Server-Sent Events (SSE) — no
polling, no page refreshes. Multiple browsers can be connected simultaneously and
all receive the same real-time updates.

---

## Requirements

- Python 3.11 or later
- [ebusd](https://github.com/john30/ebusd) running and connected to your heat pump
- `uv` (Python package manager — see below)
- A room temperature sensor that can make HTTP POST requests (e.g. Home Assistant,
  a Raspberry Pi with a sensor, or any HTTP-capable device)

---

## Installation

### 1. Install uv

`uv` is a fast Python package and project manager. It handles virtual environments
and dependencies automatically.

On macOS and Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify the installation:

```bash
uv --version
```

See [uv documentation](https://docs.astral.sh/uv/) for more options.

### 2. Clone the repository

```bash
git clone https://github.com/yourname/therminus.git
cd therminus
```

### 3. Create a virtual environment and install dependencies

```bash
uv venv
uv pip install flask pyebus
```

This creates a `.venv` directory in the project folder. You do not need to
activate it manually — `uv run` handles that.

Alternatively, if the project has a `pyproject.toml`:

```bash
uv sync
```

### 4. Verify ebusd is running

Therminus connects to ebusd on `127.0.0.1:8888` by default. Check that ebusd
is running and accessible:

```bash
ebusctl info
```

If ebusd is on a different host or port, update `EBUSD_HOST` and `EBUSD_PORT`
at the top of `app.py`.

---

## Configuration

All configuration is at the top of `app.py`. The most important settings:

| Setting              | Default  | Description                                          |
|----------------------|----------|------------------------------------------------------|
| `EBUSD_HOST`         | `127.0.0.1` | ebusd hostname or IP                              |
| `EBUSD_PORT`         | `8888`   | ebusd port                                           |
| `SETPOINT`           | `21.0`   | Desired room temperature (°C)                        |
| `KP`                 | `1.0`    | Proportional gain. 1.0 = 1°C below target → 1°C nudge |
| `BAND`               | `0.3`    | Deadband around setpoint (°C). Prevents reacting to tiny fluctuations |
| `T_MIN_REST_SHORT`   | `1800`   | Seconds to rest after room-temp stop (30 min)        |
| `T_MIN_REST_LONG`    | `3600`   | Seconds to rest after compressor-off stop (60 min)   |
| `IDLE_TEMP`          | `15.0`   | Fake setpoint written while resting (°C)             |
| `FLOOR_COMFORT_TEMP` | `25.0`   | Flow temp below which a cold-floor run is triggered (°C) |
| `TARGET_MIN`         | `16.0`   | Minimum fake setpoint ever written (°C)              |
| `TARGET_MAX`         | `24.0`   | Maximum fake setpoint ever written (°C)              |
| `LATITUDE`           | `52.3` | Your latitude for weather fetch                    |
| `LONGITUDE`          | `4.98` | Your longitude for weather fetch                   |

---

## Running

### Development

```bash
uv run python app.py
```

The app starts on `http://0.0.0.0:6790`.

### With initial rest override

When restarting the app after a short downtime (e.g. a code update), you may
not want to wait the full 60-minute default rest before the pump can start:

```bash
# Start with 10-minute rest instead of 60
uv run python app.py --initial-rest 10

# Start with no rest (pump can start immediately on next tick)
uv run python app.py --initial-rest 0
```

Use `--initial-rest 0` only when you know the pump has been off long enough
that immediate re-engagement is safe.

---

## Sending room temperature

POST the current room temperature to `/roomtemp` every few minutes. The `current`
parameter accepts a variety of formats:

```bash
# Plain decimal
curl -X POST http://therminus-host:6790/roomtemp -d "current=21.4"

# Comma decimal (common in European locales)
curl -X POST http://therminus-host:6790/roomtemp -d "current=21,4"

# With unit suffix (e.g. from Home Assistant templates)
curl -X POST http://therminus-host:6790/roomtemp -d "current=21.4 °C"

# JSON body
curl -X POST http://therminus-host:6790/roomtemp \
  -H "Content-Type: application/json" \
  -d '{"current": 21.4}'
```

Valid range: 5–35°C. Values outside this range are rejected with a 400 error.

### Home Assistant example

In `configuration.yaml` or an automation, use a REST command:

```yaml
rest_command:
  send_room_temp:
    url: "http://192.168.1.x:6790/roomtemp"
    method: POST
    payload: "current={{ states('sensor.living_room_temperature') }}"
    content_type: "application/x-www-form-urlencoded"
```

Then call `rest_command.send_room_temp` from an automation that triggers every
5 minutes on sensor state change.

---

## The web UI

Open `http://therminus-host:6790` in any browser. It works well on mobile and
can be added to the iPhone home screen as a standalone app (Safari → Share →
Add to Home Screen).

**Front card** — shows:
- Day of week and current time (top left)
- Current weather icon and outdoor temperature (top right)
- Room temperature (large)
- State label: *HEATING*, *LOADING HOT WATER*, or *RESTING* (all-caps monospace, coloured)
- A plain-language sentence explaining the current situation

**Back panel** (tap the ⓘ button) — shows:
- Raw status string with technical details
- Current state (RUNNING / RESTING)
- Last write time and next write countdown
- Room temperature history chart for the current day

---

## API reference

All endpoints return JSON unless noted.

| Method | Path           | Description                                              |
|--------|---------------|----------------------------------------------------------|
| `GET`  | `/`           | Serves the web UI (HTML)                                 |
| `POST` | `/roomtemp`   | Receive room temperature. Body: `current=<value>`        |
| `GET`  | `/api/stream` | SSE stream of real-time state updates                    |
| `GET`  | `/api/state`  | Current state snapshot                                   |
| `GET`  | `/api/weather`| Latest cached weather data                               |

### SSE message types

Messages on `/api/stream` are JSON objects with a `type` field:

**`snapshot`** — sent immediately on connection, contains full current state:
```json
{
  "type": "snapshot",
  "room_temp": 20.9,
  "target": 21.0,
  "state": "RESTING",
  "status": "RESTING  room=20.9°C  rested=12/60min",
  "status_sentence": "Right where we want it. Having a rest for at least 48 more min.",
  "history": [{"ts": "2025-01-01T08:00:00", "value": 20.8}, ...],
  "weather": {"icon": "⛅", "desc": "Partly cloudy · 7.2°C outside"}
}
```

**`update`** — sent on every control tick (~60s) and every sensor POST:
```json
{
  "type": "update",
  "ts": "2025-01-01T10:00:00",
  "room_temp": 20.7,
  "target": 21.3,
  "state": "RUNNING",
  "wrote": true,
  "status": "RUNNING  room=20.7°C  err=+0.30  → 21.3°C  ran=8min",
  "status_sentence": "Working on it. Been at it for 8 min."
}
```

**`weather`** — sent when weather data refreshes (every 30 min):
```json
{
  "type": "weather",
  "icon": "🌧️",
  "desc": "Rain · 5.1°C outside"
}
```

---

## Running as a service

### systemd (Linux)

Create `/etc/systemd/system/therminus.service`:

```ini
[Unit]
Description=Therminus heat pump controller
After=network.target ebusd.service

[Service]
Type=simple
User=therminus
WorkingDirectory=/home/therminus/therminus
ExecStart=/home/therminus/therminus/.venv/bin/python app.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable therminus
sudo systemctl start therminus
sudo systemctl status therminus
```

View logs:

```bash
journalctl -u therminus -f
```

### Updating the app

When deploying a code update where the pump may have recently stopped:

```bash
sudo systemctl stop therminus
# deploy new code
sudo systemctl start therminus --initial-rest 0
```

Note: `--initial-rest` is passed via the `ExecStart` line when needed, or you
can temporarily edit the service file for a one-off restart.

---

## Tuning guide

### The room is slow to reach setpoint

Increase `KP` slightly (try 1.2 or 1.5). This makes the proportional nudge
larger, asking the pump for a higher water temperature when the room is cold.
Be careful not to go too high — the pump's own modulation handles the final
approach.

### The pump cycles too frequently

Increase `BAND` (try 0.4 or 0.5). A wider deadband means the room has to drift
further from setpoint before a state change is triggered, resulting in fewer but
longer heating cycles — which is better for heat pump efficiency.

### The floor feels cold between runs

Lower `FLOOR_COMFORT_TEMP` slightly (try 23°C). This makes the cold-floor
trigger fire earlier, starting a heating run before the floor has cooled as far.

### The pump runs too long / does not stop when the room is warm

Check that the three-way valve register name matches what your pump reports.
Look in the logs for lines like:
```
[therminus] ebus: flow=38°C  comp=45%  valve=heating circuit  ...
```
If `valve` shows something other than `heating circuit` during a heating run,
update `VALVE_HEATING` in the config to match.

### Reading the logs

Every tick during an active run produces a log line:
```
[therminus] ebus: flow=34.2°C  comp=32%  valve=heating circuit  outdoor=6.1°C  was_heating=True
[therminus] wrote TargetTempHc = 21.3
```

State transitions are logged explicitly:
```
[therminus] → RUNNING  room=20.6  rested=62min
[therminus] → RUNNING (cold floor)  flow=23.8°C  room=20.9
[therminus] → RESTING (compressor stopped)  flow=36.1°C  rest=60min
```

On startup, if the pump is already running:
```
[therminus] startup: compressor already running, → RUNNING
```
