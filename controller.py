"""
HeatPumpController — pure state machine for Therminus.

Receives sensor and ebus readings via tick(); updates internal state and
computes a target setpoint. No I/O, no Flask dependencies.

Public attributes (read by app.py):
    state         "IDLE" | "RUNNING" | "RESTING" | "WATER" | "OFF"
    target        setpoint (°C) to write to the pump after the last tick

Private attributes:
    _t_min_rest   seconds the current RESTING period must last
    _state_since  datetime when the current state was entered
    _setpoint     desired room temperature (°C)
    _band         deadband half-width (°C)

State transitions (our logic):
    RESTING → IDLE       rest timer elapsed
    IDLE    → RUNNING    ebus: compressor > 0 AND valve == heating circuit
    RUNNING → RESTING    ebus: compressor == 0

State transitions (heat pump controlled):
    *       → WATER      ebus: compressor > 0 AND valve == warm water circuit
    WATER   → IDLE       DHW ended AND 10-min after-run wait elapsed
    *       → OFF        ebus: building_circuit_flow == 0 (not from WATER)
    OFF     → RUNNING    ebus: circuit_running AND heating
    OFF     → IDLE       ebus: circuit_running AND not heating
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

# After DHW ends, wait this long before leaving WATER state.
# Prevents mistaking a post-DHW after-run on the heating circuit for a new heating cycle.
WATER_AFTER_RUN_WAIT = 10 * 60  # seconds (10 min)

# Written to the pump during RESTING (and when IDLE with room above band).
# Far enough below any real room temperature that the pump will not run its
# compressor for space heating.
IDLE_TEMP    = 15.0   # °C

# Run extender: keeps the pump running by nudging MinFlowTemp upward when the
# compressor is at minimum modulation but the flow temperature still overshoots
# the target.  Resets to MIN_FLOW_TEMP when no longer needed.
MIN_FLOW_TEMP        = 15.0   # °C — pump's configured baseline minimum flow temperature
COMPRESSOR_MIN_SPEED = 30.0   # % — minimum modulation speed (pump cannot go lower)
COMPRESSOR_MIN_TOL   =  1.0   # % — tolerance band around minimum modulation


class HeatPumpController:
    def __init__(self):
        # Public — readable by app.py
        self.state       = "IDLE"
        self.target      = IDLE_TEMP

        # Private
        self._setpoint    = SETPOINT
        self._band        = BAND
        self._state_since = datetime.now()
        self._t_min_rest  = T_MIN_REST
        self._desired_min_flow_temp: float | None = None  # None = no write needed

        # Last known sensor values — used by debug_status()
        self._current_temp     = None
        self._compressor_speed = None
        self._water_ended_at: datetime | None = None  # when DHW last stopped (for after-run wait)

    def current_temp(self):
        return self._current_temp

    def error(self):
        return SETPOINT - self._current_temp

    def elapsed(self) -> int:
        return (datetime.now() - self._state_since).total_seconds()

    def temp_above_upper_band(self) -> bool:
        return self._current_temp > self._setpoint + self._band

    def temp_below_lower_band(self) -> bool:
        return self._current_temp < self._setpoint - self._band

    def tick(self, current_temp: float, telemetry) -> dict:
        """
        Advance the state machine one step.

        Reads current conditions; may transition state; always updates
        self.target with the setpoint the pump should receive this tick.
        Sets self._desired_min_flow_temp when the run extender wants to write
        MinFlowTemp (None means no write needed).
        Returns {"room_target": float, "min_flow_temp": float | None}.
        """
        if current_temp is None:
            return {"room_target": None, "min_flow_temp": None}

        now = datetime.now()
        self._current_temp     = current_temp

        # ── Phase 1: transitions ──────────────────────────────────────────────

        # Enter WATER from any non-WATER state (heat pump started making DHW)
        if self.state != "WATER" and telemetry.making_dhw():
            self._transition("WATER")
            self._water_ended_at = None

        elif self.state == "WATER":
            if telemetry.making_dhw():
                self._water_ended_at = None  # still active, reset exit timer
            else:
                if self._water_ended_at is None:
                    self._water_ended_at = now
                elif (now - self._water_ended_at).total_seconds() >= WATER_AFTER_RUN_WAIT:
                    self._transition("IDLE")
                    self._water_ended_at = None

        elif self.state == "OFF":
            if telemetry.circuit_running():
                # WATER is handled by the first branch above (making_dhw() fires first).
                # Distinguish between a real heating run and an idle restart.
                self._transition("RUNNING" if telemetry.heating() else "IDLE")

        # Remaining states: IDLE, RUNNING, RESTING
        elif telemetry.circuit_off():
            self._transition("OFF")

        elif self.state == "RESTING":
            if self.elapsed() >= self._t_min_rest:
                self._transition("IDLE")

        elif self.state == "IDLE":
            if telemetry.heating():
                self._transition("RUNNING")

        elif self.state == "RUNNING":
            if telemetry.compressor_off():
                self._t_min_rest = T_MIN_REST
                self._transition("RESTING")

        # ── Phase 2: compute room target───────────────────────────────────────

        if self.state == "RESTING":
            # Suppress the pump while the floor distributes heat.
            self._set_idle_target()

        else:
            # Always set the right target room temp so the pump knows what's going on.
            if current_temp > (SETPOINT + BAND):
                self._set_idle_target()
            else:
                self._set_active_target(current_temp, self.error())

        # ── Phase 3: run extender ─────────────────────────────────────────────
        # strategy: keep raising the minimum flow temp while doing the heating run

        # make sure extender stops when room temp reached
        if self.state != "RUNNING":
            self._desired_min_flow_temp = MIN_FLOW_TEMP
            return {"room_target": self.target, "min_flow_temp": self._desired_min_flow_temp}

        if self._has_run_extender_data(telemetry):
            compressor_running_at_min = self._is_compressor_running_at_min(telemetry)

            if (current_temp > SETPOINT - BAND and self.elapsed() >= 3 * 60 * 60):
                self._desired_min_flow_temp = MIN_FLOW_TEMP
                print(f"[controller] run-extender stopped after 3 hours and room is good"
                      f"  min={telemetry.min_flow_temp}")

            elif telemetry.min_flow_temp < MIN_FLOW_TEMP:
                # Pump minimum dropped below our baseline — restore it.
                self._desired_min_flow_temp = MIN_FLOW_TEMP
                print(f"[controller] run-extender: reset (min below baseline)"
                      f"  min={telemetry.min_flow_temp}")

            elif (telemetry.target_flow_temp < telemetry.min_flow_temp
                  and telemetry.min_flow_temp > MIN_FLOW_TEMP):
                # Target flow calculated by heat pump drops below the set min flow temp
                # We raised the minimum, but heat is no longer requested above it.
                self._desired_min_flow_temp = MIN_FLOW_TEMP
                print(f"[controller] run-extender: reset (no longer needed)"
                      f"  target={telemetry.target_flow_temp}  min={telemetry.min_flow_temp}")

            elif (telemetry.flow_temp > telemetry.target_flow_temp
                  and compressor_running_at_min
                  and telemetry.flow_temp < telemetry.max_flow_temp
                  and telemetry.target_flow_temp >= MIN_FLOW_TEMP):
                # Actual flow overshoots target at minimum modulation — extend the run.
                self._desired_min_flow_temp = telemetry.flow_temp
                print(f"[controller] run-extender: extend"
                      f"  flow={telemetry.flow_temp}  target={telemetry.target_flow_temp}"
                      f"  comp={telemetry.compressor_speed}%")

        return {"room_target": self.target, "min_flow_temp": self._desired_min_flow_temp}

    def _transition(self, new_state: str) -> None:
        """Record a state change and log it."""
        print(f"[controller] {self.state} → {new_state}")
        self.state       = new_state
        self._state_since = datetime.now()

    def _has_run_extender_data(self, telemetry) -> bool:
        """True when all telemetry fields required by the run extender are available."""
        return None not in (
            telemetry.compressor_speed, telemetry.flow_temp, telemetry.target_flow_temp,
            telemetry.min_flow_temp, telemetry.max_flow_temp)

    def _is_compressor_running_at_min(self, telemetry) -> bool:
        """True when the compressor is on and running at its minimum modulation speed."""
        return (telemetry.compressor_on()
                and abs(telemetry.compressor_speed - COMPRESSOR_MIN_SPEED) <= COMPRESSOR_MIN_TOL)

    def _set_active_target(self, current_temp: float, error: float) -> None:
        """Set target using the Vaillant active algorithm: mirror room error onto flow setpoint."""
        self.target = round(max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)

    def _set_idle_target(self) -> None:
        """Set target to idle temp — low enough that the pump will not run its compressor."""
        self.target = IDLE_TEMP

    def debug_status(self) -> str:
        """Internal debug status string. Not for display — use for logging/back panel."""
        if self._current_temp is None:
            return "Waiting for room temperature…"
        now     = datetime.now()
        t       = self._current_temp
        elapsed = self.elapsed()
        error   = self.error()
        dhw     = (self.state == "WATER")
        if self.state == "WATER":
            wait = ""
            if self._water_ended_at is not None:
                waited = (now - self._water_ended_at).total_seconds()
                wait = f"  after-run={waited/60:.0f}/{WATER_AFTER_RUN_WAIT/60:.0f}min"
            return f"WATER  room={t:.1f}°C  elapsed={elapsed/60:.0f}min{wait}"
        if self.state == "OFF":
            return f"OFF  room={t:.1f}°C  (circuit flow = 0)"
        if self.state == "RESTING":
            return (f"RESTING  room={t:.1f}°C"
                    f"  rested={elapsed/60:.0f}/{self._t_min_rest/60:.0f}min"
                    f"  dhw={dhw}")
        if self.state == "IDLE":
            return (f"IDLE  room={t:.1f}°C"
                    f"  → {self.target}°C  dhw={dhw}")
        if self.state == "RUNNING":
            ext = (f"  min↑{self._desired_min_flow_temp:.1f}°C"
                   if self._desired_min_flow_temp and self._desired_min_flow_temp > MIN_FLOW_TEMP
                   else "")
            if t > (SETPOINT + BAND):
                return (f"RUNNING  room={t:.1f}°C"
                        f"  warm, waiting for compressor stop  ran={elapsed/60:.0f}min{ext}")
            return (f"RUNNING  room={t:.1f}°C  err={error:+.2f}"
                    f"  → {self.target}°C  ran={elapsed/60:.0f}min  dhw={dhw}{ext}")
        return ""
