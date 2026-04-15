"""
weather — Open-Meteo weather fetcher for Therminus.

Fetches current conditions for a fixed location and caches the result.
Night-appropriate icons are selected for clear/partly-cloudy conditions.

Public functions:
    fetch(on_update)    fetch current weather; calls on_update(icon, desc) if changed

Public attributes:
    cache               dict {icon, desc, fetched_at} or None if not yet fetched
    INTERVAL            seconds between refreshes (for the background scheduler)
"""

import gzip
import json
import ssl
import time
import urllib.request
import urllib.parse

# ── Configuration ──────────────────────────────────────────────────────────────
LATITUDE  = "52.3"
LONGITUDE = "4.98"
INTERVAL  = 30 * 60   # seconds between refreshes

# ── WMO weather code tables ────────────────────────────────────────────────────
_ICONS = {
    0:'☀️',  1:'🌤️', 2:'⛅',  3:'☁️',
    45:'🌫️', 48:'🌫️',
    51:'🌦️', 53:'🌦️', 55:'🌧️',
    61:'🌧️', 63:'🌧️', 65:'🌧️',
    71:'🌨️', 73:'🌨️', 75:'❄️',
    80:'🌦️', 81:'🌧️', 82:'⛈️',
    95:'⛈️', 96:'⛈️', 99:'⛈️',
}
_ICONS_NIGHT = {0:'🌕', 1:'🌔', 2:'🌑', 3:'☁️'}
_DESC = {
    0:'Clear',        1:'Mainly clear',   2:'Partly cloudy',  3:'Overcast',
    45:'Fog',         48:'Icy fog',
    51:'Light drizzle', 53:'Drizzle',     55:'Heavy drizzle',
    61:'Light rain',  63:'Rain',          65:'Heavy rain',
    71:'Light snow',  73:'Snow',          75:'Heavy snow',
    80:'Showers',     81:'Heavy showers', 82:'Violent showers',
    95:'Thunderstorm', 96:'Thunderstorm+hail', 99:'Thunderstorm+hail',
}

# ── Module state ───────────────────────────────────────────────────────────────
cache = None   # dict {icon, desc, fetched_at} or None

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch(on_update=None) -> None:
    """
    Fetch current weather from Open-Meteo and update cache.

    Calls on_update(icon, desc) if the fetch succeeds — use this to push
    the result to SSE clients or any other consumer. Safe to call on_update=None.
    """
    global cache
    params = urllib.parse.urlencode({
        'latitude':  LATITUDE,
        'longitude': LONGITUDE,
        'current':   'weather_code,temperature_2m,is_day',
        'daily':     'temperature_2m_min,temperature_2m_max',
        'timezone':  'Europe/Amsterdam',
    })
    d = _fetch_url(f'https://api.open-meteo.com/v1/forecast?{params}')
    if not d:
        return
    cur    = d.get('current', {})
    code   = cur.get('weather_code', 0)
    is_day = cur.get('is_day', 1) == 1
    icon   = (_ICONS_NIGHT if not is_day and code <= 3 else _ICONS).get(code, '🌡️')
    desc   = _DESC.get(code, 'Unknown')
    temp   = cur.get('temperature_2m')
    desc_str = f"{temp:.1f}°" if temp is not None else desc
    daily = d.get('daily', {})
    mins  = daily.get('temperature_2m_min') or []
    maxs  = daily.get('temperature_2m_max') or []
    cache = {
        'icon': icon, 'desc': desc_str, 'fetched_at': time.time(),
        'forecast_low_tomorrow':  mins[1] if len(mins) > 1 else None,
        'forecast_high_tomorrow': maxs[1] if len(maxs) > 1 else None,
    }
    low  = cache['forecast_low_tomorrow']
    high = cache['forecast_high_tomorrow']
    print(f'[weather] {icon} {desc_str}  low_tomorrow={low}° high_tomorrow={high}°')
    if on_update:
        on_update(icon, desc_str)


# ── Internals ──────────────────────────────────────────────────────────────────

def _fetch_url(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            raw = resp.read()
        if raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
        return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        print(f'[weather] HTTP {e.code} from {url}: {e.read().decode(errors="replace")[:500]}')
        return None
    except Exception as e:
        print(f'[weather] fetch error: {e}')
        return None
