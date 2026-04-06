"""
Therminus — Heat Pump Controller
=================================
Controls a hydronic heat pump with underfloor heating (UFH) by writing a
fake room-temperature setpoint (TargetTempHc) to the pump via ebusd/pyebus.

See doc/ARCHITECTURE.md for an overview.
"""
import asyncio
import threading
import json
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
from flask import Flask, Response, render_template, jsonify, request
from controller import HeatPumpController

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
EBUSD_HOST      = "127.0.0.1"
EBUSD_PORT      = 8888

# Minimum time between writes to ebusd. Writing more often than the sensor
# posts (every ~5 min) is fine because the background thread updates the
# target every CONTROL_DT seconds regardless of sensor arrivals.
UPDATE_INTERVAL = 60    # seconds
CONTROL_DT      = 60    # seconds — how often the control loop runs

# Number of room-temp readings to keep in memory for the history chart.
# At one reading per ~5 minutes, 1440 points covers roughly 5 days.
HISTORY_POINTS  = 1440

# ── External API config ────────────────────────────────────────────────────────
# Coordinates for Amsterdam — used for the Open-Meteo weather fetch.
# Open-Meteo is free and requires no API key.
LATITUDE         = "52.3"
LONGITUDE        = "4.98"
WEATHER_INTERVAL = 30 * 60      # seconds between weather refreshes

# ── Shared state ──────────────────────────────────────────────────────────────
# All variables below are accessed from multiple threads (Flask request handlers,
# the background control thread, and the SSE generator). All reads and writes
# must happen under state_lock, except where explicitly noted.
state_lock       = threading.Lock()
current_temp     = None          # most recent room temp from sensor POST (°C)
room_history     = deque(maxlen=HISTORY_POINTS)  # list of {ts, value} dicts for chart
state_events     = deque(maxlen=2000)            # list of {ts, state} — badge transitions today
_prev_badge_cls  = None                          # last recorded badge cls for transition detection
HISTORY_FILE     = Path("history.json")
last_write_time  = None          # datetime of last successful ebusd write
last_target      = None          # last TargetTempHc value written (°C)
sse_clients      = []            # list of queue.Queue, one per connected browser

# ── State machine ──────────────────────────────────────────────────────────────
controller = HeatPumpController()

# ── Ebus read cache ────────────────────────────────────────────────────────────
# These are written by _refresh_ebus_reads() (outside state_lock) and read by
# _control_tick() (inside state_lock). Since Python's GIL makes individual
# attribute reads/writes atomic for simple types, this is safe without locking —
# worst case we use a value that's one tick stale.
ebus_flow_temp         = None    # hmu/RunDataFlowTemp (°C) — floor circuit flow temp
ebus_compressor_speed  = None    # hmu/RunDataCompressorSpeed (%) — 0 means idle
ebus_valve             = None    # vwzio/ThreeWayValve — 'heating circuit' or 'warm water circuit'
ebus_outdoor_temp      = None    # vwzio/OutdoorTemp (°C)

# ── External data cache ────────────────────────────────────────────────────────
weather_cache    = None   # dict: {icon, desc, fetched_at}

# Shared asyncio loop
_loop            = None


# ── External data helpers ──────────────────────────────────────────────────────

WMO_ICONS = {
    0:'☀️',  1:'🌤️', 2:'⛅',  3:'☁️',
    45:'🌫️', 48:'🌫️',
    51:'🌦️', 53:'🌦️', 55:'🌧️',
    61:'🌧️', 63:'🌧️', 65:'🌧️',
    71:'🌨️', 73:'🌨️', 75:'❄️',
    80:'🌦️', 81:'🌧️', 82:'⛈️',
    95:'⛈️', 96:'⛈️', 99:'⛈️',
}
WMO_ICONS_NIGHT = {0:'🌕', 1:'🌔', 2:'🌑', 3:'☁️'}
WMO_DESC = {
    0:'Clear',        1:'Mainly clear',   2:'Partly cloudy',  3:'Overcast',
    45:'Fog',         48:'Icy fog',
    51:'Light drizzle', 53:'Drizzle',     55:'Heavy drizzle',
    61:'Light rain',  63:'Rain',          65:'Heavy rain',
    71:'Light snow',  73:'Snow',          75:'Heavy snow',
    80:'Showers',     81:'Heavy showers', 82:'Violent showers',
    95:'Thunderstorm', 96:'Thunderstorm+hail', 99:'Thunderstorm+hail',
}


def _make_badge() -> dict:
    """Badge label and CSS class for the current state. Called with state_lock held."""
    if controller.dhw_active:
        return {"label": "Loading hot water", "cls": "dhw", "active": False}
    if controller.state == "RUNNING":
        return {"label": "Heating", "cls": "heating", "active": True}
    if controller.state == "RESTING":
        return {"label": "Resting", "cls": "resting", "active": False}
    return {"label": "Idle", "cls": "idle", "active": False}


def _record_state_event(badge_cls: str) -> dict | None:
    """Append a state event if the badge class changed. Returns the new event or None."""
    global _prev_badge_cls
    if badge_cls == _prev_badge_cls:
        return None
    _prev_badge_cls = badge_cls
    event = {"ts": datetime.now().isoformat(timespec="seconds"), "state": badge_cls}
    state_events.append(event)
    return event


def _save_history():
    """Persist today's room_history and state_events to disk."""
    try:
        HISTORY_FILE.write_text(json.dumps({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "room_history": list(room_history),
            "state_events": list(state_events),
        }))
    except Exception as e:
        print(f"[therminus] history save error: {e}")


def _load_history():
    """Load today's history from disk on startup. Silently ignores missing/stale file."""
    global _prev_badge_cls
    try:
        data = json.loads(HISTORY_FILE.read_text())
        if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return
        for p in data.get("room_history", []):
            room_history.append(p)
        for e in data.get("state_events", []):
            state_events.append(e)
        if state_events:
            _prev_badge_cls = state_events[-1]["state"]
        print(f"[therminus] loaded history: {len(room_history)} temp points, {len(state_events)} state events")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[therminus] history load error: {e}")


def _make_status_sentence() -> str:
    """Plain-language explanation of the current state. Called with state_lock held."""
    if current_temp is None:
        return "Waking up, waiting for the first temperature reading."

    dhw_prefix = "Loading hot water. " if controller.dhw_active else ""
    elapsed    = (datetime.now() - controller.state_since).total_seconds()
    diff       = current_temp - controller.setpoint
    band       = controller.band

    if controller.state == "RESTING":
        rest_remaining = max(0, controller.t_min_rest - elapsed)
        if diff > band:
            return dhw_prefix + "Giving the floor a rest, it's warm enough!"
        elif diff < -band:
            return dhw_prefix + f"I know it's getting colder, but resting for {rest_remaining/60:.0f} mins."
        else:
            return dhw_prefix + "Heating done. I'll let it rest for now."

    elif controller.state == "IDLE":
        if diff > band:
            return dhw_prefix + "Pretty warm inside! Pump won't run for now."
        elif diff < -band:
            return dhw_prefix + "Getting a bit cool. Heating will kick in shortly."
        else:
            return dhw_prefix + "Temperature is fine. Tuning where needed."

    elif controller.state == "RUNNING":
        if controller.dhw_active:
            if diff < -band:
                return "Pump is charging hot water, will start heating after."
            else:
                return "The hot water tank is being charged."
        if diff > band:
            return "Heating the floor a little."
        elif diff < -band:
            return f"Heating right now! Been at it for {elapsed/60:.0f} min."
        else:
            return "Heating a little to keep it nice and cosy."

import ssl
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

def _fetch_url(url: str, headers: dict | None = None) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            raw = resp.read()
        # Decompress if gzip (magic bytes 1f 8b) regardless of Content-Encoding header
        if raw[:2] == b'\x1f\x8b':
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        print(f'[therminus] HTTP {e.code} from {url}: {body[:500]}')
        return None
    except Exception as e:
        print(f'[therminus] fetch error {url}: {e}')
        return None

def _refresh_weather():
    """
    Fetch current weather from Open-Meteo and push to all SSE clients.

    Uses the is_day field to select night-appropriate icons for clear/partly-
    cloudy conditions (moon phases instead of sun). Only called from the
    background thread; never blocks Flask request handling.
    """
    params = urllib.parse.urlencode({
        'latitude': LATITUDE, 'longitude': LONGITUDE,
        'current': 'weather_code,temperature_2m,is_day',
        'timezone': 'Europe/Amsterdam',
    })
    d = _fetch_url(f'https://api.open-meteo.com/v1/forecast?{params}')
    if not d:
        return
    cur  = d.get('current', {})
    code = cur.get('weather_code', 0)
    is_day = cur.get('is_day', 1) == 1
    if not is_day and code <= 3:
        icon = WMO_ICONS_NIGHT.get(code, '🌙')
    else:
        icon = WMO_ICONS.get(code, '🌡️')
    desc = WMO_DESC.get(code, 'Unknown')
    outside_temp = cur.get('temperature_2m')
    # removed for now {desc} ·
    desc_str = f"{outside_temp:.1f}°" if outside_temp is not None else desc
    global weather_cache
    weather_cache = {'icon': icon, 'desc': desc_str, 'fetched_at': time.time()}
    _broadcast(json.dumps({'type': 'weather', 'icon': icon, 'desc': desc_str}))
    print(f'[therminus] weather refreshed: {icon} {desc_str}')

def _control_tick():
    """
    App-level control step. Called every CONTROL_DT seconds (and on sensor POST).

    Must be called with state_lock held. Delegates state logic to controller.tick(),
    then writes the resulting target to the pump.
    """
    if current_temp is None:
        return
    now = datetime.now()
    controller.tick(now, current_temp, ebus_compressor_speed, ebus_valve)
    _write_now(controller.target, now)


def _write_now(target: float, now: datetime):
    """
    Write a TargetTempHc value to ebusd, subject to UPDATE_INTERVAL debounce.

    This is the only place writes actually happen. The debounce prevents
    flooding ebusd — at most one write per UPDATE_INTERVAL seconds. Writes
    that fall within the interval are silently skipped; the next tick will
    write the then-current value instead.

    Must be called with state_lock held (via _control_tick).
    """
    global last_write_time, last_target
    if last_write_time and (now - last_write_time).total_seconds() < UPDATE_INTERVAL:
        return
    try:
        _run_async(_write_target(target))
        last_write_time = now
        last_target = target
    except Exception as e:
        print(f"[therminus] write error: {e}")


def _tick():
    """
    One control cycle. Called every CONTROL_DT seconds by _background_refresh.

    Deliberately structured to do ebus I/O *outside* state_lock and control
    logic *inside* it, because pyebus calls are blocking (routed through the
    asyncio loop via run_coroutine_threadsafe) and we don't want to hold the
    lock during network/bus I/O.

    Sequence:
      1. Snapshot state (under lock) to decide whether ebus reads are needed.
      2. Perform ebus reads if needed (outside lock).
      3. Run _control_tick (under lock) — state machine + ebus write.
      4. Broadcast updated state to all SSE clients (outside lock).
    """
    with state_lock:
        state_snapshot = controller.state
    # Ebus reads outside the lock (blocking I/O)
    if state_snapshot in ("RUNNING", "IDLE"):
        _refresh_ebus_reads()

    with state_lock:
        _control_tick()
        sentence = _make_status_sentence()
        badge    = _make_badge()
        event    = _record_state_event(badge["cls"])
    if event:
        _save_history()
    if current_temp is not None:
        _broadcast(json.dumps({
            "type": "update",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "room_temp": current_temp,
            "target": controller.target,
            "state": controller.state,
            "status": controller.debug_status(datetime.now()),
            "status_sentence": sentence,
            "badge": badge,
            "state_event": event,
        }))


def _background_refresh():
    """
    Long-running background thread. Runs for the lifetime of the process.

    Responsibilities:
      - Refresh weather from Open-Meteo every WEATHER_INTERVAL seconds.
      - Run one control tick every CONTROL_DT seconds.

    Sleeps 10 seconds between wakeups so that CONTROL_DT ticks land within
    ±10s of their scheduled time without busy-waiting.

    Note: the docstring mentions 'PI integral' in the original code — this
    was removed. The background thread now only drives the bang-bang control
    loop and weather refresh.
    """
    last_weather  = 0.0
    last_integral = 0.0
    while True:
        now = time.time()
        if now - last_weather >= WEATHER_INTERVAL:
            _refresh_weather()
            last_weather = now
        if now - last_integral >= CONTROL_DT:
            _tick()
            last_integral = now
        time.sleep(10)


# ── pyebus helpers ─────────────────────────────────────────────────────────────
# All ebus communication goes through pyebus (https://github.com/ebus/pyebus),
# which talks to a running ebusd daemon on EBUSD_HOST:EBUSD_PORT.
#
# pyebus is async-only, so all calls are dispatched to the dedicated asyncio
# loop (_loop) via asyncio.run_coroutine_threadsafe and awaited synchronously
# with fut.result(timeout=15). This bridges the sync control thread and the
# async pyebus API without introducing a second event loop or threading issues.
#
# Each call to _make_ebus() creates a fresh Ebus connection and loads message
# definitions from ebusd. This is slightly wasteful but keeps the code simple
# and avoids stale connection state.

async def _make_ebus():
    """Create and return a connected, msgdef-loaded Ebus instance."""
    from pyebus import Ebus
    ebus = Ebus(EBUSD_HOST, port=EBUSD_PORT)
    await ebus.async_load_msgdefs()
    return ebus


async def _write_target(target: float):
    """
    Write TargetTempHc to ebusd.

    TargetTempHc is the fake room-temperature setpoint that Therminus uses
    to control the heat pump. The pump maps this to a water temperature via
    its own heating curve (outdoor reset). By writing a value above the real
    room temperature, we tell the pump to heat; below, to not heat.
    """
    ebus = await _make_ebus()
    for msgdef in ebus.msgdefs:
        if msgdef.name.lower() == "targettemphc":
            await ebus.async_write(msgdef, target)
            print(f"[therminus] wrote TargetTempHc = {target}")
            return
    print("[therminus] WARNING: TargetTempHc msgdef not found")


async def _read_ebus_values():
    """
    Read four telemetry values from ebusd in a single session.

    Returns a dict with keys: flow_temp, compressor_speed, valve, outdoor_temp.
    Missing keys mean the msgdef was not found or the read failed.

    pyebus async_read returns a Msg object; the actual value is in msg.values[0].
    The valve is a string enum; all other values are floats.
    """
    ebus = await _make_ebus()
    result = {}
    targets = {
        "rundataflowtemp":       "flow_temp",
        "rundatacompressorspeed":"compressor_speed",
        "threewayvalve":         "valve",   # vwzio/ThreeWayValve
        "outdoortemp":           "outdoor_temp",  # vwzio/OutdoorTemp
    }
    for msgdef in ebus.msgdefs:
        key = targets.get(msgdef.name.lower())
        if key:
            msg = await ebus.async_read(msgdef)
            if msg is not None:
                try:
                    raw = msg.values[0] if hasattr(msg, 'values') else msg
                    # valve is a string, others are numeric
                    if key == "valve":
                        result[key] = str(raw).strip().lower()
                    else:
                        result[key] = float(raw)
                except (TypeError, ValueError, IndexError) as e:
                    print(f"[therminus] ebus parse error for {msgdef.name}: {e}  raw={msg!r}")
    return result


def _refresh_ebus_reads():
    """
    Synchronously read ebus telemetry and update the global cache.

    Called outside state_lock to avoid holding the lock during blocking I/O.
    The cached values are safe to read inside the lock on the next tick.
    """
    global ebus_flow_temp, ebus_compressor_speed, ebus_valve, ebus_outdoor_temp
    try:
        vals = _run_async(_read_ebus_values())
        ebus_flow_temp        = vals.get("flow_temp")
        ebus_compressor_speed = vals.get("compressor_speed")
        ebus_valve            = vals.get("valve")
        ebus_outdoor_temp     = vals.get("outdoor_temp")
        print(f"[therminus] ebus: flow={ebus_flow_temp}°C  comp={ebus_compressor_speed}%  "
              f"valve={ebus_valve}  outdoor={ebus_outdoor_temp}°C")
    except Exception as e:
        print(f"[therminus] ebus read error: {e}")


def _run_async(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=15)


def _start_async_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


# ── Control logic ──────────────────────────────────────────────────────────────


# ── SSE broadcast ──────────────────────────────────────────────────────────────
def _broadcast(payload: str):
    """
    Push a JSON string to all connected SSE clients.

    Each client has a Queue (maxsize=50). If a queue is full (client too slow)
    or the client has disconnected, it is silently removed. This ensures a
    slow or disconnected client never blocks the control thread.
    """
    dead = []
    for q in sse_clients:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            sse_clients.remove(q)
        except ValueError:
            pass


# ── Flask routes ───────────────────────────────────────────────────────────────
# POST /roomtemp  — receives room temperature from an external sensor.
#                   Accepts form data or JSON with a 'current' field.
#                   Handles comma decimals and unit suffixes ("21,4 °C").
# GET  /          — serves the single-page UI.
# GET  /api/stream — SSE endpoint; browsers connect here and receive all updates.
# GET  /api/state  — JSON snapshot of current state (for debugging/integration).
# GET  /api/weather — JSON of latest cached weather data.
@app.route("/")
def index():
    return render_template("interface.html", update_interval=UPDATE_INTERVAL)


@app.route("/roomtemp", methods=["POST"])
def post_roomtemp():
    global current_temp
    raw = request.form.get("current") or (request.json or {}).get("current")
    if raw is None:
        return jsonify({"error": "missing 'current' param"}), 400

    # Normalise: strip unit suffix, whitespace, replace comma decimal separator
    try:
        cleaned = re.sub(r'[^\d,\.\-]', '', str(raw)).replace(',', '.')
        value = float(cleaned)
    except ValueError:
        return jsonify({"error": "invalid value"}), 400

    if not (5.0 <= value <= 35.0):
        return jsonify({"error": f"value {value} out of bounds [5, 35]"}), 400

    ts = datetime.now().isoformat(timespec="seconds")

    # Update temp and snapshot state — same split-lock pattern as _tick() so
    # we can do a blocking ebus read outside the lock before running control.
    with state_lock:
        current_temp = value
        room_history.append({"ts": ts, "value": value})
        state_snapshot = controller.state
    # Ebus reads outside the lock (blocking I/O)
    if state_snapshot in ("RUNNING", "IDLE"):
        _refresh_ebus_reads()

    with state_lock:
        _control_tick()
        sentence = _make_status_sentence()
        badge    = _make_badge()
        event    = _record_state_event(badge["cls"])

    _save_history()

    _broadcast(json.dumps({
        "type": "update",
        "ts": ts,
        "room_temp": value,
        "target": controller.target,
        "state": controller.state,
        "status": controller.debug_status(datetime.now()),
        "status_sentence": sentence,
        "badge": badge,
        "state_event": event,
    }))

    return jsonify({"ok": True, "ts": ts, "target": controller.target, "state": controller.state})


@app.route("/api/weather")
def api_weather():
    return jsonify(weather_cache or {})


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "room_temp": current_temp,
            "target": last_target,
            "status": controller.debug_status(datetime.now()),
            "history": list(room_history),
            "last_write": last_write_time.isoformat() if last_write_time else None,
        })


@app.route("/api/stream")
def api_stream():
    import queue
    q = queue.Queue(maxsize=50)
    sse_clients.append(q)

    def generate():
        with state_lock:
            snap = {
                "type": "snapshot",
                "room_temp": current_temp,
                "target": controller.target,
                "status": controller.debug_status(datetime.now()),
                "state": controller.state,
                "status_sentence": _make_status_sentence(),
                "badge": _make_badge(),
                "history": list(room_history),
                "state_events": list(state_events),
                "weather": weather_cache,
            }
        yield f"data: {json.dumps(snap)}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            try:
                sse_clients.remove(q)
            except ValueError:
                pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Therminus heat pump controller")
    parser.add_argument(
        "--initial-rest", type=int, default=None, metavar="MINUTES",
        help="Override the initial resting period in minutes (default: 60). "
             "Use 0 to start immediately."
    )
    args = parser.parse_args()

    if args.initial_rest is not None:
        controller.t_min_rest  = args.initial_rest * 60
        controller.state_since = datetime.now() - timedelta(seconds=controller.t_min_rest)
        print(f"[therminus] initial rest overridden: {args.initial_rest} min "
              f"({'immediate start eligible' if args.initial_rest == 0 else 'timer already elapsed'})")

    _load_history()
    threading.Thread(target=_start_async_loop, daemon=True).start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
