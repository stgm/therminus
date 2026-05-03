import time

from ebus import Telemetry

# Outdoor temperature above which we _may_ suppress.
SUPPRESS_OUTDOOR_MIN = 10.0

# Outdoor above which we _always_ suppress.
SUPPRESS_OUTDOOR_ALWAYS = 15.0

# During suppress, briefly raise to IDLE_TEMP once per cycle so the
# circulation pump can run without the compressor firing.
SUPPRESS_LEN = 60 * 60  # seconds — full suppress cycle length
SUPPRESS_PAUSE = 5 * 60  # seconds — circulation burst per cycle

# Minimum time that suppress stays off before re-activating.
SUPPRESS_OFF_MIN = 30 * 60

# Stops circulation when it's generally warm, by setting the target
# room temperature to 10ºC.
#
# Note: when re-enabling circulation, the heat pump sets the energy
# integral exactly to the lowest limit, so if the water near the
# heat pump gets very cool, the flow sensor notices and the energy
# integral drops goes down, which immediately starts the compressor.
# And if the building water circuit is still reasonably high, it
# will also very quickly turn of the compressor again.
#
# This is why the suppression algorithm briefly restarts the circuit
# from time to time, to make sure this doesn't happen.

_suppressing = False
_started_at = 0  # to track when pause is needed
_stopped_at = 0  # to track minimum waiting time after stop
_suppress_entry_diff: float | None = None  # flow_temp - target_flow_temp when suppression last started


def reset():
    global _suppressing, _started_at, _stopped_at, _suppress_entry_diff
    _suppressing = False
    _started_at = 0
    _stopped_at = 0
    _suppress_entry_diff = None


def check(telemetry: Telemetry) -> bool:
    global _suppressing, _started_at, _stopped_at, _suppress_entry_diff

    now = int(time.monotonic())
    time_since_start = now - _started_at
    time_since_last = now - _stopped_at

    if telemetry.outdoor_temp >= SUPPRESS_OUTDOOR_ALWAYS:
        return True

    # Minimum requirement for the suppressor to be asked to check
    # is that room temp is enough. But we also check a few other
    # things:

    # 1. outdoor temp should be high enough or we never consider
    min_outdoor_reached = telemetry.outdoor_temp >= SUPPRESS_OUTDOOR_MIN

    # 2. we need the flow sensor in the outside heat pump to stay
    #    some margin above target, otherwise we stop suppressing
    comfortable_flow_temp = telemetry.flow_temp >= telemetry.target_flow_temp + 1.0

    # 3. If we have a suppression pause (re-enabling circuit flow
    #    for a while) we ignore requirement #2
    in_pause = _suppressing and time_since_start >= SUPPRESS_LEN

    suppress_allowed = min_outdoor_reached and (comfortable_flow_temp or in_pause)

    # toggle suppression state (otherwise it just stays the same)
    if suppress_allowed and not _suppressing and time_since_last >= SUPPRESS_OFF_MIN:
        current_diff = telemetry.flow_temp - telemetry.target_flow_temp
        if _suppress_entry_diff is None or current_diff > _suppress_entry_diff:
            _suppressing = True
            _started_at = now
            _suppress_entry_diff = current_diff
    elif not suppress_allowed and _suppressing:
        _suppressing = False
        _stopped_at = now

    if not _suppressing:
        return False
    elif time_since_start >= SUPPRESS_LEN + SUPPRESS_PAUSE:
        # reset timer after pause
        _started_at = now
        return True
    elif time_since_start >= SUPPRESS_LEN:
        # start of pause
        return False
    else:
        return True
