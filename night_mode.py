import math
import time

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
