"""
Therminus Controller
Receives room temperature, computes a corrected TargetTempHc and writes it
to ebusd via pyebus. Presents a mobile-first UI.

Formula:  PI controller — nudge = Kp*error + Ki*integral
Update:   every CONTROL_DT seconds via background thread.
Port:     6790
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
from flask import Flask, Response, render_template_string, jsonify, request

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
EBUSD_HOST       = "127.0.0.1"
EBUSD_PORT       = 8888
SETPOINT         = 21.0          # °C — desired room temperature
UPDATE_INTERVAL  = 60            # seconds — write to ebusd at most once per minute
HISTORY_POINTS   = 1440          # room temp history points to keep

# ── Proportional control ───────────────────────────────────────────────────────
# target = clamp(SETPOINT + Kp * error, TARGET_MIN, TARGET_MAX)
# Standard heat pump room compensation — no integral.
KP               = 1.0
TARGET_MIN       = 16.0          # °C — never send lower than this
TARGET_MAX       = 24.0          # °C — never send higher than this
CONTROL_DT       = 60            # seconds — control tick rate

# ── Bang-bang config ───────────────────────────────────────────────────────────
BAND             = 0.3           # °C — deadband around setpoint
T_MIN_REST_SHORT = 30 * 60       # seconds — rest after room-temp stop
T_MIN_REST_LONG  = 60 * 60       # seconds — rest after compressor-off stop
IDLE_TEMP        = 15.0          # °C — target sent to pump while resting

# ── WARMING state config ───────────────────────────────────────────────────────
# Entered from RESTING when floor is cold, rested long enough, outdoor cold enough.
FLOOR_COMFORT_TEMP   = 25.0      # °C — flow temp below which floor feels cold
FLOOR_TARGET_TEMP    = 28.0      # °C — water target written during WARMING
WARMING_OUTDOOR_MAX  = 17.0      # °C — only enter WARMING when colder than this outside
VALVE_HEATING        = 'heating circuit'  # three-way valve value for heating mode

# ── External API config ────────────────────────────────────────────────────────
LATITUDE         = "52.3"
LONGITUDE        = "4.98"
WEATHER_INTERVAL = 30 * 60      # seconds between Open-Meteo refreshes

# ── Shared state ──────────────────────────────────────────────────────────────
state_lock       = threading.Lock()
current_temp     = None          # latest room temp received
room_history     = deque(maxlen=HISTORY_POINTS)  # {ts, value} for chart
last_write_time  = None          # datetime of last ebusd write
last_target      = None          # last target value written
last_status      = "Waiting for room temperature…"
sse_clients      = []

# ── State machine ──────────────────────────────────────────────────────────────
pump_state       = "RESTING"     # "RUNNING", "WARMING", or "RESTING"
state_since      = datetime.now()
t_min_rest       = T_MIN_REST_SHORT  # current rest duration (SHORT or LONG)

# ── Ebus read cache ────────────────────────────────────────────────────────────
ebus_flow_temp        = None     # hmu/RunDataFlowTemp
ebus_compressor_speed = None     # hmu/RunDataCompressorSpeed
ebus_valve            = None     # vwzio/ThreeWayValve — current value
ebus_valve_was_heating = False   # True if valve was on heating circuit while compressor ran
ebus_outdoor_temp     = None     # vwzio/OutdoorTemp

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

def _make_status_sentence() -> str:
    """Plain-language explanation of the current state. Called with state_lock held."""
    if current_temp is None:
        return "Waking up — waiting for the first temperature reading."

    elapsed = (datetime.now() - state_since).total_seconds()
    diff    = current_temp - SETPOINT

    if pump_state == "RESTING":
        rest_remaining = max(0, t_min_rest - elapsed)
        if diff > BAND:
            if rest_remaining > 0:
                return (f"Nice and cosy in here. "
                        f"Taking a well-deserved break for at least {rest_remaining/60:.0f} more min.")
            return "The room is lovely and warm. Heating is off and happy to stay that way."
        elif diff < -BAND:
            if rest_remaining > 0:
                return (f"It's cooling down a little. Sitting tight for {rest_remaining/60:.0f} more min, "
                        f"then we'll get things going again.")
            return "Getting a bit cool. Heating will kick in on the next cycle."
        else:
            if rest_remaining > 0:
                return f"Right where we want it. Having a rest for at least {rest_remaining/60:.0f} more min."
            return "Temperature is spot on. No need to do anything just now."

    elif pump_state == "RUNNING":
        if diff > BAND:
            return "Lovely and warm now. Wrapping up and heading to rest soon."
        elif diff < -BAND:
            return f"Working on it. Been at it for {elapsed/60:.0f} min."
        else:
            return (f"Nearly there, just making sure the warmth settles in properly. "
                    f"{elapsed/60:.0f} min in.")

    else:  # WARMING
        return f"The floor was going cold, so giving it a gentle top-up. Been running for {elapsed/60:.0f} min."

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
    global weather_cache
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
    weather_cache = {'icon': icon, 'desc': desc_str, 'fetched_at': time.time()}
    _broadcast(json.dumps({'type': 'weather', 'icon': icon, 'desc': desc_str}))
    print(f'[therminus] weather refreshed: {icon} {desc_str}')

def _control_tick():
    """
    Three-state machine: RESTING / RUNNING / WARMING.
    Must be called with state_lock held.
    Ebus reads happen outside the lock in _tick before this is called.
    """
    global pump_state, state_since, t_min_rest, last_write_time, last_target, last_status

    if current_temp is None:
        return None

    now     = datetime.now()
    elapsed = (now - state_since).total_seconds()
    error   = SETPOINT - current_temp

    # ── RESTING ───────────────────────────────────────────────────────────────
    if pump_state == "RESTING":
        if elapsed >= t_min_rest:
            # Normal trigger: room has cooled below band
            if current_temp < (SETPOINT - BAND):
                pump_state  = "RUNNING"
                state_since = now
                print(f"[therminus] → RUNNING  room={current_temp:.1f}  rested={elapsed/60:.0f}min")

            # WARMING trigger: room at/below setpoint, floor cold, outdoor cold, compressor idle
            elif (current_temp <= (SETPOINT + BAND)
                    and ebus_flow_temp is not None
                    and ebus_flow_temp < FLOOR_COMFORT_TEMP
                    and ebus_outdoor_temp is not None
                    and ebus_outdoor_temp < WARMING_OUTDOOR_MAX
                    and (ebus_compressor_speed is None or ebus_compressor_speed == 0)):
                pump_state  = "WARMING"
                state_since = now
                print(f"[therminus] → WARMING  flow={ebus_flow_temp}°C  "
                      f"outdoor={ebus_outdoor_temp}°C")

        if pump_state == "RESTING":
            _write_now(IDLE_TEMP, now)
            last_status = f"RESTING  room={current_temp:.1f}°C  rested={elapsed/60:.0f}/{t_min_rest/60:.0f}min"
            return {"target": IDLE_TEMP, "wrote": True, "state": pump_state}

    # ── RUNNING ───────────────────────────────────────────────────────────────
    if pump_state == "RUNNING":
        compressor_stopped = (ebus_compressor_speed is not None
                              and ebus_compressor_speed == 0
                              and ebus_valve_was_heating)
        room_warm = current_temp > (SETPOINT + BAND)

        if room_warm or compressor_stopped:
            pump_state  = "RESTING"
            state_since = now
            t_min_rest  = T_MIN_REST_LONG if compressor_stopped else T_MIN_REST_SHORT
            reason      = "compressor stopped" if compressor_stopped else "room warm"
            print(f"[therminus] → RESTING ({reason})  room={current_temp:.1f}  "
                  f"rest={t_min_rest/60:.0f}min")
            _write_now(IDLE_TEMP, now)
            last_status = f"→ RESTING ({reason})  room={current_temp:.1f}°C"
            return {"target": IDLE_TEMP, "wrote": True, "state": pump_state}

        # Stay running — pure proportional control
        target = round(max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)
        _write_now(target, now)
        last_status = (f"RUNNING  room={current_temp:.1f}°C  err={error:+.2f}  "
                       f"→ {target}°C  ran={elapsed/60:.0f}min")
        return {"target": target, "wrote": True, "state": pump_state}

    # ── WARMING ───────────────────────────────────────────────────────────────
    if pump_state == "WARMING":
        compressor_stopped = (ebus_compressor_speed is not None
                              and ebus_compressor_speed == 0
                              and ebus_valve_was_heating)
        floor_warm = (ebus_flow_temp is not None
                      and ebus_flow_temp >= FLOOR_COMFORT_TEMP)

        if compressor_stopped or floor_warm:
            pump_state  = "RESTING"
            state_since = now
            t_min_rest  = T_MIN_REST_LONG if compressor_stopped else T_MIN_REST_SHORT
            reason      = "compressor stopped" if compressor_stopped else "floor warm"
            print(f"[therminus] → RESTING ({reason})  flow={ebus_flow_temp}°C  "
                  f"rest={t_min_rest/60:.0f}min")
            _write_now(IDLE_TEMP, now)
            last_status = f"→ RESTING ({reason})  flow={ebus_flow_temp}°C"
            return {"target": IDLE_TEMP, "wrote": True, "state": pump_state}

        # Stay warming — fixed floor target, no PI
        _write_now(FLOOR_TARGET_TEMP, now)
        last_status = (f"WARMING  flow={ebus_flow_temp}°C → {FLOOR_TARGET_TEMP}°C  "
                       f"ran={elapsed/60:.0f}min")
        return {"target": FLOOR_TARGET_TEMP, "wrote": True, "state": pump_state}


def _write_now(target: float, now: datetime):
    """Write target to ebusd unconditionally, respecting UPDATE_INTERVAL debounce."""
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
    """Called every CONTROL_DT seconds from the background thread."""
    with state_lock:
        state_snapshot = pump_state
        elapsed = (datetime.now() - state_since).total_seconds()
        needs_floor_check = (state_snapshot == "RESTING"
                             and current_temp is not None
                             and elapsed >= t_min_rest
                             and current_temp <= (SETPOINT + BAND))

    # Ebus reads outside the lock (blocking I/O)
    if state_snapshot in ("RUNNING", "WARMING"):
        _refresh_ebus_reads(active_run=True)
    elif needs_floor_check:
        _refresh_ebus_reads(active_run=False)

    with state_lock:
        result = _control_tick()
        sentence = _make_status_sentence()
        _state = pump_state
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
        }))


def _background_refresh():
    """Periodically refresh weather and step the PI integral."""
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
async def _make_ebus():
    from pyebus import Ebus
    ebus = Ebus(EBUSD_HOST, port=EBUSD_PORT)
    await ebus.async_load_msgdefs()
    return ebus


async def _write_target(target: float):
    """Write TargetTempHc to ebusd."""
    ebus = await _make_ebus()
    for msgdef in ebus.msgdefs:
        if msgdef.name.lower() == "targettemphc":
            await ebus.async_write(msgdef, target)
            print(f"[therminus] wrote TargetTempHc = {target}")
            return
    print("[therminus] WARNING: TargetTempHc msgdef not found")


async def _read_ebus_values():
    """Read flow temp, compressor speed, three-way valve and outdoor temp from ebusd."""
    ebus = await _make_ebus()
    result = {}
    targets = {
        "rundataflowtemp":       "flow_temp",
        "rundatacompressorspeed":"compressor_speed",
        "threewayvvalve":        "valve",   # vwzio/ThreeWayValve
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
    Sync wrapper — update global ebus read cache.
    active_run=True: also update ebus_valve_was_heating while compressor is running.
    """
    global ebus_flow_temp, ebus_compressor_speed, ebus_valve, ebus_valve_was_heating, ebus_outdoor_temp
    try:
        vals = _run_async(_read_ebus_values())
        ebus_flow_temp        = vals.get("flow_temp")
        ebus_compressor_speed = vals.get("compressor_speed")
        ebus_valve            = vals.get("valve")
        ebus_outdoor_temp     = vals.get("outdoor_temp")
        # While compressor is running, track whether valve was on heating circuit
        if active_run and ebus_compressor_speed is not None and ebus_compressor_speed > 0:
            ebus_valve_was_heating = (ebus_valve == VALVE_HEATING.lower())
        print(f"[therminus] ebus: flow={ebus_flow_temp}°C  comp={ebus_compressor_speed}%  "
              f"valve={ebus_valve}  outdoor={ebus_outdoor_temp}°C  "
              f"was_heating={ebus_valve_was_heating}")
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
    Called on sensor POST — just updates current_temp in state.
    The control tick runs independently every CONTROL_DT seconds.
    Lock must be held by caller.
    """
    # Sensor arrival: run a control tick immediately so the pump
    # gets fresh data without waiting up to 60s.
    result = _control_tick()
    return result or {"target": last_target, "wrote": False, "state": pump_state}


# ── SSE broadcast ──────────────────────────────────────────────────────────────
def _broadcast(payload: str):
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
@app.route("/")
def index():
    return render_template_string(UI_HTML, update_interval=UPDATE_INTERVAL)


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
    with state_lock:
        current_temp = value
        room_history.append({"ts": ts, "value": value})
        result = _apply_control(value)
        sentence = _make_status_sentence()

    _broadcast(json.dumps({
        "type": "update",
        "ts": ts,
        "room_temp": value,
        "target": result["target"],
        "wrote": result["wrote"],
        "state": result.get("state", pump_state),
        "status": last_status,
        "status_sentence": sentence,
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
                "history": list(room_history),
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


# ── UI ─────────────────────────────────────────────────────────────────────────
UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Therminus">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0f1117">
<title>Therminus</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:         #0c0e14;
    --surface:    #13161f;
    --card:       #191d2a;
    --border:     #252b3d;
    --accent:     #60cdff;
    --accent2:    #6ee7a0;
    --warn:       #ffc14d;
    --red:        #ff6b6b;
    --text:       #f0f2f8;
    --text-dim:   #6b7590;
    --back-bg:    #10121a;
    --heating:    #ff8c42;
    --resting:    #6ee7a0;
    --mono:       'DM Mono', monospace;
    --sans:       'DM Sans', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  html, body {
    background: var(--bg);
    font-family: var(--sans);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
    height: 100%;
  }

  body {
    margin: 0;
  }

  .widget-scene {
    width: 100%;
    max-width: 420px;
    perspective: 1200px;
    margin: 0 auto;
  }

  .widget-flipper {
    width: 100%;
    min-height: 100dvh;
    position: relative;
    transform-style: preserve-3d;
    transition: transform 0.65s cubic-bezier(0.4, 0.2, 0.2, 1);
  }

  .widget-flipper.is-flipped {
    transform: rotateY(180deg);
  }

  .widget-face {
    width: 100%;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: env(safe-area-inset-top, 16px) 0 env(safe-area-inset-bottom, 16px);
    backface-visibility: hidden;
    -webkit-backface-visibility: hidden;
  }

  /* Front face: normal flow */
  .widget-front {
    position: relative;
  }

  /* Back face: absolutely positioned, rotated, independently centered */
  .widget-back {
    position: absolute;
    top: 0; left: 0;
    transform: rotateY(180deg);
    background: var(--back-bg);
  }

  /* ── ⓘ flip button — bottom-right corner of front ── */
  .flip-btn {
    position: absolute;
    bottom: 18px;
    right: 20px;
    width: 26px; height: 26px;
    border-radius: 50%;
    border: 1.5px solid var(--border);
    background: rgba(30,35,51,.85);
    backdrop-filter: blur(4px);
    color: var(--text-dim);
    font-size: 13px;
    font-family: var(--sans);
    font-weight: 500;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: border-color .2s, color .2s, background .2s;
    z-index: 10;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
    line-height: 1;
  }
  .flip-btn:hover, .flip-btn:active {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(79,195,247,.08);
  }
  .is-flipped .flip-btn {
    visibility: hidden;
    pointer-events: none;
  }

  /* ── Done button on back ── */
  .done-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 16px;
    border-radius: 99px;
    border: 1.5px solid var(--accent);
    background: rgba(79,195,247,.10);
    color: var(--accent);
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background .2s;
    -webkit-tap-highlight-color: transparent;
  }
  .done-btn:hover, .done-btn:active { background: rgba(79,195,247,.2); }

  /* ─────────────────────────────────────────
     FRONT FACE styles
  ───────────────────────────────────────── */

  /* Single front card — contains everything */
  .front-card {
    width: calc(100% - 32px);
    margin: 16px 16px 16px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 26px;
    padding: 24px 26px 28px;
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .front-card::before {
    content: '';
    position: absolute; top: -60px; right: -60px;
    width: 200px; height: 200px;
    background: radial-gradient(circle, rgba(79,195,247,.06) 0%, transparent 70%);
    pointer-events: none;
  }

  /* Row 1: date/time left, weather right */
  .card-toprow {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 20px;
  }
  .datetime-block { display: flex; flex-direction: column; gap: 2px; }
  .day-label {
    font-size: 11px; font-weight: 500;
    letter-spacing: .14em; text-transform: uppercase;
    color: var(--text-dim);
  }
  .time-label {
    font-family: var(--mono);
    font-size: 30px; font-weight: 300; letter-spacing: -.5px;
    color: var(--text); line-height: 1;
  }
  .weather-block {
    display: flex; flex-direction: column; align-items: flex-end; gap: 3px;
  }
  .weather-icon { font-size: 38px; line-height: 1; }
  .weather-desc {
    font-size: 11px; color: var(--text-dim);
    font-weight: 400; letter-spacing: .04em; text-align: right;
  }

  /* Row 2: big temperature */
  .temp-row { margin-bottom: 14px; }
  .temp-big {
    font-family: var(--mono); font-size: 86px; font-weight: 300;
    line-height: 1; color: var(--accent); letter-spacing: -3px;
  }
  .temp-unit { font-size: 26px; font-weight: 300; color: var(--text-dim); }

  /* Row 3: status sentence */
  .status-sentence {
    font-size: 14px;
    font-weight: 300;
    color: var(--text-dim);
    line-height: 1.55;
    letter-spacing: .01em;
  }

  /* ─────────────────────────────────────────
     BACK FACE styles
  ───────────────────────────────────────── */
  .back-header {
    width: 100%;
    padding: 20px 20px 14px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .back-title {
    font-size: 11px; font-weight: 500;
    letter-spacing: .14em; text-transform: uppercase;
    color: var(--text-dim);
  }

  /* Status pill */
  .status-pill {
    width: calc(100% - 32px);
    margin: 0 16px 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 10px 14px;
    display: flex; align-items: center; gap: 10px; min-height: 42px;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--text-dim); flex-shrink: 0; transition: background .4s;
  }
  .status-dot.ok   { background: var(--accent2); box-shadow: 0 0 6px var(--accent2); }
  .status-dot.warn { background: var(--warn);    box-shadow: 0 0 6px var(--warn); }
  .status-dot.err  { background: var(--red);     box-shadow: 0 0 6px var(--red); }
  .status-text {
    font-family: var(--mono); font-size: 11px; color: var(--text-dim);
    flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  /* Info chips */
  .chip-grid {
    width: calc(100% - 32px);
    margin: 0 16px 14px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  }
  .chip {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 12px 14px;
    display: flex; flex-direction: column; gap: 4px;
  }
  .chip-label {
    font-size: 10px; font-weight: 500;
    letter-spacing: .1em; text-transform: uppercase; color: var(--text-dim);
  }
  .chip-val { font-family: var(--mono); font-size: 18px; font-weight: 400; color: var(--text); }
  .chip-val.ok    { color: var(--accent2); }
  .chip-val.warn  { color: var(--warn); }
  .chip-val.err   { color: var(--red); }

  /* Chart */
  .chart-card {
    width: calc(100% - 32px);
    margin: 0 16px 20px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 14px 14px 10px;
  }
  .chart-title {
    font-size: 10px; font-weight: 500;
    letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-dim); margin-bottom: 10px;
  }
  canvas { display: block; width: 100% !important; }
</style>
</head>
<body>

<div class="widget-scene">
  <div class="widget-flipper" id="flipper">

    <!-- ══════════════ FRONT FACE ══════════════ -->
    <div class="widget-face widget-front">

      <div class="front-card">

        <!-- Row 1: date/time + weather -->
        <div class="card-toprow">
          <div class="datetime-block">
            <div class="day-label" id="day-label">—</div>
            <div class="time-label" id="time-label">--:--</div>
          </div>
          <div class="weather-block">
            <div class="weather-icon" id="weather-icon">⋯</div>
            <div class="weather-desc" id="weather-desc">loading…</div>
          </div>
        </div>

        <!-- Row 2: big temperature + action badge -->
        <div class="temp-row">
          <span class="temp-big" id="lcd-temp">--.-</span>
          <span class="temp-unit">°C</span>
        </div>
        <div class="action-badge" id="action-badge">
          <span class="badge-dot"></span>
          <span id="action-label">—</span>
        </div>

        <!-- Row 3: status sentence -->
        <div class="status-sentence" id="status-sentence">—</div>

        <!-- ⓘ flip button -->
        <button class="flip-btn" id="btn-flip-to-back" title="System info">ⓘ</button>

      </div>

    </div><!-- /widget-front -->

    <!-- ══════════════ BACK FACE ══════════════ -->
    <div class="widget-face widget-back" id="back-face">

      <div class="back-header">
        <div class="back-title">System Info</div>
        <button class="done-btn" id="btn-flip-to-front">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M9 3L3 9M3 3l6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          Done
        </button>
      </div>

      <div class="status-pill">
        <div class="status-dot" id="status-dot"></div>
        <span class="status-text" id="status-text">Connecting…</span>
      </div>

      <div class="chip-grid">
        <div class="chip">
          <div class="chip-label">State</div>
          <div class="chip-val" id="info-state">—</div>
        </div>
        <div class="chip">
          <div class="chip-label">Last write</div>
          <div class="chip-val" id="info-write">—</div>
        </div>
        <div class="chip">
          <div class="chip-label">Next write</div>
          <div class="chip-val" id="info-next">—</div>
        </div>
      </div>

      <div class="chart-card">
        <div class="chart-title">Room Temperature — Today</div>
        <canvas id="chart" height="120"></canvas>
      </div>

    </div><!-- /widget-back -->

  </div><!-- /widget-flipper -->
</div><!-- /widget-scene -->

<script>
// ── Flip logic ────────────────────────────────────────────────────────────────
const flipper   = document.getElementById('flipper');
const backFace  = document.getElementById('back-face');

function flip(showBack) {
  flipper.classList.toggle('is-flipped', showBack);
  // Sync back-face height to front so the scene doesn't collapse
  if (showBack) {
    // let it render first
    requestAnimationFrame(() => {
      backFace.style.minHeight = flipper.offsetHeight + 'px';
    });
  }
}

document.getElementById('btn-flip-to-back').addEventListener('click', () => flip(true));
document.getElementById('btn-flip-to-front').addEventListener('click', () => flip(false));

// ── Clock + date ──────────────────────────────────────────────────────────────
const DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function updateClock() {
  const now = new Date();
  document.getElementById('day-label').textContent = DAYS[now.getDay()];
  document.getElementById('time-label').textContent =
    now.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
}
updateClock();
setInterval(updateClock, 10000);

// ── Weather + status sentence: applied from SSE ───────────────────────────────
function applyWeather(w) {
  if (!w) return;
  document.getElementById('weather-icon').textContent = w.icon ?? '🌡️';
  document.getElementById('weather-desc').textContent = w.desc ?? '';
}
function applyStatusSentence(msg) {
  if (msg.status_sentence != null)
    document.getElementById('status-sentence').textContent = msg.status_sentence;
}

// ── Chart ─────────────────────────────────────────────────────────────────────
const dayStart = new Date(); dayStart.setHours(0,0,0,0);
const dayEnd   = new Date(); dayEnd.setHours(23,59,59,999);

const chart = new Chart(document.getElementById('chart').getContext('2d'), {
  type: 'line',
  data: {
    datasets: [{
      data: [],
      borderColor: '#4fc3f7',
      backgroundColor: 'rgba(79,195,247,.06)',
      borderWidth: 1.5, pointRadius: 0, tension: 0.4, fill: true,
      spanGaps: 10 * 60 * 1000,
    }]
  },
  options: {
    animation: false, responsive: true, maintainAspectRatio: true,
    plugins: { legend: { display: false } },
    scales: {
      x: {
        type: 'time', min: dayStart, max: dayEnd,
        time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } },
        ticks: { color: '#7a8099', font: { family: "'DM Mono'", size: 10 }, maxTicksLimit: 8, maxRotation: 0 },
        grid: { color: 'rgba(255,255,255,.04)' }, border: { color: 'transparent' }
      },
      y: {
        ticks: { color: '#7a8099', font: { family: "'DM Mono'", size: 10 } },
        grid: { color: 'rgba(255,255,255,.04)' }, border: { color: 'transparent' }
      }
    }
  }
});

// ── State update ──────────────────────────────────────────────────────────────
let lastWriteTime = null;

function applyState(msg) {
  if (msg.room_temp != null)
    document.getElementById('lcd-temp').textContent = msg.room_temp.toFixed(1);

  if (msg.state) {
    const badge   = document.getElementById('action-badge');
    const label   = document.getElementById('action-label');
    const isRunning = msg.state === 'RUNNING';
    const isWarming = msg.state === 'WARMING';
    const isActive  = isRunning || isWarming;
    badge.className = 'action-badge ' + (isWarming ? 'heating' : isRunning ? 'heating' : 'resting');
    label.textContent = isWarming ? 'Warming the floor' : (isRunning ? 'Heating' : 'Resting');
    const el = document.getElementById('info-state');
    if (el) {
      el.textContent = msg.state;
      el.className = 'chip-val ' + (isActive ? 'ok' : 'warn');
    }
    document.getElementById('status-dot').className =
      'status-dot ' + (isActive ? 'ok' : '');
  }

  if (msg.status)
    document.getElementById('status-text').textContent = msg.status;

  if (msg.last_write) {
    lastWriteTime = new Date(msg.last_write.replace('T',' '));
    document.getElementById('info-write').textContent =
      lastWriteTime.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  }
}

function updateNextWrite() {
  if (!lastWriteTime) return;
  const remaining = Math.max(0, {{ update_interval }} - (Date.now() - lastWriteTime) / 1000);
  document.getElementById('info-next').textContent = remaining > 0 ? Math.ceil(remaining) + 's' : 'now';
}
setInterval(updateNextWrite, 1000);

// ── SSE ───────────────────────────────────────────────────────────────────────
const es = new EventSource('/api/stream');
es.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'snapshot') {
    for (const p of (msg.history || []))
      chart.data.datasets[0].data.push({ x: new Date(p.ts.replace('T',' ')), y: p.value });
    chart.update('none');
    applyState(msg);
    if (msg.last_write) applyState(msg);
    applyWeather(msg.weather);
    applyStatusSentence(msg);
  }
  if (msg.type === 'update') {
    chart.data.datasets[0].data.push({ x: new Date(msg.ts.replace('T',' ')), y: msg.room_temp });
    chart.update('none');
    applyState(msg);
    applyStatusSentence(msg);
    if (msg.wrote) lastWriteTime = new Date(msg.ts.replace('T',' '));
  }
  if (msg.type === 'weather') applyWeather(msg);
};
</script>
</body>
</html>
"""

# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_start_async_loop, daemon=True).start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
