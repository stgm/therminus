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

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
EBUSD_HOST       = "127.0.0.1"
EBUSD_PORT       = 8888

# The desired room temperature. This is the real setpoint the occupant wants.
# All control logic is centred around this value.
SETPOINT         = 21.0          # °C

# Minimum time between writes to ebusd. Writing more often than the sensor
# posts (every ~5 min) is fine because the background thread updates the
# target every CONTROL_DT seconds regardless of sensor arrivals.
UPDATE_INTERVAL  = 60            # seconds

# Number of room-temp readings to keep in memory for the history chart.
# At one reading per ~5 minutes, 1440 points covers roughly 5 days.
HISTORY_POINTS   = 1440

# ── Proportional control ───────────────────────────────────────────────────────
# During RUNNING, the target written to the pump is:
#   target = clamp(SETPOINT + Kp * error, TARGET_MIN, TARGET_MAX)
# where error = SETPOINT - room_temp.
#
# Kp = 1.0 means: 1°C below setpoint → request 1°C above setpoint from pump.
# This is the standard "room compensation" formula used by heat pump manufacturers.
# No integral term is used. UFH has very slow thermal dynamics, and the pump's
# own outdoor-reset heating curve already provides the long-term correction that
# an integral would otherwise add. Adding an integral caused windup problems and
# added complexity without meaningful benefit.
KP               = 1.0
TARGET_MIN       = 16.0          # °C — safety floor; never request below this
TARGET_MAX       = 24.0          # °C — safety ceiling; never request above this
CONTROL_DT       = 60            # seconds — how often the control loop runs

# ── Bang-bang config ───────────────────────────────────────────────────────────
# The outer loop is bang-bang: the pump either runs or rests. BAND defines
# the deadband around SETPOINT — we don't react to tiny fluctuations.
BAND             = 0.3           # °C — half-width of deadband

# After a run ends, the pump must rest before starting again. The rest duration
# depends on *why* the run ended:
#   - Room got warm enough (room_temp > SETPOINT + BAND): short rest.
#     The floor is still warm; a brief pause is enough.
#   - Compressor stopped on its own: long rest. The pump has decided it's done;
#     respect that decision and give the floor time to distribute heat.
T_MIN_REST_SHORT = 30 * 60       # seconds (30 min) — after room-temp stop
T_MIN_REST_LONG  = 60 * 60       # seconds (60 min) — after compressor-off stop

# During RESTING, we write this as the fake room setpoint. 15°C is far enough
# below any real room temperature that the pump will not run its compressor for
# space heating. (It may still run for domestic hot water — that's handled via
# the three-way valve guard.)
IDLE_TEMP        = 15.0          # °C

# ── WARMING state config ───────────────────────────────────────────────────────
# WARMING is a special state for maintaining floor comfort during rest periods.
# UFH floors lose heat slowly but feel cold underfoot once the flow temp drops
# below a threshold. WARMING lets the pump do a gentle top-up run without
# Therminus driving it via room temperature (the room may already be at setpoint).
#
# Entry conditions (all must be true, and rest timer must have elapsed):
#   - room_temp <= SETPOINT + BAND  (room is not already too warm)
#   - flow_temp < FLOOR_COMFORT_TEMP (floor is going cold)
#   - outdoor_temp < WARMING_OUTDOOR_MAX (only needed in cold weather)
#   - compressor_speed == 0  (pump is fully idle, not doing DHW)
#
# During WARMING we write SETPOINT as the fake room temp, giving the pump a
# modest target to aim for. The pump's own thermostat and heating curve decide
# how hard to run. We stay in WARMING until the compressor stops — the pump
# knows when the floor is warm enough.
FLOOR_COMFORT_TEMP   = 25.0      # °C — flow temp below which floor feels cold
WARMING_OUTDOOR_MAX  = 17.0      # °C — above this, floor cooling is slow enough to ignore

# The string value the three-way valve reports when in space-heating mode.
# Used to distinguish heating runs from domestic hot water (DHW) runs so we
# don't misinterpret a DHW compressor stop as the end of a heating run.
VALVE_HEATING        = 'heating circuit'
VALVE_DHW            = 'warm water circuit'

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
last_status      = "Waiting for room temperature…"  # raw debug status string (back panel)
sse_clients      = []            # list of queue.Queue, one per connected browser

# ── State machine ──────────────────────────────────────────────────────────────
pump_state       = "IDLE"     # current state: "RUNNING", "RESTING", or "IDLE"
state_since      = datetime.now()  # when the current state was entered
# t_min_rest is the minimum number of seconds we must stay in RESTING before
# the next run. It is set dynamically on each RESTING transition:
#   T_MIN_REST_LONG  after a compressor-off stop (pump decided it was done)
#   T_MIN_REST_SHORT after a room-temp stop (room got warm, may need heat again soon)
# Starts at T_MIN_REST_LONG so a cold start doesn't immediately trigger heating.
t_min_rest       = T_MIN_REST_LONG

# ── Ebus read cache ────────────────────────────────────────────────────────────
# These are written by _refresh_ebus_reads() (outside state_lock) and read by
# _control_tick() (inside state_lock). Since Python's GIL makes individual
# attribute reads/writes atomic for simple types, this is safe without locking —
# worst case we use a value that's one tick stale.
ebus_flow_temp         = None    # hmu/RunDataFlowTemp (°C) — floor circuit flow temp
ebus_compressor_speed  = None    # hmu/RunDataCompressorSpeed (%) — 0 means idle
ebus_valve             = None    # vwzio/ThreeWayValve — 'heating circuit' or 'warm water circuit'
ebus_outdoor_temp      = None    # vwzio/OutdoorTemp (°C)
# Sticky flag: True if the valve was confirmed on 'heating circuit' while the
# compressor was running during the current RUNNING or WARMING state. We use this
# instead of the current valve value because the valve may switch back to neutral
# before we detect the compressor stopping, creating a race condition.
ebus_valve_was_heating = False
ebus_dhw_active        = False   # True when compressor on AND valve = 'warm water circuit'

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
    if ebus_dhw_active:
        return {"label": "Loading hot water", "cls": "dhw", "active": False}
    if pump_state == "RUNNING":
        return {"label": "Heating", "cls": "heating", "active": True}
    if pump_state == "RESTING":
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

    dhw_prefix = "Loading hot water. " if ebus_dhw_active else ""
    elapsed = (datetime.now() - state_since).total_seconds()
    diff    = current_temp - SETPOINT

    if pump_state == "RESTING":
        rest_remaining = max(0, t_min_rest - elapsed)
        if diff > BAND:
            return dhw_prefix + f"Giving the floor a rest. Resuming checks in {rest_remaining/60:.0f} min."
        elif diff < -BAND:
            return dhw_prefix + f"It's cooling down. Sitting tight for {rest_remaining/60:.0f} more min, then heating again."
        else:
            return dhw_prefix + f"Right where we want it. Letting the floor rest for {rest_remaining/60:.0f} more min."

    elif pump_state == "IDLE":
        if diff > BAND:
            return dhw_prefix + "The room is lovely and warm. Watching and ready."
        elif diff < -BAND:
            return dhw_prefix + "Getting a bit cool. Heating will kick in shortly."
        else:
            return dhw_prefix + "Temperature is spot on. Watching conditions."

    elif pump_state == "RUNNING":
        if ebus_dhw_active:
            if diff < -BAND:
                return "Pump is loading hot water first. Heating will start right after."
            else:
                return "Pump is loading hot water first. Will check whether heating is still needed after."
        if diff > BAND:
            return "Lovely and warm now. Wrapping up and heading to rest soon."
        elif diff < -BAND:
            return f"Working on it. Been at it for {elapsed/60:.0f} min."
        else:
            return (f"Nearly there, just making sure the warmth settles in properly. "
                    f"{elapsed/60:.0f} min in.")

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
    desc_str = f"{desc} · {outside_temp:.1f}°C outside" if outside_temp is not None else desc
    global weather_cache
    weather_cache = {'icon': icon, 'desc': desc_str, 'fetched_at': time.time()}
    _broadcast(json.dumps({'type': 'weather', 'icon': icon, 'desc': desc_str}))
    print(f'[therminus] weather refreshed: {icon} {desc_str}')

def _control_tick():
    """
    The core three-state control machine. Called every CONTROL_DT seconds.

    Must be called with state_lock held. Ebus values (flow temp, compressor
    speed, valve, outdoor temp) are read outside the lock in _tick() before
    this function runs, so they are always fresh when evaluated here.

    The function may transition state and/or write a new target to the pump.
    It returns a dict with at minimum {target, wrote, state}, or None if
    current_temp is not yet known.

    See module docstring for full state transition logic.
    """
    global pump_state, state_since, t_min_rest, ebus_valve_was_heating, ebus_dhw_active, last_write_time, last_target, last_status

    if current_temp is None:
        return None

    now     = datetime.now()
    elapsed = (now - state_since).total_seconds()
    error   = SETPOINT - current_temp

    # ── Startup detection ─────────────────────────────────────────────────────
    # If we just started and the compressor is already running for heating,
    # jump straight to RUNNING so we can detect when it stops.
    if (pump_state == "IDLE"
            and last_write_time is None       # haven't written anything yet
            and ebus_compressor_speed is not None
            and ebus_compressor_speed > 0
            and ebus_valve == VALVE_HEATING.lower()):
        pump_state             = "RUNNING"
        state_since            = now
        ebus_valve_was_heating = True
        print(f"[therminus] startup: compressor already running, → RUNNING")

    elif (pump_state == "IDLE"
            and last_write_time is None
            and ebus_dhw_active):
        print(f"[therminus] startup: compressor running for DHW (hot water tank loading)")

    # ── RESTING ───────────────────────────────────────────────────────────────
    if pump_state == "RESTING":
        if elapsed >= t_min_rest:
            pump_state  = "IDLE"
            state_since = now
            print(f"[therminus] → IDLE  rested={elapsed/60:.0f}min")

        if pump_state == "RESTING":
            _write_now(IDLE_TEMP, now)
            last_status = (f"RESTING  room={current_temp:.1f}°C  rested={elapsed/60:.0f}/{t_min_rest/60:.0f}min"
                           f"  dhw={ebus_dhw_active}")
            return {"target": IDLE_TEMP, "wrote": True, "state": pump_state}

    # ── IDLE ───────────────────────────────────────────────────────────────
    if pump_state == "IDLE":
        # Normal trigger: room has cooled below band
        if current_temp < (SETPOINT - BAND):
            pump_state  = "RUNNING"
            state_since = now
            print(f"[therminus] → RUNNING  room={current_temp:.1f}")

        # Floor too cold while room is still in band
        elif (ebus_flow_temp is not None
                and ebus_flow_temp < FLOOR_COMFORT_TEMP):
            pump_state  = "RUNNING"
            state_since = now
            print(f"[therminus] → RUNNING (cold floor)  flow={ebus_flow_temp}°C  room={current_temp:.1f}")

        if pump_state == "IDLE":
            _write_now(IDLE_TEMP, now)
            last_status = (f"IDLE  room={current_temp:.1f}°C  dhw={ebus_dhw_active}")
            return {"target": IDLE_TEMP, "wrote": True, "state": pump_state}

    # ── RUNNING ───────────────────────────────────────────────────────────────
    if pump_state == "RUNNING":
        compressor_stopped = (ebus_compressor_speed is not None
                              and ebus_compressor_speed == 0
                              and ebus_valve_was_heating)
        room_warm = current_temp > (SETPOINT + BAND)

        if room_warm or compressor_stopped:
            if ebus_valve_was_heating:
                pump_state = "RESTING"
                t_min_rest = T_MIN_REST_LONG if compressor_stopped else T_MIN_REST_SHORT
                reason     = "compressor stopped" if compressor_stopped else "room warm"
                print(f"[therminus] → RESTING ({reason})  room={current_temp:.1f}  rest={t_min_rest/60:.0f}min")
                last_status = f"→ RESTING ({reason})  room={current_temp:.1f}°C"
            else:
                pump_state = "IDLE"
                reason     = "compressor stopped" if compressor_stopped else "room warm"
                print(f"[therminus] → IDLE ({reason}, no heating done)  room={current_temp:.1f}")
                last_status = f"→ IDLE ({reason}, no heating done)  room={current_temp:.1f}°C"
            state_since = now
            _write_now(IDLE_TEMP, now)
            return {"target": IDLE_TEMP, "wrote": True, "state": pump_state}

        # Stay running — pure proportional control
        target = round(max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)
        _write_now(target, now)
        last_status = (f"RUNNING  room={current_temp:.1f}°C  err={error:+.2f}  "
                       f"→ {target}°C  ran={elapsed/60:.0f}min  dhw={ebus_dhw_active}")
        return {"target": target, "wrote": True, "state": pump_state}



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
        state_snapshot = pump_state
        elapsed = (datetime.now() - state_since).total_seconds()
        needs_floor_check = (state_snapshot == "IDLE"
                             and current_temp is not None
                             and current_temp <= (SETPOINT + BAND))

    # Ebus reads outside the lock (blocking I/O)
    if state_snapshot == "RUNNING":
        _refresh_ebus_reads(active_run=True)
    elif needs_floor_check:
        _refresh_ebus_reads(active_run=False)

    with state_lock:
        result = _control_tick()
        sentence = _make_status_sentence()
        badge    = _make_badge()
        event    = _record_state_event(badge["cls"])
        _state = pump_state
    if event:
        _save_history()
    if result:
        _broadcast(json.dumps({
            "type": "update",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "room_temp": current_temp,
            "target": result["target"],
            "wrote": result["wrote"],
            "state": result["state"],
            "status": last_status,
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


def _refresh_ebus_reads(active_run: bool = False):
    """
    Synchronously read ebus telemetry and update the global cache.

    active_run=True should be passed when called during RUNNING or WARMING.
    In that case, if the compressor is currently spinning and the valve is on
    the heating circuit, ebus_valve_was_heating is set to True. This sticky
    flag persists until the next RESTING transition so that we can correctly
    identify a compressor stop as a heating stop even if the valve has already
    switched away from the heating circuit by the time we detect it.

    Called outside state_lock to avoid holding the lock during blocking I/O.
    The cached values are safe to read inside the lock on the next tick.
    """
    global ebus_flow_temp, ebus_compressor_speed, ebus_valve, ebus_valve_was_heating, ebus_outdoor_temp, ebus_dhw_active
    try:
        vals = _run_async(_read_ebus_values())
        ebus_flow_temp        = vals.get("flow_temp")
        ebus_compressor_speed = vals.get("compressor_speed")
        ebus_valve            = vals.get("valve")
        ebus_outdoor_temp     = vals.get("outdoor_temp")
        ebus_dhw_active = (
            ebus_compressor_speed is not None
            and ebus_compressor_speed > 0
            and ebus_valve == VALVE_DHW.lower()
        )
        # While compressor is running, track whether valve was on heating circuit
        if active_run and ebus_compressor_speed is not None and ebus_compressor_speed > 0:
            ebus_valve_was_heating = (ebus_valve == VALVE_HEATING.lower())
        print(f"[therminus] ebus: flow={ebus_flow_temp}°C  comp={ebus_compressor_speed}%  "
              f"valve={ebus_valve}  outdoor={ebus_outdoor_temp}°C  "
              f"was_heating={ebus_valve_was_heating}  dhw={ebus_dhw_active}")
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
def _apply_control(room_temp: float) -> dict:
    """
    Called on every sensor POST, immediately after updating current_temp.

    Runs _control_tick() right away so the pump gets the fresh reading without
    waiting up to CONTROL_DT seconds for the next background tick. This is
    particularly useful when the room crosses a band threshold — the state
    transition happens within seconds of the sensor posting, not minutes.

    Must be called with state_lock held.
    """
    # Sensor arrival: run a control tick immediately so the pump
    # gets fresh data without waiting up to 60s.
    result = _control_tick()
    return result or {"target": last_target, "wrote": False, "state": pump_state}


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
        state_snapshot = pump_state
        elapsed_snap   = (datetime.now() - state_since).total_seconds()
        needs_floor_check = (state_snapshot == "IDLE"
                             and value <= (SETPOINT + BAND))

    # Ebus reads outside the lock (blocking I/O)
    if state_snapshot == "RUNNING":
        _refresh_ebus_reads(active_run=True)
    elif needs_floor_check:
        _refresh_ebus_reads(active_run=False)

    with state_lock:
        result = _apply_control(value)
        sentence = _make_status_sentence()
        badge    = _make_badge()
        event    = _record_state_event(badge["cls"])

    _save_history()

    _broadcast(json.dumps({
        "type": "update",
        "ts": ts,
        "room_temp": value,
        "target": result["target"],
        "wrote": result["wrote"],
        "state": result.get("state", pump_state),
        "status": last_status,
        "status_sentence": sentence,
        "badge": badge,
        "state_event": event,
    }))

    return jsonify({"ok": True, "ts": ts, **result})


@app.route("/api/weather")
def api_weather():
    return jsonify(weather_cache or {})


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "room_temp": current_temp,
            "target": last_target,
            "status": last_status,
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
                "target": last_target,
                "status": last_status,
                "state": pump_state,
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
        t_min_rest  = args.initial_rest * 60
        state_since = datetime.now() - timedelta(seconds=t_min_rest)
        print(f"[therminus] initial rest overridden: {args.initial_rest} min "
              f"({'immediate start eligible' if args.initial_rest == 0 else 'timer already elapsed'})")

    _load_history()
    threading.Thread(target=_start_async_loop, daemon=True).start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
