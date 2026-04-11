# Therminus

A smart heat pump controller for homes with underfloor heating (UFH). Therminus
runs as a small web server, receives room temperature from a sensor, and controls
the heat pump by writing a fake room-temperature setpoint to it via the ebus
protocol. It serves a mobile-first web UI that shows what's happening in plain
language.

---

## How it works

### The core idea

Heat pumps with ebus connectivity expose a register called `TargetTempHc` — the
room temperature setpoint for the heating circuit. Normally the pump's own
thermostat writes this. Therminus takes over: it reads the actual room
temperature from an external sensor and writes its own setpoint, nudging the
pump up or down based on how far the room is from the target.

This is sometimes called *room compensation* or a *fake setpoint*. The pump maps
whatever setpoint it receives to a water temperature via its internal heating
curve (outdoor reset), so Therminus never has to think about water temperatures
directly — it just nudges the setpoint, and the pump handles the rest.

### States

Therminus runs a bang-bang outer loop with a proportional inner loop:

```
                  room below band
                  + rested                    floor cold + in band
RESTING ──────────────────────────────────────────────────────────> RUNNING
   ^                                                                     |
   |             room warm / compressor stops (heating confirmed)        |
   +─────────────────────────────────────────────────────────────────────+
```

**RESTING** — the pump is told to idle. Therminus writes a very low fake
setpoint (15°C), telling the pump there is no heat demand. The compressor
stays off. This state enforces a minimum rest period before heating can start
again, preventing short-cycling.

After the rest period, two conditions can trigger a move to RUNNING:

- **Room too cold** — room temperature has dropped below `SETPOINT − BAND`.
  The normal heating case.
- **Floor too cold** — the floor circuit flow temperature has dropped below
  `FLOOR_COMFORT_TEMP` (25°C) while the room is still within the band. The
  floor has given up its stored heat and needs a top-up even though the room
  itself hasn't cooled enough yet.

**RUNNING** — Therminus writes a proportional setpoint:
`SETPOINT + KP × (SETPOINT − room_temp)`. The pump responds by heating the
floor to whatever water temperature its heating curve prescribes for that
demand level. RUNNING ends when:

- the room rises above `SETPOINT + BAND` (room is warm enough), or
- the pump's compressor stops while the three-way valve confirms it was on
  the heating circuit (not domestic hot water).

### Why not a full PID?

UFH floors have enormous thermal mass — room temperature changes lag hours
behind water temperature changes. An integral term winds up aggressively while
the floor is slow to respond, then overshoots badly. The pump's own outdoor
reset curve already provides the long-term correction an integral would add,
without the instability. Proportional-only is simpler, easier to reason about,
and works well here.

### Why bang-bang as the outer loop?

Heat pumps are most efficient running long cycles at low intensity. Short
cycling (frequent start/stop) reduces efficiency and wears the compressor. The
bang-bang outer loop enforces minimum rest periods between runs, encouraging
longer and fewer cycles. The proportional inner loop determines how hard to push
during a run.

### ebus communication

Therminus talks to the pump via [ebusd](https://github.com/john30/ebusd) and
[pyebus](https://github.com/ebus/pyebus). It reads four values from the pump
each control tick:

| Register                       | Value                                    |
|--------------------------------|------------------------------------------|
| `hmu/RunDataFlowTemp`          | Floor circuit flow temperature (°C)      |
| `hmu/RunDataCompressorSpeed`   | Compressor speed (%, 0 = idle)           |
| `vwzio/ThreeWayValve`          | `heating circuit` or `warm water circuit`|
| `vwzio/OutdoorTemp`            | Outdoor air temperature (°C)             |

And writes one value:

| Register         | Value                    |
|------------------|--------------------------|
| `hmu/TargetTempHc` | Fake room setpoint (°C) |

### Room temperature input

Therminus does not read a room sensor itself. An external source POSTs the
current reading to `/roomtemp` every few minutes — Home Assistant, a cron job,
Node-RED, or any HTTP client. The endpoint tolerates many input formats.

### Web UI

A mobile app at port 6790. The web app is continually updated
by the web server. Multiple browsers can be connected simultaneously.

**Front card:**
- Day/time (top left), weather icon and outdoor temperature (top right)
- Current room temperature (large)
- State badge: *HEATING*, *LOADING HOT WATER*, or *RESTING*
- A plain-language sentence explaining what is happening and why

**Back panel** (tap the ⓘ button):
- Raw status string with technical details
- Last write time and next write countdown
- Room temperature history chart for the current day

---

## Requirements

- Python 3.11 or later
- Probably `uv` (Python package manager)
- [ebusd](https://github.com/john30/ebusd) running and connected to your heat pump
- A way to POST the current room temperature to the application:
  - Home Assistant may be able to do this
  - Apple Home can be configured to post a temperature every 5 minutes
  - Maybe you have a sensor that can post to a web service

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
uv pip install -r requirements.txt
```

### 4. Verify ebusd is running

Therminus connects to ebusd on `127.0.0.1:8888` by default:

```bash
ebusctl info
```

If ebusd is on a different host or port, update `EBUSD_HOST` and `EBUSD_PORT`
at the top of `ebus.py`.

---

## Configuration

All configuration lives at the top of `app.py` and `controller.py`.

| Setting              | Default     | Description                                          |
|----------------------|-------------|------------------------------------------------------|
| `EBUSD_HOST`         | `127.0.0.1` | ebusd hostname or IP                                 |
| `EBUSD_PORT`         | `8888`      | ebusd port                                           |
| `SETPOINT`           | `21.0`      | Desired room temperature (°C)                        |
| `KP`                 | `1.0`       | Proportional gain (1°C error → 1°C nudge)            |
| `BAND`               | `0.3`       | Deadband around setpoint (°C)                        |
| `T_MIN_REST_SHORT`   | `1800`      | Rest after room-temp stop (30 min)                   |
| `T_MIN_REST_LONG`    | `3600`      | Rest after compressor-off stop (60 min)              |
| `IDLE_TEMP`          | `15.0`      | Fake setpoint written while resting (°C)             |
| `FLOOR_COMFORT_TEMP` | `25.0`      | Flow temp below which a cold-floor run is triggered  |
| `TARGET_MIN`         | `16.0`      | Minimum fake setpoint ever written (°C)              |
| `TARGET_MAX`         | `24.0`      | Maximum fake setpoint ever written (°C)              |
| `LATITUDE`           | `52.3`  | Your latitude for weather                            |
| `LONGITUDE`          | `4.98`   | Your longitude for weather                           |

---

## Running

```bash
uv run python app.py
```

The app starts on `http://0.0.0.0:6790`.

### Initial rest override

After a short restart you may not want to wait the full rest period before the
pump can start again:

```bash
# Start with a 10-minute rest instead of 60
uv run python app.py --initial-rest 10

# Start with no rest (pump can start immediately on next tick)
uv run python app.py --initial-rest 0
```

Use `--initial-rest 0` only when it's OK that the app starts your pump immediately (if needed).

---

## Sending room temperature

POST the current room temperature to `/roomtemp` every few minutes. The
`current` parameter accepts several formats:

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

## Tuning

### Room is slow to reach setpoint

Increase `KP` slightly (try 1.2 or 1.5). This asks the pump for a higher water
temperature when the room is cold. Don't go too high — the pump's own modulation
handles the final approach.

### Pump cycles too frequently

Increase `BAND` (try 0.4 or 0.5). A wider deadband means the room has to drift
further before a state change fires, resulting in fewer but longer heating
cycles — which is better for heat pump efficiency.

### Floor feels cold between runs

Lower `FLOOR_COMFORT_TEMP` slightly (try 23°C). This triggers the cold-floor
start earlier, before the floor has cooled as far.

### Pump runs too long / doesn't stop when room is warm

Check that the valve register name matches what your pump reports. Look in the
logs for:

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
