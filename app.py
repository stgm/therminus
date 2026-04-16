"""
Therminus — Heat Pump Controller
=================================
Controls a hydronic heat pump with underfloor heating (UFH) by writing a
fake room-temperature setpoint (TargetTempHc) to the pump via ebusd/pyebus.

See doc/ARCHITECTURE.md for an overview.
"""
import threading
import json
import re
import time
import zoneinfo
from datetime import datetime, timedelta
from collections import deque

_TZ = zoneinfo.ZoneInfo("Europe/Amsterdam")
from pathlib import Path
from flask import Flask, Response, render_template, jsonify, request
from controller import HeatPumpController
import ebus
import weather

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
CONTROL_DT = 60   # seconds — how often the control loop runs

# Number of room-temp readings to keep in memory for the history chart.
# At one reading per ~5 minutes, 1440 points covers roughly 5 days.
HISTORY_POINTS  = 1440

# ── Shared state ──────────────────────────────────────────────────────────────
# All variables below are accessed from multiple threads (Flask request handlers,
# the background control thread, and the SSE generator). All reads and writes
# must happen under state_lock, except where explicitly noted.
state_lock       = threading.Lock()
_night_mode_active: bool = False  # True while the overnight heating limiter is running
room_history     = deque(maxlen=HISTORY_POINTS)  # list of {ts, value} dicts for chart
state_events     = deque(maxlen=2000)            # list of {ts, state} — badge transitions today
_prev_badge_cls  = None                          # last recorded badge cls for transition detection
HISTORY_FILE     = Path("history.json")
sse_clients   = []    # list of queue.Queue, one per connected browser

# ── State machine ──────────────────────────────────────────────────────────────
controller = HeatPumpController()


def _make_badge() -> dict:
    """Badge label and CSS class for the current state. Called with state_lock held."""
    if controller.state == "WATER":
        return {"label": "Loading hot water", "cls": "dhw", "active": False}
    if controller.state == "OFF":
        return {"label": "Off", "cls": "off", "active": False}
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
    if controller.current_temp() is None:
        return "Waking up, waiting for the first temperature reading."

    if controller.state == "WATER":
        return f"Casually loading the hot water tank."

    elif controller.state == "OFF":
        return "Outside seems warm enough so everything's off."

    elif controller.state == "RESTING":
        if controller.temp_above_upper_band():
            return "Giving the floor a rest, it's warm enough!"
        elif controller.temp_below_lower_band():
            return f"Slightly cold, but your heat pump is taking a nap..."
        else:
            return "Heating done. I'll let it rest for now."

    elif controller.state == "IDLE":
        if controller.temp_above_upper_band():
            return "Pretty warm inside!"
        elif controller.temp_below_lower_band():
            return "Waiting for the pump to notice that it's a bit cold."
        else:
            return "Temperature is fine. Tuning up and down where needed."

    elif controller.state == "RUNNING":
        if controller.night_limit_reached():
            return "No heating anymore! Tomorrow's forecast is great."
        if controller.temp_above_upper_band():
            return "Heating the floor a little."
        elif controller.temp_below_lower_band():
            return f"Heating right now! Been at it for {controller.elapsed()/60:.0f} min."
        else:
            return "Heating a little to keep it nice and cosy."


def _activate_night_mode(outdoor_t: float | None) -> None:
    c = weather.cache or {}
    with state_lock:
        controller.start_night_mode(
            outdoor_temp=outdoor_t,
            forecast_low_tomorrow=c.get('forecast_low_tomorrow'),
            forecast_high_tomorrow=c.get('forecast_high_tomorrow'),
        )


def _dhw_scheduled_temp() -> float:
    """Return the target DHW temperature based on time of day and weekday."""
    now = datetime.now(_TZ)
    if 6 <= now.hour < 14:
        return 50.0
    if now.hour >= 14 and now.weekday() == 4:  # Friday
        return 60.0
    return 55.0


def _tick():
    """
    One control cycle. Called every CONTROL_DT seconds by _background_refresh.

    Ebus I/O happens outside state_lock (blocking calls routed through the
    asyncio loop); control logic and state updates happen inside it.
    """
    if controller.current_temp() is None:
        return

    global _night_mode_active

    # Ebus reads outside the lock (blocking I/O) — always read so we can detect
    # OFF (circuit flow) and WATER (DHW valve) from any state.
    telemetry = ebus.read_telemetry()

    # Night mode activation/deactivation. Runs every tick (every 60 s), which
    # is precise enough for an overnight schedule. The flag correctly handles
    # startup inside the night window without any special-case logic.
    _in_night_window = datetime.now(_TZ).hour >= 22 or datetime.now(_TZ).hour < 7
    if _in_night_window and not _night_mode_active:
        _activate_night_mode(telemetry.outdoor_temp)
        _night_mode_active = True
    elif not _in_night_window and _night_mode_active:
        with state_lock:
            controller.end_night_mode()
        _night_mode_active = False

    with state_lock:
        setpoints = controller.tick(telemetry)
        if setpoints["room_target"] is not None:
            setpoints["dhw_target"] = _dhw_scheduled_temp()
            ebus.write(setpoints)
        sentence = _make_status_sentence()
        badge    = _make_badge()
        event    = _record_state_event(badge["cls"])
    if event:
        _save_history()
    _broadcast(json.dumps({
        "type": "update",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "room_temp": controller.current_temp(),
        "target": controller.target,
        "state": controller.state,
        "status": controller.debug_status(),
        "status_sentence": sentence,
        "badge": badge,
        "state_event": event,
        "last_write": datetime.now().time().isoformat(timespec="minutes"),
    }))


def _background_refresh():
    """
    Long-running background thread. Runs for the lifetime of the process.

    Refreshes weather every WEATHER_INTERVAL seconds and runs one control
    tick every CONTROL_DT seconds. Sleeps 10 seconds between wakeups so
    ticks land within ±10s of their scheduled time without busy-waiting.
    """
    last_weather  = 0.0
    last_tick     = 0.0

    while True:
        now = time.time()
        if now - last_weather >= weather.INTERVAL:
            weather.fetch(on_update=lambda icon, desc: _broadcast(
                json.dumps({'type': 'weather', 'icon': icon, 'desc': desc})
            ))
            last_weather = now
        if now - last_tick >= CONTROL_DT:
            _tick()
            last_tick = now
        time.sleep(10)


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

@app.route("/")
def index():
    """Serve the single-page UI."""
    return render_template("interface.html", update_interval=CONTROL_DT)


@app.route("/roomtemp", methods=["POST"])
def post_roomtemp():
    """
    Receive a room temperature reading from an external sensor.

    Accepts form data or JSON with a 'current' field. Tolerates comma decimal
    separators and unit suffixes (e.g. "21,4 °C"). Frontend receives it
    on the next background cycle (within CONTROL_DT seconds).

    Returns JSON {ok, ts} or {error} with a 4xx status.
    """
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
        controller.update_temp(value)
        room_history.append({"ts": ts, "value": value})

    return jsonify({"ok": True, "ts": ts})


@app.route("/api/stream")
def api_stream():
    """
    Server-Sent Events stream. Browsers connect here and receive all real-time
    updates for the lifetime of the page.

    On connect, immediately sends a 'snapshot' event with the full current
    state so the UI can render without waiting for the next tick. Subsequent
    events are 'update' (control tick or sensor POST) and 'weather'.

    Each client gets its own Queue (maxsize=50). Slow or disconnected clients
    are silently dropped — they never block the control thread.
    A keepalive comment (': ping') is sent every 25 seconds so proxies and
    browsers don't close the connection on idle.
    """
    import queue
    q = queue.Queue(maxsize=50)
    sse_clients.append(q)

    def generate():
        with state_lock:
            snap = {
                "type": "snapshot",
                "room_temp": controller.current_temp(),
                "target": controller.target,
                "status": controller.debug_status(),
                "state": controller.state,
                "status_sentence": _make_status_sentence(),
                "badge": _make_badge(),
                "history": list(room_history),
                "state_events": list(state_events),
                "weather": weather.cache,
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
    # import argparse
    # parser = argparse.ArgumentParser(description="Therminus heat pump controller")
    # parser.add_argument(
    #     "--initial-rest", type=int, default=None, metavar="MINUTES",
    #     help="Override the initial resting period in minutes (default: 60). "
    #          "Use 0 to start immediately."
    # )
    # args = parser.parse_args()

    _load_history()
    ebus.start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
