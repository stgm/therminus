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

import asyncio
import threading
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────
EBUSD_HOST      = "127.0.0.1"
EBUSD_PORT      = 8888

# ── Module state ───────────────────────────────────────────────────────────────
_loop           = None
last_target     = None    # last TargetTempHc value successfully written (°C)
last_write_time = None    # datetime of last successful write


@dataclass
class Telemetry:
    """
    Snapshot of pump telemetry from a single ebus read session.

    Each field carries a 'msgdef' metadata key with the ebusd message name,
    and a 'type' key for parsing. This is the single source of truth for the
    read mapping — add a field here and it is automatically read from ebusd.
    """
    target_flow_temp: float | None = field(default=None, metadata={"msgdef": "targetflowtemp",         "type": float})
    flow_temp:        float | None = field(default=None, metadata={"msgdef": "flowtemp",               "type": float})
    min_flow_temp:    float | None = field(default=None, metadata={"msgdef": "minflowtemp",            "type": float})
    max_flow_temp:    float | None = field(default=None, metadata={"msgdef": "maxflowtemp",            "type": float})
    compressor_speed: float | None = field(default=None, metadata={"msgdef": "rundatacompressorspeed", "type": float})
    valve:            str   | None = field(default=None, metadata={"msgdef": "threewayvalve",          "type": str})
    outdoor_temp:     float | None = field(default=None, metadata={"msgdef": "outdoortemp",            "type": float})

    @classmethod
    def msgdef_lookup(cls) -> dict[str, tuple[str, type]]:
        """Return {ebusd_msgdef: (field_name, type)} for all fields."""
        return {f.metadata["msgdef"]: (f.name, f.metadata["type"]) for f in dc_fields(cls)}


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


async def _make_ebus():
    """Create and return a connected, msgdef-loaded Ebus instance."""
    from pyebus import Ebus
    ebus = Ebus(EBUSD_HOST, port=EBUSD_PORT)
    await ebus.async_load_msgdefs()
    return ebus


async def _async_write(msgdef_name: str, value: float) -> None:
    """Write a single value to ebusd by msgdef name."""
    ebus = await _make_ebus()
    name = msgdef_name.lower()
    for msgdef in ebus.msgdefs:
        if msgdef.name.lower() == name:
            await ebus.async_write(msgdef, value)
            print(f"[ebus] wrote {msgdef_name} = {value}")
            return
    print(f"[ebus] WARNING: {msgdef_name} msgdef not found")


async def _async_read_telemetry() -> Telemetry:
    """Read telemetry values from ebusd in a single session."""
    ebus   = await _make_ebus()
    lookup = Telemetry.msgdef_lookup()
    result = {}
    for msgdef in ebus.msgdefs:
        entry = lookup.get(msgdef.name.lower())
        if entry:
            name, typ = entry
            msg = await ebus.async_read(msgdef)
            if msg is not None:
                try:
                    raw = msg.values[0] if hasattr(msg, 'values') else msg
                    result[name] = typ(str(raw).strip() if typ is str else raw)
                except (TypeError, ValueError, IndexError) as e:
                    print(f"[ebus] parse error for {msgdef.name}: {e}  raw={msg!r}")
    return Telemetry(**result)
