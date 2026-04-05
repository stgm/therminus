"""
HeatPumpController — pure state machine for Therminus.

Receives sensor and ebus readings via tick(); updates internal state and
computes a target setpoint. No I/O, no Flask dependencies.

Public attributes (read by app.py):
    state        "IDLE" | "RUNNING" | "RESTING"
    state_since  datetime when the current state was entered
    t_min_rest   seconds the current RESTING period must last
    target       setpoint (°C) to write to the pump after the last tick
    dhw_active   True when compressor is running on the DHW circuit
    setpoint     desired room temperature (°C)
    band         deadband half-width (°C)

State transitions:
    RESTING → IDLE       rest timer elapsed
    IDLE    → RUNNING    ebus: compressor > 0 AND valve == heating circuit
    RUNNING → RESTING    ebus: compressor == 0
"""

from datetime import datetime

# ── Control constants ─────────────────────────────────────────────────────────

# The desired room temperature. All control logic is centred around this value.
SETPOINT     = 21.0   # °C

# Deadband around SETPOINT. Prevents reacting to tiny fluctuations.
BAND         = 0.3    # °C — half-width

# Proportional gain. During RUNNING:
#   target = clamp(SETPOINT + KP * error, TARGET_MIN, TARGET_MAX)
# KP = 1.0 means: 1°C below setpoint → request 1°C above setpoint from pump.
# No integral term — UFH thermal dynamics are slow and the pump's own
# outdoor-reset curve provides long-term correction without windup risk.
KP           = 1.0
TARGET_MIN   = 16.0   # °C — safety floor; never request below this
TARGET_MAX   = 24.0   # °C — safety ceiling; never request above this

# Minimum rest after a run ends (compressor stopped on its own).
# Gives the floor time to distribute heat before the next cycle.
T_MIN_REST   = 60 * 60   # seconds (60 min)

# Written to the pump during RESTING (and when IDLE with room above band).
# Far enough below any real room temperature that the pump will not run its
# compressor for space heating.
IDLE_TEMP    = 15.0   # °C

# Three-way valve position strings as reported by ebusd.
VALVE_HEATING = 'heating circuit'
VALVE_DHW     = 'warm water circuit'


class HeatPumpController:
    def __init__(self):
        # Public — readable by app.py
        self.setpoint    = SETPOINT
        self.band        = BAND
        self.state       = "IDLE"
        self.state_since = datetime.now()
        self.t_min_rest  = T_MIN_REST
        self.target      = IDLE_TEMP
        self.dhw_active  = False

        # Last known sensor values — used by debug_status()
        self._current_temp     = None
        self._compressor_speed = None

    def tick(self, now: datetime, current_temp: float,
             compressor_speed, valve) -> None:
        """
        Advance the state machine one step.

        Reads current conditions; may transition state; always updates
        self.target with the setpoint the pump should receive this tick.
        Returns nothing — callers read attributes directly.
        """
        if current_temp is None:
            return

        self._current_temp     = current_temp
        self._compressor_speed = compressor_speed
        self.dhw_active = (compressor_speed is not None
                           and compressor_speed > 0
                           and valve == VALVE_DHW)

        elapsed = (now - self.state_since).total_seconds()
        error   = SETPOINT - current_temp

        # ── Phase 1: transitions ──────────────────────────────────────────────

        if self.state == "RESTING":
            if elapsed >= self.t_min_rest:
                self.state       = "IDLE"
                self.state_since = now
                print(f"[controller] → IDLE  rested={elapsed/60:.0f}min")

        elif self.state == "IDLE":
            if (compressor_speed is not None
                    and compressor_speed > 0
                    and valve == VALVE_HEATING):
                self.state       = "RUNNING"
                self.state_since = now
                print(f"[controller] → RUNNING  room={current_temp:.1f}")

        elif self.state == "RUNNING":
            if compressor_speed is not None and compressor_speed == 0:
                self.state       = "RESTING"
                self.t_min_rest  = T_MIN_REST
                self.state_since = now
                print(f"[controller] → RESTING (compressor stopped)"
                      f"  room={current_temp:.1f}  rest={self.t_min_rest/60:.0f}min")

        # ── Phase 2: compute target ───────────────────────────────────────────

        if self.state == "RESTING":
            self.target = IDLE_TEMP

        elif self.state == "IDLE":
            if current_temp > (SETPOINT + BAND):
                self.target = IDLE_TEMP
            else:
                self.target = round(
                    max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)

        elif self.state == "RUNNING":
            if current_temp > (SETPOINT + BAND):
                # Room warm — ask pump to stop; stay RUNNING until compressor off.
                self.target = IDLE_TEMP
            else:
                self.target = round(
                    max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)

    def debug_status(self, now: datetime) -> str:
        """Internal debug status string. Not for display — use for logging/back panel."""
        if self._current_temp is None:
            return "Waiting for room temperature…"
        t       = self._current_temp
        elapsed = (now - self.state_since).total_seconds()
        error   = SETPOINT - t
        dhw     = self.dhw_active
        if self.state == "RESTING":
            return (f"RESTING  room={t:.1f}°C"
                    f"  rested={elapsed/60:.0f}/{self.t_min_rest/60:.0f}min"
                    f"  dhw={dhw}")
        if self.state == "IDLE":
            return (f"IDLE  room={t:.1f}°C"
                    f"  → {self.target}°C  dhw={dhw}")
        if self.state == "RUNNING":
            if t > (SETPOINT + BAND):
                return (f"RUNNING  room={t:.1f}°C"
                        f"  warm, waiting for compressor stop  ran={elapsed/60:.0f}min")
            return (f"RUNNING  room={t:.1f}°C  err={error:+.2f}"
                    f"  → {self.target}°C  ran={elapsed/60:.0f}min  dhw={dhw}")
        return ""
