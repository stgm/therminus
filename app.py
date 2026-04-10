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
from datetime import datetime, timedelta
from collections import deque
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
current_temp     = None          # most recent room temp from sensor POST (°C)
room_history     = deque(maxlen=HISTORY_POINTS)  # list of {ts, value} dicts for chart
state_events     = deque(maxlen=2000)            # list of {ts, state} — badge transitions today
_prev_badge_cls  = None                          # last recorded badge cls for transition detection
HISTORY_FILE     = Path("history.json")
sse_clients   = []    # list of queue.Queue, one per connected browser

# ── State machine ──────────────────────────────────────────────────────────────
controller = HeatPumpController()


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


def _control_tick(telemetry: ebus.Telemetry) -> None:
    """
    App-level control step. Called every CONTROL_DT seconds (and on sensor POST).

    Must be called with state_lock held. Passes fresh telemetry to the
    controller, then writes the resulting target to the pump via ebus.
    """
    if current_temp is None:
        return
    now = datetime.now()
    controller.tick(now, current_temp, telemetry)
    ebus.write_target(controller.target)
    if controller.desired_min_flow_temp is not None:
        ebus.write_min_flow_temp(controller.desired_min_flow_temp)


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
    telemetry = ebus.read_telemetry() if state_snapshot in ("RUNNING", "IDLE") else ebus.Telemetry()

    with state_lock:
        _control_tick(telemetry)
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
        if now - last_weather >= weather.INTERVAL:
            weather.fetch(on_update=lambda icon, desc: _broadcast(
                json.dumps({'type': 'weather', 'icon': icon, 'desc': desc})
            ))
            last_weather = now
        if now - last_integral >= CONTROL_DT:
            _tick()
            last_integral = now
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
    return render_template("interface.html", update_interval=ebus.UPDATE_INTERVAL)


@app.route("/roomtemp", methods=["POST"])
def post_roomtemp():
    """
    Receive a room temperature reading from an external sensor.

    Accepts form data or JSON with a 'current' field. Tolerates comma decimal
    separators and unit suffixes (e.g. "21,4 °C"). Triggers an immediate
    control tick so the pump setpoint is updated without waiting for the next
    background cycle.

    Returns JSON {ok, ts, target, state} or {error} with a 4xx status.
    """
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
    telemetry = ebus.read_telemetry() if state_snapshot in ("RUNNING", "IDLE") else ebus.Telemetry()

    with state_lock:
        _control_tick(telemetry)
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
    """Return the latest cached weather data as JSON {icon, desc, fetched_at}."""
    return jsonify(weather.cache or {})


@app.route("/api/state")
def api_state():
    """
    Return a JSON snapshot of current controller state for debugging or integration.

    Includes room temperature, last written target, debug status string, full
    room temperature history, and the timestamp of the last ebusd write.
    """
    with state_lock:
        return jsonify({
            "room_temp":  current_temp,
            "target":     ebus.last_target,
            "status":     controller.debug_status(datetime.now()),
            "history":    list(room_history),
            "last_write": ebus.last_write_time.isoformat() if ebus.last_write_time else None,
        })


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
                "room_temp": current_temp,
                "target": controller.target,
                "status": controller.debug_status(datetime.now()),
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
    ebus.start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
