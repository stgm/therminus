"""
HeatPumpController — dual state machine for Therminus.

Receives sensor and ebus readings via tick(); updates internal state and
computes a target setpoint. No I/O, no Flask dependencies.

  Heat pump states  (from telemetry):
      dhw          compressor on + DHW valve
      dormant      building circuit flow == 0
      heating      compressor on + heating valve
      circulating  circuit running, compressor off

  Controller states:
      DHW        pump actively making hot water
      DHW_WAIT   DHW just ended; 10-min after-run settling
      IDLE       ready; waiting for pump to start heating
      RUNNING    pump is heating; we are managing the run
      RESTING    compressor just stopped; floor distributing; mandatory 60-min pause

Public attributes (read by app.py):
    state         one of the six controller states above
    target        setpoint (°C) to write to the pump after the last tick

Basic state transitions for this controller:
    *          → DHW        pump state == dhw  (from any non-DHW state)
    DHW        → DHW_WAIT   pump state != dhw  (DHW just finished; 10-min timer starts)
    DHW_WAIT   → IDLE       10-min timer elapsed
    IDLE       → RUNNING    pump state == heating
    RUNNING    → RESTING    pump state != heating
    RESTING    → IDLE       60-min rest timer elapsed
"""

import math
import time

import ebus
from logger import Logger
from run_extender import RunExtender
from night_mode import NightMode
from suppressor import CirculationSuppressor

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

# After DHW ends, wait this long before leaving DHW_WAIT state.
# Prevents mistaking a post-DHW after-run on the heating circuit for a new heating cycle.
DHW_AFTER_RUN_WAIT = 10 * 60  # seconds (10 min)

# Written to the pump during RESTING (and when IDLE/RUNNING with room above band).
# Far enough below any real room temperature that the pump will not run its
# compressor for space heating.
IDLE_TEMP    = 15.0   # °C

# Written to the pump during SUPPRESSED state.
# Low enough to ensure the circulation pump stops entirely.
SUPPRESSED_TEMP      = 10.0   # °C



class HeatPumpController:
    def __init__(self, initial_state: str | None = None):
        # Public — readable by app.py
        self.state       = initial_state if initial_state is not None else "IDLE"
        self.pump        = "dormant"   # last known pump state: dhw|dormant|heating|circulating
        self.target      = IDLE_TEMP

        # Private
        self._setpoint    = SETPOINT
        self._band        = BAND
        self._state_since = time.monotonic_ns()

        self.run_extender = RunExtender()
        self.night_mode = NightMode()
        self.suppressor = CirculationSuppressor()

        self.log = Logger()

        # Last known sensor value
        self._current_temp     = None

    def _pump_state(self, telemetry: ebus.Telemetry) -> str:
        """
        Derive the current pump hardware state from telemetry — no history, no side effects.

        Priority order ensures unambiguous classification:
        dhw          compressor on + DHW valve (takes priority over all)
        dormant      building circuit flow == 0
        heating      compressor on + heating valve
        circulating  circuit running, compressor off
        """
        if telemetry.is_making_dhw():      return "dhw"
        if telemetry.circuit_off():     return "dormant"
        if telemetry.is_heating():         return "heating"
        if telemetry.circuit_running(): return "circulating"
        return "dormant"                # telemetry not yet available

    def current_temp(self):
        return self._current_temp

    def update_temp(self, temp: float):
        """Update the known room temperature without running a full tick."""
        self._current_temp = temp

    def error(self):
        return SETPOINT - self._current_temp

    def elapsed(self) -> float:
        return (time.monotonic_ns() - self._state_since) / 1e9

    def room_temp_above_upper_band(self) -> bool:
        return self._current_temp > self._setpoint + self._band

    def room_temp_below_lower_band(self) -> bool:
        return self._current_temp < self._setpoint - self._band

    def tick(self, telemetry: ebus.Telemetry, weather_cache: dict | None = None) -> dict:
        """
        Advance the state machine one step.

        Reads current conditions; may transition state; always updates
        self.target with the setpoint the pump should receive this tick.
        Returns {"room_target": float | None, "min_flow_temp": float | None}.
        """
        if self._current_temp is None or not telemetry.has_all_data():
            return {}

        self.log.base = (
            f"flow={telemetry.flow_temp:2.2f}°C comp={telemetry.compressor_speed:3.0f}%"
            f" valve={telemetry.valve[:4]} in={self.current_temp():2.2f} out={telemetry.outdoor_temp:2.2f}°C"
        )


        # Night window: 22:00–08:00. Gets the current hour to decide when to start/stop.
        # When starting, it takes weather forecast data + local measurements.
        self.night_mode.tick(
            ebus.now().hour,
            on_activate=lambda: (telemetry.outdoor_temp, weather_cache or {}, self._current_temp),
        )

        self.pump = pump = self._pump_state(telemetry)

        # ── Phase 1: transitions ──────────────────────────────────────────────

        if pump == "dhw":
            # DHW wins from any non-DHW/DHW_WAIT state.
            self._transition("DHW")

        elif self.state == "DHW":
            if pump != "dhw":
                # DHW just stopped. In this case we move to DHW_WAIT, which
                # makes sure that we do not turn off the cirulation immediately.
                # Instead, we let the remaining hot water from the piping
                # distribute in the heating system for 10 minutes.
                self._transition("DHW_WAIT")

        elif self.state == "DHW_WAIT":
            # normal temperature management, except it will never be set
            # such that the circulation turns off
            if self.elapsed() >= DHW_AFTER_RUN_WAIT:
                self._transition("IDLE")

        elif self.state == "RESTING":
            # for some time after a run, keep target room temp low
            if self.elapsed() >= T_MIN_REST:
                self._transition("IDLE")

        elif self.state == "IDLE":
            if pump == "heating":
                self._transition("RUNNING")

        elif self.state == "RUNNING":
            if pump != "heating":
                self._transition("RESTING")

        # ── Phase 2: compute room target ──────────────────────────────────────

        if self.night_mode.limit_reached():
            # keep artificially low when heating limit is reached during the night
            # this is for colder nights and warmer days, it's fine to have it a bit
            # colder during the mornings
            self._set_idle_target()
            self.log.extra = (
                f"night limit reached"
                f"  total={self.night_mode.run_total()/3600:.2f}h"
                f"  limit={self.night_mode.limit_hours:.1f}h"
            )

        elif self.state == "RESTING":
            # keep the target artificially low for some time after a run, to force a pause
            self._set_idle_target()

        elif self._current_temp > (SETPOINT + BAND):
            # Room satisfied — keep target low so pump won't fire compressor.
            # Stay at 15 (not 10) while RUNNING so we don't risk circuit_off
            # before the compressor stops naturally.
            self.log.extra = "idle"
            self._set_idle_target()

        elif self._current_temp >= (SETPOINT - BAND) and telemetry.outdoor_temp >= 15:
            self.log.extra = "warm enough (out or in)"
            self._set_idle_target()

        else:
            # "active" strategy
            # regulate a little based on room temperature: the heat pump combines
            # with outside temp and heat curve to calculate required flow temp
            self.log.extra = "active"
            self._set_active_target(self._current_temp, self.error())

        # ── Phase 3: disable circulation ──────────────────────────────────────
        #

        if self.state == "IDLE" and self.target == IDLE_TEMP:
            if self.suppressor.check(telemetry):
                self.log.extra = "no circulation"
                self.target = SUPPRESSED_TEMP
            else:
                self.log.extra = "circulation for 5 minutes"
        else:
            self.suppressor.reset()

        # ── Phase 4: run extender ─────────────────────────────────────────────
        #
        # Slightly raises the minimum flow temp to extend a run started by the
        # heat pump; if not required, set minimum to a safe default (15ºC).

        if self.state == "RUNNING":
            desired_min_flow_temp, self.log.extra = self.run_extender.run(self._current_temp >= SETPOINT, telemetry)
        else:
            desired_min_flow_temp, self.log.extra = self.run_extender.stop()

        # Final conclusion
        conclusion = {
            "room_target": self.target,
            "min_flow_temp": desired_min_flow_temp,
            "dhw_target": self.dhw_scheduled_temp()
        }

        self.log.targets = f'{conclusion["room_target"]} | {conclusion["min_flow_temp"]} | {conclusion["dhw_target"]}'
        print(self.log)
        return conclusion

    def dhw_scheduled_temp(self) -> float:
        """Return the target DHW temperature based on time of day and weekday."""
        now = ebus.now()
        if 6 <= now.hour < 14:
            return 45.0
        if now.hour >= 14 and now.weekday() == 4:  # Friday
            return 60.0
        return 50.0

    def _transition(self, new_state: str) -> None:
        """Record a state change and log it."""
        if self.state == new_state: return
        self.log.extra = f"{self.state} → {new_state}"
        if self.state == "RUNNING" and new_state != "RUNNING":
            self.night_mode.run_ended()
        if new_state == "RUNNING":
            self.night_mode.run_started()
        self.state        = new_state
        self._state_since = time.monotonic_ns()

    def night_limit_reached(self) -> bool:
        return self.night_mode.limit_reached()

    def _set_active_target(self, current_temp: float, error: float) -> None:
        """Set target using the Vaillant active algorithm: mirror room error onto flow setpoint."""
        self.target = round(max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)

    def _set_idle_target(self) -> None:
        """Set target to idle temp — low enough that the pump will not run its compressor."""
        self.target = IDLE_TEMP
