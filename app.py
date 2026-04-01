"""
Thermostat Controller
Receives room temperature, computes a corrected TargetTempHc and writes it
to ebusd via pyebus. Presents a retro Mac-style thermostat UI.

Formula:  target = 20 + (20 - current_room_temp)
Failsafe: target clamped to ±2°C of the 3-hour rolling average of past targets.
Update:   at most once per 10 minutes.
Port:     6790
"""
import asyncio
import threading
import json
import re
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, Response, render_template_string, jsonify, request

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
EBUSD_HOST       = "127.0.0.1"
EBUSD_PORT       = 8888
SETPOINT         = 20.0          # °C — mirror point for the formula
UPDATE_INTERVAL  = 10 * 60      # seconds between writes to ebusd
FAILSAFE_WINDOW  = 3 * 60 * 60  # seconds — rolling window for avg (3h)
FAILSAFE_DELTA   = 2.0          # °C — max deviation from rolling avg
HISTORY_POINTS   = 1440         # room temp history points to keep

# Signed-square penalty: pushes target further from setpoint the larger the error.
# error > 0 (cold room) → target goes UP extra to heat faster
# error < 0 (warm room) → target goes DOWN extra to drain the pump's water-temp integral
# penalty = α * error * |error|
# At ±1°C error → ±0.10°C extra (α=0.10)
# At ±2°C error → ±0.40°C extra
# At ±3°C error → ±0.90°C extra
# Set to 0.0 to disable.
PENALTY_ALPHA    = 0.10

# ── Shared state ──────────────────────────────────────────────────────────────
state_lock       = threading.Lock()
current_temp     = None          # latest room temp received
target_history   = deque()       # deque of (datetime, target_value) tuples
room_history     = deque(maxlen=HISTORY_POINTS)  # {ts, value} for chart
last_write_time  = None          # datetime of last ebusd write
last_target      = None          # last target value written
last_status      = "Waiting for room temperature…"
sse_clients      = []

# Shared asyncio loop
_loop            = None


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
            print(f"[thermostat] wrote TargetTempHc = {target}")
            return
    print("[thermostat] WARNING: TargetTempHc msgdef not found")


def _run_async(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=15)


def _start_async_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


# ── Control logic ──────────────────────────────────────────────────────────────
def _rolling_avg_target() -> float | None:
    """Average of TargetTempHc values written in the past FAILSAFE_WINDOW seconds."""
    cutoff = datetime.now() - timedelta(seconds=FAILSAFE_WINDOW)
    recent = [v for ts, v in target_history if ts >= cutoff]
    return sum(recent) / len(recent) if recent else None


def _prune_target_history():
    """Remove entries older than FAILSAFE_WINDOW."""
    cutoff = datetime.now() - timedelta(seconds=FAILSAFE_WINDOW)
    while target_history and target_history[0][0] < cutoff:
        target_history.popleft()


def _apply_control(room_temp: float) -> dict:
    """
    Compute the new target, apply failsafe, decide whether to write.
    Returns a status dict.
    """
    global last_write_time, last_target, last_status

    now = datetime.now()

    # Formula: mirror around setpoint + signed-square penalty
    # error > 0 (room cold) → target pushed UP   to heat faster
    # error < 0 (room warm) → target pushed DOWN  to kill the pump's water-temp integral
    # penalty = α * error * |error|  (same as α * error², but preserves sign)
    error = SETPOINT - room_temp
    penalty = PENALTY_ALPHA * abs(error) * error
    raw_target = SETPOINT + error + penalty
    raw_target = round(raw_target, 1)

    # Failsafe: clamp to ±FAILSAFE_DELTA of rolling average
    _prune_target_history()
    avg = _rolling_avg_target()
    if avg is not None:
        clamped = round(max(avg - FAILSAFE_DELTA, min(avg + FAILSAFE_DELTA, raw_target)), 1)
        failsafe_active = clamped != raw_target
    else:
        clamped = raw_target
        failsafe_active = False

    # Rate limit: once per UPDATE_INTERVAL
    if last_write_time and (now - last_write_time).total_seconds() < UPDATE_INTERVAL:
        remaining = UPDATE_INTERVAL - (now - last_write_time).total_seconds()
        last_status = (f"Room {room_temp}°C → target {clamped}°C "
                       f"(next write in {int(remaining)}s)")
        return {"target": clamped, "wrote": False,
                "failsafe": failsafe_active, "avg": avg}

    # Write to ebusd
    try:
        _run_async(_write_target(clamped))
        last_write_time = now
        last_target = clamped
        target_history.append((now, clamped))
        fs_note = " [failsafe]" if failsafe_active else ""
        last_status = f"Room {room_temp}°C → wrote target {clamped}°C{fs_note}"
        return {"target": clamped, "wrote": True,
                "failsafe": failsafe_active, "avg": avg}
    except Exception as e:
        last_status = f"Error writing target: {e}"
        print(f"[thermostat] write error: {e}")
        return {"target": clamped, "wrote": False,
                "failsafe": failsafe_active, "avg": avg, "error": str(e)}


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

    _broadcast(json.dumps({
        "type": "update",
        "ts": ts,
        "room_temp": value,
        "target": result["target"],
        "wrote": result["wrote"],
        "failsafe": result["failsafe"],
        "avg": result.get("avg"),
        "status": last_status,
    }))

    return jsonify({"ok": True, "ts": ts, **result})


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
                "history": list(room_history),
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
<meta name="apple-mobile-web-app-title" content="Thermostat">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0f1117">
<title>Thermostat</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:         #0f1117;
    --surface:    #181c27;
    --card:       #1e2333;
    --border:     #2a3045;
    --accent:     #4fc3f7;
    --accent2:    #81c995;
    --warn:       #ffb74d;
    --red:        #ef5350;
    --text:       #e8eaf0;
    --text-dim:   #7a8099;
    --back-bg:    #141824;
    --mono:       'DM Mono', monospace;
    --sans:       'DM Sans', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  html {
    height: 100%;
  }

  html, body {
    background: var(--bg);
    font-family: var(--sans);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
  }

  body {
    min-height: 100%;
    min-height: 100dvh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: env(safe-area-inset-top, 16px) env(safe-area-inset-right, 16px) env(safe-area-inset-bottom, 16px) env(safe-area-inset-left, 16px);
  }

  .widget-scene {
    width: 100%;
    max-width: 420px;
    perspective: 1200px;
  }

  .widget-flipper {
    width: 100%;
    position: relative;
    transform-style: preserve-3d;
    transition: transform 0.65s cubic-bezier(0.4, 0.2, 0.2, 1);
  }

  .widget-flipper.is-flipped {
    transform: rotateY(180deg);
  }

  .widget-face {
    width: 100%;
    backface-visibility: hidden;
    -webkit-backface-visibility: hidden;
  }

  /* Front face: normal flow */
  .widget-front {
    position: relative;
  }

  /* Back face: absolutely positioned, rotated */
  .widget-back {
    position: absolute;
    top: 0; left: 0;
    transform: rotateY(180deg);
    background: var(--back-bg);
    border-bottom: 1px solid var(--border);
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
  .temp-row { margin-bottom: 16px; }
  .temp-sublabel {
    font-size: 11px; font-weight: 500;
    letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-dim); margin-bottom: 2px;
  }
  .temp-big {
    font-family: var(--mono); font-size: 86px; font-weight: 300;
    line-height: 1; color: var(--accent); letter-spacing: -3px;
  }
  .temp-unit { font-size: 26px; font-weight: 300; color: var(--text-dim); }

  /* Row 3: sun sentence */
  .sun-sentence {
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

        <!-- Row 2: big temperature -->
        <div class="temp-row">
          <div class="temp-sublabel">Room Temperature</div>
          <span class="temp-big" id="lcd-temp">--.-</span>
          <span class="temp-unit">°C</span>
        </div>

        <!-- Row 3: sun sentence -->
        <div class="sun-sentence" id="sun-sentence">—</div>

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
          <div class="chip-label">Last write</div>
          <div class="chip-val" id="info-write">—</div>
        </div>
        <div class="chip">
          <div class="chip-label">3h avg target</div>
          <div class="chip-val" id="info-avg">—</div>
        </div>
        <div class="chip">
          <div class="chip-label">Failsafe</div>
          <div class="chip-val ok" id="info-failsafe">—</div>
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

// ── Weather ───────────────────────────────────────────────────────────────────
const WMO_ICONS = {
  0:'☀️',1:'🌤️',2:'⛅',3:'☁️',
  45:'🌫️',48:'🌫️',
  51:'🌦️',53:'🌦️',55:'🌧️',
  61:'🌧️',63:'🌧️',65:'🌧️',
  71:'🌨️',73:'🌨️',75:'❄️',
  80:'🌦️',81:'🌧️',82:'⛈️',
  95:'⛈️',96:'⛈️',99:'⛈️',
};
const WMO_DESC = {
  0:'Clear',1:'Mainly clear',2:'Partly cloudy',3:'Overcast',
  45:'Fog',48:'Icy fog',
  51:'Light drizzle',53:'Drizzle',55:'Heavy drizzle',
  61:'Light rain',63:'Rain',65:'Heavy rain',
  71:'Light snow',73:'Snow',75:'Heavy snow',
  80:'Showers',81:'Heavy showers',82:'Violent showers',
  95:'Thunderstorm',96:'Thunderstorm+hail',99:'Thunderstorm+hail',
};

async function fetchWeather() {
  try {
    const r = await fetch('https://api.open-meteo.com/v1/forecast?latitude=52.3&longitude=4.98&current=weather_code,temperature_2m,is_day&timezone=Europe%2FAmsterdam');
    const d = await r.json();
    const code = d.current.weather_code;
    const isDay = d.current.is_day === 1;

    // Night overrides: clear/partly-cloudy sky codes get moon treatment
    const NIGHT_ICONS = { 0:'🌕', 1:'🌔', 2:'🌑', 3:'☁️' };
    let icon;
    if (!isDay && code <= 3) {
      icon = NIGHT_ICONS[code] ?? '🌙';
    } else {
      icon = WMO_ICONS[code] ?? '🌡️';
    }

    document.getElementById('weather-icon').textContent = icon;
    document.getElementById('weather-desc').textContent =
      (WMO_DESC[code] ?? 'Unknown') + ' · ' + d.current.temperature_2m.toFixed(1) + '°C outside';
  } catch {
    document.getElementById('weather-icon').textContent = '🌡️';
    document.getElementById('weather-desc').textContent = 'weather unavailable';
  }
}
fetchWeather();
setInterval(fetchWeather, 30 * 60 * 1000);

// ── UV Index ──────────────────────────────────────────────────────────────────
const UV_CACHE_KEY = 'uv_cache';
const UV_MAX_INDEX = 11;

async function fetchUV() {
  const cached = (() => {
    try { return JSON.parse(sessionStorage.getItem(UV_CACHE_KEY)); } catch { return null; }
  })();
  if (cached && (Date.now() - cached.ts) < 5 * 60 * 60 * 1000) { renderUV(cached.data); return; }
  try {
    const r = await fetch('https://uvindexapi.com/api/v1/forecast?latitude=52.3&longitude=4.98',
      { headers: { Accept: 'application/json' } });
    const d = await r.json();
    if (d.ok) {
      sessionStorage.setItem(UV_CACHE_KEY, JSON.stringify({ ts: Date.now(), data: d }));
      renderUV(d);
    }
  } catch { /* silent */ }
}

function uvToSentence(maxUV, isForTomorrow) {
  const when = isForTomorrow ? 'Tomorrow' : 'Today';
  if (maxUV === null || maxUV === undefined) return '';
  if (maxUV < 1)  return `${when} will be almost entirely overcast — barely any solar gain.`;
  if (maxUV < 2)  return `${when} is quite grey — very little sun to warm things up.`;
  if (maxUV < 3)  return `${when} has a little sun, but not much solar contribution to expect.`;
  if (maxUV < 5)  return `${when} should see some decent sunshine — a modest boost.`;
  if (maxUV < 7)  return `${when} looks sunny — the sun will do some of the heating work.`;
  if (maxUV < 9)  return `${when} is bright and sunny — good passive solar warmth expected.`;
  return `${when} will be very sunny — the house should warm up nicely on its own.`;
}

function renderUV(d) {
  const hour = new Date().getHours();
  const useTomorrow = hour >= 19;
  const maxUV = useTomorrow
    ? (d.tomorrow?.max?.uv_index ?? null)
    : (d.today?.max?.uv_index ?? null);
  const sentence = uvToSentence(maxUV, useTomorrow);
  document.getElementById('sun-sentence').textContent = sentence;
}
fetchUV();
setInterval(fetchUV, 5 * 60 * 60 * 1000);

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

  if (msg.status) {
    document.getElementById('status-text').textContent = msg.status;
    document.getElementById('status-dot').className =
      'status-dot ' + (msg.wrote ? 'ok' : msg.failsafe ? 'warn' : '');
  }
  if (msg.last_write) {
    lastWriteTime = new Date(msg.last_write.replace('T',' '));
    document.getElementById('info-write').textContent =
      lastWriteTime.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  }
  if (msg.avg != null)
    document.getElementById('info-avg').textContent = msg.avg.toFixed(1) + '°C';
  if (msg.failsafe != null) {
    const el = document.getElementById('info-failsafe');
    el.textContent = msg.failsafe ? 'ACTIVE' : 'OK';
    el.className = 'chip-val ' + (msg.failsafe ? 'err' : 'ok');
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
  }
  if (msg.type === 'update') {
    chart.data.datasets[0].data.push({ x: new Date(msg.ts.replace('T',' ')), y: msg.room_temp });
    chart.update('none');
    applyState(msg);
    if (msg.wrote) lastWriteTime = new Date(msg.ts.replace('T',' '));
  }
};
</script>
</body>
</html>
"""

# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_start_async_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
