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

from flask import Flask, Response, render_template, jsonify, request
from controller import HeatPumpController
from history import DayHistory
import ebus
import weather
import presenter

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
CONTROL_DT = 60   # seconds — how often the control loop runs

# ── Shared state ──────────────────────────────────────────────────────────────
# All variables below are accessed from multiple threads (Flask request handlers,
# the background control thread, and the SSE generator). All reads and writes
# must happen under state_lock, except where explicitly noted.
state_lock  = threading.Lock()
history     = DayHistory()   # owns room_history, state_events, _prev_badge_cls
sse_clients = []             # list of queue.Queue, one per connected browser

# ── State machine ──────────────────────────────────────────────────────────────
controller = HeatPumpController()

def _tick():
    """
    One control cycle. Called every CONTROL_DT seconds by _background_refresh.

    Ebus I/O happens outside state_lock (blocking calls routed through the
    asyncio loop); control logic and state updates happen inside it.
    """
    if controller.current_temp() is None:
        return

    # Ebus reads outside the lock (blocking I/O).
    telemetry = ebus.read_telemetry()

    with state_lock:
        setpoints = controller.tick(telemetry, weather_cache=weather.cache)
        ebus.write(setpoints)
        sentence = presenter.status_sentence(controller)
        badge    = presenter.badge(controller)
        event    = history.record_state_event(badge["cls"])
    if event:
        history.save()
    _broadcast(json.dumps({
        "type": "update",
        "ts": ebus.now().isoformat(timespec="seconds"),
        "room_temp": controller.current_temp(),
        "target": controller.target,
        "state": controller.state,
        "status": controller.debug_status(),
        "status_sentence": sentence,
        "badge": badge,
        "state_event": event,
        "last_write": ebus.now().time().isoformat(timespec="minutes"),
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

    ts = ebus.now().isoformat(timespec="seconds")
    with state_lock:
        controller.update_temp(value)
        history.append_room_temp(ts, value)

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
                "status_sentence": presenter.status_sentence(controller),
                "badge": presenter.badge(controller),
                "history": list(history.room_history),
                "state_events": list(history.state_events),
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

    history.load()
    ebus.start()
    ebus.sync_time()
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=6790, debug=False, threaded=True)
