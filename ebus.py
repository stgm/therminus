"""
ebus — ebusd communication layer for Therminus.

All pyebus I/O lives here. The rest of the app calls the public functions
and reads the public attributes; it never touches pyebus directly.

Also owns wall-clock time for the app: sync_time() reads the pump's
DCF77-synced clock once at startup and infers the UTC offset; now()
returns a tz-aware datetime in that inferred zone.

Public functions:
    start()                   launch the asyncio loop thread (call once at startup)
    sync_time()               read DCF time once, infer tz from it (call after start())
    now()                     current wall-clock as a tz-aware datetime
    read_telemetry()          blocking read of pump telemetry; returns Telemetry
    write(setpoints)          write pump setpoints from a dict; None values are skipped
    bus_healthy()             False once telemetry reads have failed repeatedly
    consecutive_failures()    number of failed telemetry reads in a row

Public attributes:
    last_target               last room_target value successfully written (°C), or None
    last_write_time           datetime of last successful write, or None
    time_synced               True once sync_time() has succeeded

Failures are never raised at the caller: read_telemetry() returns an empty
Telemetry and write() logs and returns. Callers that need to know whether the
bus is alive ask bus_healthy().
"""

# Three-way valve position strings as reported by ebusd.
VALVE_HEATING = 'heating circuit'
VALVE_DHW     = 'warm water circuit'

import asyncio
import threading
import zoneinfo
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timedelta, timezone

# ── Configuration ──────────────────────────────────────────────────────────────
EBUSD_HOST      = "127.0.0.1"
EBUSD_PORT      = 8888
EBUSD_CIRCUIT   = "hmu"      # ebusd circuit that owns all heat pump messages

COMPRESSOR_MIN_SPEED = 30.0   # % — minimum modulation speed (pump cannot go lower)
COMPRESSOR_MIN_TOL   =  0.3   # % — tolerance band around minimum modulation

# Timeout ladder. Each level is a backstop for the one below it, so a stalled
# ebusd cannot wedge the caller:
#   socket  — pyebus gives up on a single request that gets no response
#   op      — the whole coroutine is cancelled inside the asyncio loop, where
#             cleanup still runs
#   call    — the calling thread stops waiting; only reached if the loop itself
#             is wedged, so it must be larger than the op timeout
EBUS_SOCKET_TIMEOUT = 5    # s — per-request socket timeout handed to pyebus
EBUS_OP_TIMEOUT     = 30   # s — in-loop budget for one full operation
EBUS_CALL_TIMEOUT   = 35   # s — thread-side backstop

BUS_FAULT_AFTER = 3   # consecutive failed telemetry reads before the UI shows a fault

# ── Module state ───────────────────────────────────────────────────────────────
_loop           = None
_ebus           = None    # Ebus instance from the most recent call, or None
_tz             = zoneinfo.ZoneInfo("Europe/Amsterdam")  # replaced by sync_time()
last_target     = None    # last TargetTempHc value successfully written (°C)
last_write_time = None    # datetime of last successful write
time_synced     = False   # True once sync_time() has succeeded
_failures       = 0       # consecutive failed read_telemetry() calls


@dataclass
class Telemetry:
    """
    Snapshot of pump telemetry from a single ebus read session.

    Each field carries 'circuit', 'msgdef', and 'type' metadata keys.
    'circuit' and 'msgdef' together uniquely identify the ebusd message.
    This is the single source of truth for the read mapping — add a field
    here and it is automatically read from ebusd.
    """
    target_flow_temp:      float | None = field(default=None, metadata={"circuit": "hmu",   "msgdef": "TargetFlowTemp",         "type": float})
    flow_temp:             float | None = field(default=None, metadata={"circuit": "hmu",   "msgdef": "FlowTemp",               "type": float})
    min_flow_temp:         float | None = field(default=None, metadata={"circuit": "hmu",   "msgdef": "MinFlowTemp",            "type": float})
    max_flow_temp:         float | None = field(default=None, metadata={"circuit": "hmu",   "msgdef": "MaxFlowTemp",            "type": float})
    compressor_speed:      float | None = field(default=None, metadata={"circuit": "hmu",   "msgdef": "RunDataCompressorSpeed", "type": float})
    valve:                 str   | None = field(default=None, metadata={"circuit": "vwzio", "msgdef": "ThreeWayValve",          "type": str})
    outdoor_temp:          float | None = field(default=None, metadata={"circuit": "vwzio", "msgdef": "OutdoorTemp",            "type": float})
    building_circuit_flow: float | None = field(default=None, metadata={"circuit": "hmu",   "msgdef": "BuildingCircuitFlow",    "type": float})

    def has_all_data(self) -> bool:
        return None not in (
            self.compressor_speed, self.flow_temp, self.target_flow_temp,
            self.min_flow_temp, self.max_flow_temp, self.valve)

    def is_compressor_on(self) -> bool:
        """Compressor is running (speed known and > 0)."""
        return self.compressor_speed is not None and self.compressor_speed > 0

    def is_compressor_off(self) -> bool:
        """Compressor is confirmed stopped (speed known and == 0)."""
        return self.compressor_speed is not None and self.compressor_speed == 0

    def is_compressor_running_at_min(self) -> bool:
        """True when the compressor is on and running at its minimum modulation speed."""
        if self.compressor_speed is None:
            return False
        return (self.is_compressor_on()
                and abs(self.compressor_speed - COMPRESSOR_MIN_SPEED) <= COMPRESSOR_MIN_TOL)

    def is_heating(self) -> bool:
        """Compressor is running on the heating circuit."""
        return self.is_compressor_on() and self.valve == VALVE_HEATING

    def is_making_dhw(self) -> bool:
        """Compressor is running on the domestic hot water circuit."""
        return self.is_compressor_on() and self.valve == VALVE_DHW

    def circuit_running(self) -> bool:
        """Building circuit circulation pump is confirmed on."""
        return self.building_circuit_flow is not None and self.building_circuit_flow > 0

    def circuit_off(self) -> bool:
        """Building circuit circulation pump is confirmed off (flow == 0)."""
        return self.building_circuit_flow is not None and self.building_circuit_flow == 0


# ── Public API ─────────────────────────────────────────────────────────────────

def start() -> None:
    """Launch the dedicated asyncio event loop in a daemon thread."""
    ready = threading.Event()
    threading.Thread(target=_run_loop, args=(ready,), daemon=True).start()
    ready.wait()


def now() -> datetime:
    """Current wall-clock time as a tz-aware datetime.

    Timezone is inferred from the pump's DCF clock at startup via
    sync_time(); falls back to Europe/Amsterdam if that read never
    succeeded.
    """
    return datetime.now(_tz)


def sync_time() -> bool:
    """Read DCF time from vwzio/OutsideReceiver once and set the module tz.

    Computes offset_hours = round((DCF wall-clock) - (system UTC)), in
    hours, and uses that fixed offset as the app-wide timezone. Returns
    True on success; on any failure logs the reason and leaves the
    fallback tz in place.
    """
    global _tz, time_synced
    try:
        fields = _run_async(_async_read_outside_receiver())
    except Exception as e:
        print(f"[ebus] time sync read error: {e!r}")
        return False
    if not fields:
        print("[ebus] time sync: OutsideReceiver read failed")
        return False
    state = str(fields.get('dcfstate', '')).strip()
    if state not in ('ok', 'valid'):
        print(f"[ebus] DCF not valid ({state or 'unknown'}); "
              f"falling back to Europe/Amsterdam")
        return False
    bdate = str(fields.get('bdate', '')).strip()
    btime = str(fields.get('btime', '')).strip()
    try:
        dcf = datetime.strptime(f"{bdate} {btime}", "%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        print(f"[ebus] DCF parse error: {e}  date={bdate!r} time={btime!r}")
        return False
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    offset_hours = round((dcf - utc_now).total_seconds() / 3600)
    _tz = timezone(timedelta(hours=offset_hours))
    time_synced = True
    sign = '+' if offset_hours >= 0 else '-'
    print(f"[ebus] determined timezone {sign}{abs(offset_hours):02d} hours")
    return True


def read_telemetry() -> Telemetry:
    """
    Read pump telemetry from ebusd synchronously.

    Runs the async read on the shared event loop and blocks until complete.
    Returns a Telemetry with all fields None on any error.

    Errors are counted rather than raised, so one bad read cannot take the
    control loop down. Every failure is logged and the running count is
    published through consecutive_failures().
    """
    global _failures
    try:
        t = _run_async(_async_read_telemetry())
    except Exception as e:
        _failures += 1
        print(f"[ebus] read error ({_failures} in a row): {e!r}")
        return Telemetry()
    if _failures:
        print(f"[ebus] telemetry recovered after {_failures} failed reads")
        _failures = 0
    return t


def bus_healthy() -> bool:
    """False once telemetry reads have failed BUS_FAULT_AFTER times in a row.

    One failure is one missed control tick, so the threshold is also a
    duration: BUS_FAULT_AFTER * CONTROL_DT seconds without contact.
    """
    return _failures < BUS_FAULT_AFTER


def consecutive_failures() -> int:
    """Number of telemetry reads that have failed in a row. 0 when healthy."""
    return _failures


def write(setpoints: dict) -> None:
    """Write pump setpoints from a dict. Keys with None values are skipped."""
    global last_target, last_write_time
    try:
        if (v := setpoints.get("room_target")) is not None:
            _run_async(_async_write("TargetTempHc", v))
            last_target     = v
            last_write_time = now()
        if (v := setpoints.get("min_flow_temp")) is not None:
            _run_async(_async_write("MinFlowTemp", v))
        if (v := setpoints.get("dhw_target")) is not None:
            _run_async(_async_write("TargetTempHwc", v))
    except Exception as e:
        print(f"[ebus] write error: {e!r}")


# ── Internals ──────────────────────────────────────────────────────────────────

def _run_loop(ready: threading.Event | None = None) -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    if ready:
        ready.set()
    _loop.run_forever()


def _run_async(coro):
    """Run a coroutine on the ebus loop from another thread and wait for it.

    The coroutine is bounded by EBUS_OP_TIMEOUT inside the loop, so it
    normally unwinds there and its cleanup runs. EBUS_CALL_TIMEOUT is only
    reached if the loop itself is stuck; in that case the future is cancelled
    so the work is not left running behind the next call.
    """
    fut = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, EBUS_OP_TIMEOUT), _loop)
    try:
        return fut.result(timeout=EBUS_CALL_TIMEOUT)
    except TimeoutError:
        # asyncio.TimeoutError is the same class as the builtin and neither
        # carries a message, so name the level that gave up. A call timeout
        # means the loop itself is stuck, which is far worse than a slow bus.
        if fut.done():
            raise TimeoutError(f"ebus operation exceeded {EBUS_OP_TIMEOUT}s") from None
        fut.cancel()
        raise TimeoutError(f"ebus loop did not respond within {EBUS_CALL_TIMEOUT}s") from None


async def _get_ebus():
    """Return a freshly connected Ebus instance.

    We deliberately do not cache: pyebus can get confused after a period of no
    signal, and reconnecting each call is the reliable way out of that. The
    previous instance is disconnected first so sockets don't accumulate —
    including the one left behind by a cancelled call.
    """
    global _ebus
    if _ebus is not None:
        stale, _ebus = _ebus, None
        try:
            # Bounded: async_disconnect() waits on wait_closed(), which pyebus
            # does not time out, and a dead peer would otherwise spend this
            # call's whole budget closing the previous one.
            await asyncio.wait_for(stale.connection.async_disconnect(),
                                   EBUS_SOCKET_TIMEOUT)
        except Exception as e:
            print(f"[ebus] disconnect error: {e!r}")
    from pyebus import Ebus
    # Published before the load so a failed or cancelled load still gets
    # disconnected by the next call.
    _ebus = Ebus(EBUSD_HOST, port=EBUSD_PORT, timeout=EBUS_SOCKET_TIMEOUT)
    await _ebus.async_load_msgdefs()
    return _ebus


async def _async_write(msgdef_name: str, value: float) -> None:
    """Write a single value to ebusd by circuit + message name."""
    ebus   = await _get_ebus()
    msgdef = ebus.msgdefs.get(EBUSD_CIRCUIT, msgdef_name)
    if msgdef is None:
        print(f"[ebus] WARNING: {EBUSD_CIRCUIT}/{msgdef_name} msgdef not found")
        return
    await ebus.async_write(msgdef, value)
    # print(f"[ebus] wrote {EBUSD_CIRCUIT}/{msgdef_name} = {value}")


async def _async_read_outside_receiver() -> dict | None:
    """Read vwzio/OutsideReceiver — a single multi-field message.

    Returns a {field_name: raw_value} dict, or None on failure. Unlike
    Telemetry reads (one ebus message per field), this message carries
    dcfstate, btime, bdate, and temp as sibling fields of one message.
    """
    ebus = await _get_ebus()
    msgdef = ebus.msgdefs.get("vwzio", "OutsideReceiver")
    if msgdef is None:
        print("[ebus] vwzio/OutsideReceiver msgdef not found")
        return None
    msg = await ebus.async_read(msgdef)
    if msg is None:
        return None
    result: dict = {}
    try:
        for idx, fielddef in enumerate(msgdef.fields):
            name = getattr(fielddef, 'name', None)
            if name:
                result[name] = msg.values[idx]
    except (AttributeError, IndexError) as e:
        print(f"[ebus] OutsideReceiver field decode error: {e}")
        return None
    return result


async def _async_read_telemetry() -> Telemetry:
    """Read telemetry values from ebusd in a single session.

    One unreadable message costs one field, not the whole read: a connection
    error partway through used to discard everything read so far. The
    controller is gated on has_all_data(), so partial results never drive
    control — they only keep the logs and the other fields useful.

    CancelledError is a BaseException and still propagates, so the in-loop
    timeout in _run_async keeps working.
    """
    ebus   = await _get_ebus()
    result = {}
    for f in dc_fields(Telemetry):
        circuit = f.metadata["circuit"]
        name    = f.metadata["msgdef"]
        msgdef  = ebus.msgdefs.get(circuit, name)
        if msgdef is None:
            continue
        try:
            msg = await ebus.async_read(msgdef)
            if msg is None:
                continue
            if not getattr(msg, 'valid', True):
                # pyebus returns a BrokenMsg instead of raising; its .error
                # says why (e.g. 'SYN received'), which beats the
                # 'tuple index out of range' we used to report.
                print(f"[ebus] {circuit}/{name} unreadable: {msg.error}")
                continue
            typ = f.metadata["type"]
            raw = msg.values[0] if hasattr(msg, 'values') else msg
            result[f.name] = typ(str(raw).strip() if typ is str else raw)
        except Exception as e:
            print(f"[ebus] read error for {circuit}/{name}: {e!r}")
    return Telemetry(**result)
