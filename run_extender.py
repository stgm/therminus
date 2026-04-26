import time
from ebus import Telemetry

MIN_FLOW_TEMP = 15.0  # °C — pump's configured baseline minimum flow temperature


class RunExtender:
    """
    Run extender: keeps the pump running by nudging MinFlowTemp upward when the
    compressor is at minimum modulation but the flow temperature still overshoots
    the target.  Resets to MIN_FLOW_TEMP when no longer needed.

    When the pump is RUNNING, it may settle on lowest compressor speed,
    especially in spring and fall when there's not a lot of heat loss
    and/or there's a lot of heat from the sun.

    The pump may turn off quite quickly because the flow temperature
    soon rises above the heat curve flow temperature: the minimum
    compressor speed is already too much. But in a slow heating system
    the water then cools quite quickly again. Worst case, the pump
    starts cycling multiple times per hour.

    We can encourage the pump to keep going by following the flow
    temperature and trying to keep that as a target. This is done by
    raising the *minimum* flow temperature variable. In practice, this
    results in very long runs where the energy-integral hardly rises.
    """

    def __init__(self):
        self._extender_is_running = False
        self._started_at = None

    def is_running(self) -> bool:
        return self._extender_is_running

    def elapsed(self) -> int:
        assert self._started_at is not None
        return int(time.monotonic()) - self._started_at

    def stop(self) -> float | None:
        if self._extender_is_running:
            self._extender_is_running = False
            return MIN_FLOW_TEMP
        else:
            return None

    def run(self, temp_reached: bool, telemetry: Telemetry) -> float | None:
        assert telemetry.has_all_data()

        # Track the flow temperature carefully:
        desired_min_flow_temp = telemetry.target_flow_temp

        # Make sure extender stops when room temp reached
        # Although the heat pump can still decide to continue!
        if temp_reached and self._started_at is not None and self.elapsed() >= 1 * 60 * 60:
            desired_min_flow_temp = MIN_FLOW_TEMP
            print(
                f"[extender] stopped after {self.elapsed() / 3600.0} hours and room is good"
                f"  min={telemetry.min_flow_temp}"
            )

        # Target flow calculated by heat pump drops below the set min flow temp
        # We raised the minimum, but heat is no longer requested above it
        elif (
            telemetry.target_flow_temp < telemetry.min_flow_temp
            and telemetry.min_flow_temp > MIN_FLOW_TEMP
        ):
            desired_min_flow_temp = MIN_FLOW_TEMP
            print(
                f"[extender] reset (no longer needed)"
                f"  target={telemetry.target_flow_temp}  min={telemetry.min_flow_temp}"
            )

        # Actual flow overshoots target at minimum modulation — extend the run.
        elif (
            telemetry.flow_temp > telemetry.target_flow_temp
            and telemetry.is_compressor_running_at_min()
            and telemetry.flow_temp < telemetry.max_flow_temp
            and telemetry.target_flow_temp >= MIN_FLOW_TEMP
        ):
            if self._started_at is None:
                self._started_at = int(time.monotonic())
            desired_min_flow_temp = telemetry.flow_temp
            print(
                f"[extender] extending"
                f"  flow={telemetry.flow_temp}  target={telemetry.target_flow_temp}"
                f"  comp={telemetry.compressor_speed}%"
            )

        elif (
            not telemetry.is_compressor_running_at_min()
            and telemetry.min_flow_temp > MIN_FLOW_TEMP
        ):
            desired_min_flow_temp = max(
                MIN_FLOW_TEMP, telemetry.min_flow_temp - 0.5
            )
            print(f"[extender] toning down")

        # TODO or base on telemetry
        self._extender_is_running = desired_min_flow_temp > MIN_FLOW_TEMP
        return desired_min_flow_temp
