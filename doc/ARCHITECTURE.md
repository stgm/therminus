Architecture overview
---------------------
The app has three concurrent concerns, all running in the same process:

  1. Flask web server  — serves the UI and accepts room-temperature POSTs
                         from an external sensor (e.g. Home Assistant).
  2. Background thread — runs the control loop every CONTROL_DT seconds,
                         reads pump telemetry from ebusd, and pushes state
                         updates to all connected browsers via SSE.
  3. Asyncio loop      — a dedicated event loop (separate thread) that all
                         pyebus coroutines are dispatched to via
                         run_coroutine_threadsafe. This keeps async ebus I/O
                         out of the synchronous Flask/control code.

All mutable state is protected by state_lock (threading.Lock). Ebus reads
are intentionally performed *outside* the lock to avoid blocking it during
I/O, then the results are read inside _control_tick under the lock.

Surviving a bad bus
-------------------
When ebusd stalls, the failure must not take the controller with it. If the
background thread dies, Flask keeps serving and the page keeps showing the
last known state while the pump is no longer being controlled at all — the
worst failure mode this app has. Two rules prevent that.

First, nothing escapes the background loop: _background_refresh guards the
weather refresh and the control tick separately and prints a traceback
instead of letting either one end the thread.

Second, ebus calls are bounded by a three-level timeout ladder, each level a
backstop for the one below it:

  socket (EBUS_SOCKET_TIMEOUT)  pyebus gives up on a single request that
                                gets no response.
  op     (EBUS_OP_TIMEOUT)      the whole coroutine is cancelled inside the
                                asyncio loop, where its cleanup still runs.
  call   (EBUS_CALL_TIMEOUT)    the calling thread stops waiting. Only
                                reached if the loop itself is wedged, so it
                                is larger than the op timeout; the future is
                                cancelled so abandoned work does not pile up
                                behind the next call.

Each call reconnects: pyebus can get confused after a period of no signal, so
_get_ebus() builds a fresh Ebus every time and disconnects the previous one,
including any left behind by a cancelled call.

A failed telemetry read is counted, not raised — read_telemetry() returns an
empty Telemetry and the controller sits still, because controller.tick() is
gated on has_all_data() and writes nothing without a full picture. After
BUS_FAULT_AFTER consecutive failures ebus.bus_healthy() goes False, which the
UI shows as a red status dot and a "no contact" sentence, so a blind
controller cannot be mistaken for an idle one.

Control strategy
----------------
The pump is controlled by writing a fake room-temperature setpoint
(TargetTempHc) via the ebus hmu circuit. The pump maps this to a water
temperature via its own internal heating curve (outdoor reset), so we never
need to know or set water temperatures directly — the pump's own thermostat
handles that layer.

The outer control loop is a three-state bang-bang machine:

  RESTING  →  write IDLE_TEMP (15°C) every tick. The pump sees a very low
               setpoint and shuts down its compressor. The system rests for
               at minimum T_MIN_REST_SHORT or T_MIN_REST_LONG seconds
               depending on how the previous run ended.

  RUNNING  →  write SETPOINT + Kp * error every tick (pure proportional).
               This is standard heat pump room compensation: a proportional
               nudge above or below the setpoint, clamped to [TARGET_MIN,
               TARGET_MAX]. No integral term is used — the slow thermal mass
               of UFH means integration adds complexity without meaningful
               benefit, and the pump's own heating curve already provides
               long-term correction via outdoor reset.

  WARMING  →  a special state entered when the floor has gone cold during a
               rest period (flow temp below FLOOR_COMFORT_TEMP) while the
               outdoor temp is below WARMING_OUTDOOR_MAX. Writes SETPOINT
               as the target and waits for the pump to decide it's done
               (compressor stops). This lets the pump run at minimum
               intensity to restore floor warmth without Therminus trying
               to drive it via room temperature logic.

State transitions
-----------------
  RESTING → RUNNING   : room temp drops below SETPOINT - BAND
                         AND rest timer has elapsed
  RESTING → WARMING   : room temp is at/below SETPOINT + BAND
                         AND floor flow temp < FLOOR_COMFORT_TEMP
                         AND outdoor temp < WARMING_OUTDOOR_MAX
                         AND compressor is idle (not doing DHW)
                         AND rest timer has elapsed
  RUNNING → RESTING   : room temp rises above SETPOINT + BAND
                         (short rest, T_MIN_REST_SHORT)
                     OR: compressor stops AND valve was on heating circuit
                         (long rest, T_MIN_REST_LONG)
  WARMING → RESTING   : compressor stops AND valve was on heating circuit
                         (long rest, T_MIN_REST_LONG)

The "valve was on heating circuit" guard is critical: the pump also runs its
compressor for domestic hot water (DHW). We use vwzio/ThreeWayValve to
distinguish. Since the valve may already have switched back to neutral by the
time the compressor stops, we track ebus_valve_was_heating — set to True on
every tick where the compressor is running AND the valve is on the heating
circuit. This sticky flag is what we check on compressor-off.

On startup, if the compressor is already running for heating, we jump
directly to RUNNING (detected on the first ebus read before any write).
If everything is off at startup, we default to a 60-minute rest to avoid
immediately triggering a run into an unknown state.

Ebus reads
----------
Four values are read from ebusd in a single pyebus session per tick:
  hmu/RunDataFlowTemp       — floor circuit flow temperature (°C)
  hmu/RunDataCompressorSpeed — compressor speed (%, 0 = idle)
  vwzio/ThreeWayValve       — 'heating circuit' or 'warm water circuit'
  vwzio/OutdoorTemp         — outdoor air temperature (°C)

Reads are only performed when needed:
  - During RUNNING and WARMING: every tick (to detect compressor stop)
  - During RESTING: only when the rest timer has elapsed and room temp
    is within/below the band (to evaluate the WARMING trigger)

Weather
-------
Current weather conditions are fetched from Open-Meteo (free, no API key)
every 30 minutes and pushed to all clients via SSE. The weather icon
respects day/night: clear-sky icons switch to moon variants after sunset
using the is_day field from the API. Weather data is only fetched once
server-side regardless of how many clients are connected.

UI
--
Single-page mobile-first web app served at port 6790. The front card shows
room temperature, current state (Heating / Warming the floor / Resting),
a human-readable explanation of what's happening and why, and the current
weather. Tapping the info button flips the card to reveal a technical back
panel with the raw status string, last/next write times, state chip, and a
room-temperature history chart for the current day.

All UI updates are pushed from server to client via Server-Sent Events (SSE)
on /api/stream. Clients never poll — they just listen. The clock and
countdown timer are the only pieces of logic that run client-side.

Sensor input
------------
Room temperature is POSTed to /roomtemp by an external source (e.g. a
Home Assistant automation or a cron job calling curl). The value field
accepts comma or dot as decimal separator and strips unit suffixes like
"°C", so "21,4 °C" and "21.4" both work. Valid range: 5–35°C.

Port: 6790
