import json
from collections import deque
from pathlib import Path

import ebus


class DayHistory:
    def __init__(
        self,
        history_points: int = 1440,
        max_events: int = 2000,
        history_file: Path = Path("history.json"),
    ):
        self.history_file = history_file
        self.room_history: deque[dict] = deque(maxlen=history_points)
        self.state_events: deque[dict] = deque(maxlen=max_events)
        self._prev_badge_cls: str | None = None

    def append_room_temp(self, ts: str, value: float) -> None:
        self.room_history.append({"ts": ts, "value": value})

    def record_state_event(self, badge_cls: str) -> dict | None:
        if badge_cls == self._prev_badge_cls:
            return None
        self._prev_badge_cls = badge_cls
        event = {"ts": ebus.now().isoformat(timespec="seconds"), "state": badge_cls}
        self.state_events.append(event)
        return event

    def save(self) -> None:
        try:
            self.history_file.write_text(json.dumps({
                "date": ebus.now().strftime("%Y-%m-%d"),
                "room_history": list(self.room_history),
                "state_events": list(self.state_events),
            }))
        except Exception as e:
            print(f"[therminus] history save error: {e}")

    def load(self) -> None:
        try:
            data = json.loads(self.history_file.read_text())
            if data.get("date") != ebus.now().strftime("%Y-%m-%d"):
                return
            for p in data.get("room_history", []):
                self.room_history.append(p)
            for e in data.get("state_events", []):
                self.state_events.append(e)
            if self.state_events:
                self._prev_badge_cls = self.state_events[-1]["state"]
            print(f"[therminus] loaded history: {len(self.room_history)} temp points, {len(self.state_events)} state events")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[therminus] history load error: {e}")
