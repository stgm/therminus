"""
ebus — ebusd communication layer for Therminus.

All pyebus I/O lives here. The rest of the app calls the public functions
and reads the public attributes; it never touches pyebus directly.

Public functions:
    start()                   launch the asyncio loop thread (call once at startup)
    read_telemetry()          blocking read of pump telemetry; returns Telemetry
    write(setpoints)          write pump setpoints from a dict; None values are skipped

Public attributes:
    last_target               last room_target value successfully written (°C), or None
    last_write_time           datetime of last successful write, or None
"""

# Three-way valve position strings as reported by ebusd.
VALVE_HEATING = 'heating circuit'
VALVE_DHW     = 'warm water circuit'

import asyncio
import threading
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────
EBUSD_HOST      = "127.0.0.1"
EBUSD_PORT      = 8888
EBUSD_CIRCUIT   = "hmu"      # ebusd circuit that owns all heat pump messages

# ── Module state ───────────────────────────────────────────────────────────────
_loop           = None
_ebus           = None    # shared Ebus instance, initialized on first use
last_target     = None    # last TargetTempHc value successfully written (°C)
last_write_time = None    # datetime of last successful write


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

    def compressor_on(self) -> bool:
        """Compressor is running (speed known and > 0)."""
        return self.compressor_speed is not None and self.compressor_speed > 0

    def compressor_off(self) -> bool:
        """Compressor is confirmed stopped (speed known and == 0)."""
        return self.compressor_speed is not None and self.compressor_speed == 0

    def heating(self) -> bool:
        """Compressor is running on the heating circuit."""
        return self.compressor_on() and self.valve == VALVE_HEATING

    def making_dhw(self) -> bool:
        """Compressor is running on the domestic hot water circuit."""
        return self.compressor_on() and self.valve == VALVE_DHW

    def circuit_running(self) -> bool:
        """Building circuit circulation pump is confirmed on."""
        return self.building_circuit_flow is not None and self.building_circuit_flow > 0

    def circuit_off(self) -> bool:
        """Building circuit circulation pump is confirmed off (flow == 0)."""
        return self.building_circuit_flow is not None and self.building_circuit_flow == 0


# ── Public API ─────────────────────────────────────────────────────────────────

def start() -> None:
    """Launch the dedicated asyncio event loop in a daemon thread."""
    threading.Thread(target=_run_loop, daemon=True).start()


def read_telemetry() -> Telemetry:
    """
    Read pump telemetry from ebusd synchronously.

    Runs the async read on the shared event loop and blocks until complete.
    Returns a Telemetry with all fields None on any error.
    """
    try:
        t = _run_async(_async_read_telemetry())
        print(f"[ebus] flow={t.flow_temp}°C  comp={t.compressor_speed}%"
              f"  valve={t.valve}  outdoor={t.outdoor_temp}°C")
        return t
    except Exception as e:
        print(f"[ebus] read error: {e}")
        return Telemetry()


def write(setpoints: dict) -> None:
    """Write pump setpoints from a dict. Keys with None values are skipped."""
    global last_target, last_write_time
    try:
        if (v := setpoints.get("room_target")) is not None:
            _run_async(_async_write("TargetTempHc", v))
            last_target     = v
            last_write_time = datetime.now()
        if (v := setpoints.get("min_flow_temp")) is not None:
            _run_async(_async_write("MinFlowTemp", v))
    except Exception as e:
        print(f"[ebus] write error: {e}")


# ── Internals ──────────────────────────────────────────────────────────────────

def _run_loop() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _run_async(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=15)


async def _get_ebus():
    """Return the shared Ebus instance, creating it on first call."""
    global _ebus
    if _ebus is None:
        from pyebus import Ebus
        _ebus = Ebus(EBUSD_HOST, port=EBUSD_PORT)
        await _ebus.async_load_msgdefs()
        print("[ebus] definitions loaded from ebusd")
    return _ebus


async def _async_write(msgdef_name: str, value: float) -> None:
    """Write a single value to ebusd by circuit + message name."""
    ebus   = await _get_ebus()
    msgdef = ebus.msgdefs.get(EBUSD_CIRCUIT, msgdef_name)
    if msgdef is None:
        print(f"[ebus] WARNING: {EBUSD_CIRCUIT}/{msgdef_name} msgdef not found")
        return
    await ebus.async_write(msgdef, value)
    print(f"[ebus] wrote {EBUSD_CIRCUIT}/{msgdef_name} = {value}")


async def _async_read_telemetry() -> Telemetry:
    """Read telemetry values from ebusd in a single session."""
    ebus   = await _get_ebus()
    result = {}
    for f in dc_fields(Telemetry):
        circuit = f.metadata["circuit"]
        name    = f.metadata["msgdef"]
        msgdef  = ebus.msgdefs.get(circuit, name)
        if msgdef is None:
            continue
        msg = await ebus.async_read(msgdef)
        if msg is not None:
            try:
                typ = f.metadata["type"]
                raw = msg.values[0] if hasattr(msg, 'values') else msg
                result[f.name] = typ(str(raw).strip() if typ is str else raw)
            except (TypeError, ValueError, IndexError) as e:
                print(f"[ebus] parse error for {circuit}/{name}: {e}  raw={msg!r}")
    return Telemetry(**result)
