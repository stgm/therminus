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

# During suppress, briefly raise to IDLE_TEMP once per cycle so the
# circulation pump can run without the compressor firing.
SUPPRESS_CYCLE         = 60 * 60   # seconds — full suppress cycle length
SUPPRESS_CIRC_DURATION =  5 * 60   # seconds — circulation burst per cycle

# Outdoor temperature above which SUPPRESSED mode activates (room warm + mild outside).
SUPPRESS_OUTDOOR_MIN  = 10.0   # °C
SUPPRESS_OFF_MIN      = 30 * 60  # seconds — minimum time suppress stays off before re-activating

# Run extender: keeps the pump running by nudging MinFlowTemp upward when the
# compressor is at minimum modulation but the flow temperature still overshoots
# the target.  Resets to MIN_FLOW_TEMP when no longer needed.
MIN_FLOW_TEMP        = 15.0   # °C — pump's configured baseline minimum flow temperature
COMPRESSOR_MIN_SPEED = 30.0   # % — minimum modulation speed (pump cannot go lower)
COMPRESSOR_MIN_TOL   =  0.3   # % — tolerance band around minimum modulation


def _pump_state(telemetry) -> str:
    """
    Derive the current pump hardware state from telemetry — no history, no side effects.

    Priority order ensures unambiguous classification:
      dhw          compressor on + DHW valve (takes priority over all)
      dormant      building circuit flow == 0
      heating      compressor on + heating valve
      circulating  circuit running, compressor off
    """
    if telemetry.making_dhw():      return "dhw"
    if telemetry.circuit_off():     return "dormant"
    if telemetry.heating():         return "heating"
    if telemetry.circuit_running(): return "circulating"
    return "dormant"                # telemetry not yet available


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
        self._t_min_rest  = T_MIN_REST
        self._extender_is_running = False
        self._suppressing = False
        self._suppress_off_at = 0.0  # monotonic seconds

        # Night limiter (22:00–08:00)
        self.night_mode = NightMode()

        # Last known sensor value
        self._current_temp     = None

    def current_temp(self):
        return self._current_temp

    def update_temp(self, temp: float):
        """Update the known room temperature without running a full tick."""
        self._current_temp = temp

    def error(self):
        return SETPOINT - self._current_temp

    def elapsed(self) -> float:
        return (time.monotonic_ns() - self._state_since) / 1e9

    def temp_above_upper_band(self) -> bool:
        return self._current_temp > self._setpoint + self._band

    def temp_below_lower_band(self) -> bool:
        return self._current_temp < self._setpoint - self._band

    def suppress_active(self, telemetry: ebus.Telemetry) -> bool:
        return (
            telemetry.outdoor_temp is not None
            and telemetry.outdoor_temp >= SUPPRESS_OUTDOOR_MIN
            and self._current_temp >= (SETPOINT - BAND)
            and telemetry.flow_temp >= telemetry.target_flow_temp + 1.0
        )

    def tick(self, telemetry, weather_cache: dict | None = None) -> dict:
        """
        Advance the state machine one step.

        Reads current conditions; may transition state; always updates
        self.target with the setpoint the pump should receive this tick.
        Returns {"room_target": float, "min_flow_temp": float | None}.
        """
        if self._current_temp is None:
            return {}

        # Night window: 22:00–08:00. Gets the current hour to decide when to start/stop.
        # When starting, it takes weather forecast data + local measurements.
        self.night_mode.tick(
            ebus.now().hour,
            on_activate=lambda: (telemetry.outdoor_temp, weather_cache or {}, self._current_temp),
        )

        self.pump = pump = _pump_state(telemetry)

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
            # disallows heating for some time after a run
            if self.elapsed() >= self._t_min_rest:
                self._transition("IDLE")

        elif self.state == "IDLE":
            if pump == "heating":
                self._transition("RUNNING")

        elif self.state == "RUNNING":
            if pump != "heating":
                self._t_min_rest = T_MIN_REST
                self._transition("RESTING")

        # ── Phase 2: compute room target ──────────────────────────────────────

        # keep artificially low when heating limit is reached during the night
        # this is for colder nights and warmer days, it's fine to have it a bit
        # colder during the mornings
        if self.night_mode.limit_reached():
            self._set_idle_target()
            print(f"[controller] night limit reached"
                    f"  total={self.night_mode.run_total()/3600:.2f}h"
                    f"  limit={self.night_mode.limit_hours:.1f}h")

        # keep the target artificially low for some time after a run, to force a pause
        elif self.state == "RESTING":
            self._set_idle_target()

        # regulate a little based on room temperature: the heat pump combines
        # with outside temp and heat curve to calculate required flow temp
        else:
            now = time.monotonic()
            if self._suppressing and not self.suppress_active(telemetry):
                self._suppressing = False
                self._suppress_off_at = now
            elif not self._suppressing and self.suppress_active(telemetry):
                if now - self._suppress_off_at >= SUPPRESS_OFF_MIN:
                    self._suppressing = True

            if self._suppressing and self.state == "IDLE":
                cycle_pos = self.elapsed() % SUPPRESS_CYCLE
                if cycle_pos >= SUPPRESS_CYCLE - SUPPRESS_CIRC_DURATION:
                    print("[room target calculation] suppress circulation burst 15º")
                    self.target = IDLE_TEMP
                else:
                    print("[room target calculation] suppressing 10º")
                    self.target = SUPPRESSED_TEMP
            elif self._current_temp > (SETPOINT + BAND):
                # Room satisfied — keep target low so pump won't fire compressor.
                # Stay at 15 (not 10) while RUNNING so we don't risk circuit_off
                # before the compressor stops naturally.
                print("[room target calculation] idle 15º")
                self._set_idle_target()
            else:
                # track room temperature using the "active" strategy
                print("[room target calculation] active strategy")
                self._set_active_target(self._current_temp, self.error())

        # ── Phase 3: run extender ─────────────────────────────────────────────
        #
        # When the pump is RUNNING, it may settle on lowest compressor speed,
        # especially in spring and fall when there's not a lot of heat loss
        # and/or there's a lot of heat from the sun.
        #
        # The pump may turn off quite quickly because the flow temperature
        # soon rises above the heat curve flow temperature: the minimum
        # compressor speed is already too much. But in a slow heating system
        # the water then cools quite quickly again. Worst case, the pump
        # starts cycling multiple times per hour.
        #
        # We can encourage the pump to keep going by following the flow
        # temperature and trying to keep that as a target. This is done by
        # raising the *minimum* flow temperature variable. In practice, this
        # results in very long runs where the energy-integral hardly rises.

        # Extender can only run when RUNNING, in all other states we remove
        # the raised minimum and set the minimum to a safe default (15ºC).
        if self.state != "RUNNING":
            self._extender_is_running = False
            return {
                "room_target": self.target,
                "min_flow_temp": MIN_FLOW_TEMP,
                "dhw_target": self.dhw_scheduled_temp()
            }

        # Track the flow temperature carefully:
        desired_min_flow_temp = None
        if self._has_run_extender_data(telemetry):
            # Make sure extender stops when room temp reached
            # Although the heat pump can still decide to continue!
            if (self._current_temp >= SETPOINT and self.elapsed() >= 1.5 * 60 * 60):
                desired_min_flow_temp = MIN_FLOW_TEMP
                print(f"[extender] stopped after {self.elapsed()/3600.0} hours and room is good"
                      f"  min={telemetry.min_flow_temp}")

            # Target flow calculated by heat pump drops below the set min flow temp
            # We raised the minimum, but heat is no longer requested above it
            elif (telemetry.target_flow_temp < telemetry.min_flow_temp
                  and telemetry.min_flow_temp > MIN_FLOW_TEMP):
                desired_min_flow_temp = MIN_FLOW_TEMP
                print(f"[extender] reset (no longer needed)"
                      f"  target={telemetry.target_flow_temp}  min={telemetry.min_flow_temp}")

            # Actual flow overshoots target at minimum modulation — extend the run.
            elif (telemetry.flow_temp > telemetry.target_flow_temp
                  and self.elapsed() > 20 * 60 # 20 minutes
                  and self._is_compressor_running_at_min(telemetry)
                  and telemetry.flow_temp < telemetry.max_flow_temp
                  and telemetry.target_flow_temp >= MIN_FLOW_TEMP):
                desired_min_flow_temp = telemetry.flow_temp
                print(f"[extender] extending"
                      f"  flow={telemetry.flow_temp}  target={telemetry.target_flow_temp}"
                      f"  comp={telemetry.compressor_speed}%")

            elif (not self._is_compressor_running_at_min(telemetry)
                  and telemetry.min_flow_temp > MIN_FLOW_TEMP):
                desired_min_flow_temp = max(MIN_FLOW_TEMP, telemetry.min_flow_temp - 0.5)
                print(f"[extender] toning down")

        self._extender_is_running = telemetry.min_flow_temp > MIN_FLOW_TEMP
        return {
            "room_target": self.target,
            "min_flow_temp": desired_min_flow_temp,
            "dhw_target": self.dhw_scheduled_temp()
        }

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
        print(f"[controller] {self.state} → {new_state}")
        if self.state == "RUNNING" and new_state != "RUNNING":
            self.night_mode.run_ended()
        if new_state == "RUNNING":
            self.night_mode.run_started()
        self.state        = new_state
        self._state_since = time.monotonic_ns()

    def _has_run_extender_data(self, telemetry) -> bool:
        """True when all telemetry fields required by the run extender are available."""
        return None not in (
            telemetry.compressor_speed, telemetry.flow_temp, telemetry.target_flow_temp,
            telemetry.min_flow_temp, telemetry.max_flow_temp)

    def _is_compressor_running_at_min(self, telemetry) -> bool:
        """True when the compressor is on and running at its minimum modulation speed."""
        return (telemetry.compressor_on()
                and abs(telemetry.compressor_speed - COMPRESSOR_MIN_SPEED) <= COMPRESSOR_MIN_TOL)

    def night_limit_reached(self) -> bool:
        return self.night_mode.limit_reached()

    def is_extender_running(self) -> bool:
        """True when the run extender has raised MinFlowTemp above the baseline."""
        return self._extender_is_running

    def _set_active_target(self, current_temp: float, error: float) -> None:
        """Set target using the Vaillant active algorithm: mirror room error onto flow setpoint."""
        self.target = round(max(TARGET_MIN, min(TARGET_MAX, SETPOINT + KP * error)), 1)

    def _set_idle_target(self) -> None:
        """Set target to idle temp — low enough that the pump will not run its compressor."""
        self.target = IDLE_TEMP

class NightMode:
    def __init__(self):
        self.limit_hours: float | None = None
        self._accumulated: float = 0.0
        self._run_start: float | None = None

    def tick(self, hour: int, on_activate) -> None:
        in_window = hour >= 22 or hour < 8
        if in_window and not self.is_active():
            self.activate(*on_activate())
        elif not in_window and self.is_active():
            self.deactivate()

    def activate(self,
                 outdoor_temp: float | None,
                 weather_cache: dict,
                 room_temp: float | None) -> None:
        """
        Activate the overnight heating limiter. Called once around 22:00.

        Calculates the allowed heating hours from the formula:
            night_loss_compensation  = 13 - (outdoor_temp + forecast_low_tomorrow) / 2
            day_loss_precompensation = 16 - forecast_high_tomorrow
            compensation_for_feeling = sgn(forecast_high_today - forecast_high_tomorrow)
            room_overshoot_penalty   = 21 - room_temp
            limit = max(0, sum of above)  hours
        """
        forecast_low_tomorrow  = weather_cache.get('forecast_low_tomorrow')
        forecast_high_tomorrow = weather_cache.get('forecast_high_tomorrow')
        forecast_high_today    = weather_cache.get('forecast_high_today')

        out  = outdoor_temp           if outdoor_temp           is not None else 5.0
        low  = forecast_low_tomorrow  if forecast_low_tomorrow  is not None else 5.0
        high = forecast_high_tomorrow if forecast_high_tomorrow is not None else 10.0
        rt   = room_temp              if room_temp              is not None else 21.0

        night_loss   = 13 - (out + low) / 2
        day_loss     = 16 - high
        if forecast_high_today is not None and forecast_high_tomorrow is not None:
            diff = forecast_high_today - forecast_high_tomorrow
            feeling = math.copysign(1.0, diff) if diff != 0 else 0.0
        else:
            feeling = 0.0
        room_penalty = 21 - rt
        limit = max(0.0, night_loss + day_loss + feeling + room_penalty)

        self.limit_hours  = limit
        self._accumulated = 0.0
        self._run_start   = None
        print(f"[controller] night mode:"
              f"  outdoor={outdoor_temp} low={forecast_low_tomorrow}"
              f"  high_today={forecast_high_today} high_tom={forecast_high_tomorrow}"
              f"  room={rt:.1f}"
              f"  night_loss={night_loss:.1f} day_loss={day_loss:.1f}"
              f"  feeling={feeling:.0f} room_penalty={room_penalty:.1f}"
              f"  limit={limit:.1f}h")

    def deactivate(self) -> None:
        """Deactivate the overnight limiter. Called at 08:00."""
        self.limit_hours  = None
        self._accumulated = 0.0
        self._run_start   = None
        print("[controller] night mode: off")

    def is_active(self) -> bool:
        return self.limit_hours is not None

    def run_started(self) -> None:
        self._run_start = time.monotonic_ns()

    def run_ended(self) -> None:
        if self._run_start is not None:
            self._accumulated += (time.monotonic_ns() - self._run_start) / 1e9
            self._run_start = None

    def run_total(self) -> float:
        """Accumulated RUNNING time tonight, including the current in-progress run."""
        total = self._accumulated
        if self._run_start is not None:
            total += (time.monotonic_ns() - self._run_start) / 1e9
        return total

    def limit_reached(self) -> bool:
        return self.is_active() and self.run_total() >= self.limit_hours * 3600
