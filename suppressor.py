import time

from ebus import Telemetry

# During suppress, briefly raise to IDLE_TEMP once per cycle so the
# circulation pump can run without the compressor firing.
SUPPRESS_LEN = 60 * 60  # seconds — full suppress cycle length
SUPPRESS_PAUSE = 5 * 60  # seconds — circulation burst per cycle

# Outdoor temperature above which SUPPRESSED mode activates (room warm + mild outside).
SUPPRESS_OUTDOOR_MIN = 10.0  # °C

SUPPRESS_OFF_MIN = (
    30 * 60
)  # seconds — minimum time suppress stays off before re-activating


class CirculationSuppressor:
    def __init__(self):
        self.reset()

    def reset(self):
        self._suppressing = False
        self._started_at = 0
        self._stopped_at = 0

    def check(self, telemetry: Telemetry) -> bool:
        now = int(time.monotonic())
        time_since_start = now - self._started_at
        time_since_last = now - self._stopped_at

        # although we require room set point to be 15º, here we have a few
        # extra requirements for the suppression to kick in
        min_outdoor_reached = telemetry.outdoor_temp >= SUPPRESS_OUTDOOR_MIN
        in_pause = self._suppressing and time_since_start >= SUPPRESS_LEN
        comfortable_flow_temp = telemetry.flow_temp >= telemetry.target_flow_temp + 1.0
        suppress_allowed = min_outdoor_reached and (comfortable_flow_temp or in_pause)

        # toggle suppression state (otherwise it just stays the same)
        if suppress_allowed and not self._suppressing:
            if time_since_last >= SUPPRESS_OFF_MIN:
                self._suppressing = True
                self._started_at = now
        elif not suppress_allowed and self._suppressing:
            self._suppressing = False
            self._stopped_at = now

        if not self._suppressing:
            return False
        elif time_since_start >= SUPPRESS_LEN + SUPPRESS_PAUSE:
            self._started_at = now  # reset after pause
            return True
        elif time_since_start >= SUPPRESS_LEN:
            return False
        else:
            return True
