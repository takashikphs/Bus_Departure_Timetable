import os
import threading
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from flask import Flask, jsonify, send_from_directory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")

OVAPI_BASE = "http://v0.ovapi.nl/tpc"

# Stop configuration — set these in your .env / docker-compose environment.
# HOME_STOP: your local stop name (display only)
# CITY_STOP: the city terminal name (display only)
# TPC_HOME_OUTBOUND: the nearest OVAPI timing-point code on the outbound route
#                    (may be a proxy stop if your stop isn't a timing point)
# TPC_CITY_INBOUND:  the city terminal timing-point code for inbound buses
HOME_STOP_NAME    = os.getenv("HOME_STOP_NAME",    "My Stop")
CITY_STOP_NAME    = os.getenv("CITY_STOP_NAME",    "City Terminal")
TPC_HOME_OUTBOUND = os.getenv("TPC_HOME_OUTBOUND", "")
TPC_CITY_INBOUND  = os.getenv("TPC_CITY_INBOUND",  "")

# Comma-separated list of line numbers to track, e.g. "4,54"
ROUTES = set(os.getenv("ROUTES", "4,54").split(","))

CACHE = {}
CACHE_LOCK = threading.Lock()

# ntfy.sh push notifications — set NTFY_TOPIC in docker-compose.yml env to enable.
# Install the ntfy app, subscribe to your topic, and get notified when a bus is close.
NTFY_TOPIC    = os.getenv("NTFY_TOPIC", "")
NOTIFY_AT_MIN = int(os.getenv("NOTIFY_AT_MIN", 5))   # notify when bus is this many min away
# Cooldown: don't re-notify the same bus for this many minutes
NOTIFY_COOLDOWN_MIN = int(os.getenv("NOTIFY_COOLDOWN_MIN", 20))

_last_notified: dict[str, datetime] = {}   # key: "direction:route"


def send_notification(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "bus",
            },
            timeout=5,
        )
        log.info("Notification sent: %s – %s", title, message)
    except Exception as e:
        log.warning("Notification failed: %s", e)


def maybe_notify(direction: str, route: str, minutes: int, destination: str) -> None:
    if minutes > NOTIFY_AT_MIN:
        return
    key = f"{direction}:{route}"
    now = datetime.now(timezone.utc)
    last = _last_notified.get(key)
    if last and (now - last).total_seconds() < NOTIFY_COOLDOWN_MIN * 60:
        return
    _last_notified[key] = now
    if direction == "to_home":
        title = f"Bus {route} in {minutes} min - leaving {CITY_STOP_NAME}"
        body  = f"Departs {CITY_STOP_NAME} toward {destination} - get on for {HOME_STOP_NAME}"
    else:
        title = f"Bus {route} in {minutes} min - leaving {HOME_STOP_NAME}"
        body  = f"Departs {HOME_STOP_NAME} toward {destination} (to {CITY_STOP_NAME})"
    send_notification(title, body)


_AMS = ZoneInfo("Europe/Amsterdam")

def parse_dt(s: str):
    """Parse ISO datetime string to aware datetime.
    OVAPI returns naive local Dutch time (no timezone suffix), so treat as Amsterdam."""
    if not s:
        return None
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_AMS)
                return dt
            except ValueError:
                continue
    except Exception:
        pass
    return None


def minutes_until(dt) -> int | None:
    if dt is None:
        return None
    now = datetime.now(timezone.utc).astimezone()
    diff = (dt - now).total_seconds()
    if diff < -60:
        return None
    return max(0, int(diff / 60))


def fetch_departures_from_tpc(tpc: str) -> list[dict]:
    """Fetch all departures from a timing point code, return list of passes."""
    url = f"{OVAPI_BASE}/{tpc}/departures"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return []

    passes = []
    stop_data = data.get(tpc, {})
    for pid, p in stop_data.get("Passes", {}).items():
        passes.append(p)
    return passes


def next_departure(passes: list[dict], route: str, offset_min: int = 0) -> dict | None:
    """Find the next departure for a given route, adjusting by offset_min.

    offset_min compensates for proxy stops:
    - positive: proxy is before Rosenburch (bus still needs N min to arrive)
    - negative: proxy is after Rosenburch (bus passed N min ago; skip if already gone)
    """
    candidates = []
    for p in passes:
        if str(p.get("LinePublicNumber", "")) != route:
            continue
        t_str = p.get("ExpectedDepartureTime") or p.get("TargetDepartureTime") or p.get("ExpectedArrivalTime")
        dt = parse_dt(t_str)
        mins = minutes_until(dt)
        if mins is None:
            continue
        adjusted = mins + offset_min
        if adjusted < 0:
            continue  # bus already passed Rosenburch
        candidates.append({
            "minutes": adjusted,
            "destination": p.get("DestinationName50", ""),
            "route": route,
        })
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["minutes"])


# Travel time offsets (minutes) between proxy TPC stop and your home stop.
# CITY_TO_HOME_MIN: minutes from city terminal timing point to your home stop (positive = not yet arrived)
# PROXY_OFFSET_MIN: minutes from your home stop to the outbound proxy stop (subtract to get home-stop time)
CITY_TO_HOME_MIN  = int(os.getenv("CITY_TO_HOME_MIN",  7))
PROXY_OFFSET_MIN  = int(os.getenv("PROXY_OFFSET_MIN",  6))

# Weather — coordinates for your location, set via env vars
MAP_LAT = float(os.getenv("MAP_LAT", "52.0"))
MAP_LON = float(os.getenv("MAP_LON", "4.5"))
WEATHER_URL = (
    f"https://api.open-meteo.com/v1/forecast"
    f"?latitude={MAP_LAT}&longitude={MAP_LON}"
    "&current=temperature_2m,weather_code,precipitation"
    "&timezone=Europe%2FAmsterdam"
)

WMO_LABEL = {
    0: ("Sunny", "sunny"),
    1: ("Mostly clear", "sunny"),
    2: ("Partly cloudy", "cloudy"),
    3: ("Cloudy", "cloudy"),
    45: ("Foggy", "fog"),  48: ("Foggy", "fog"),
    51: ("Drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Drizzle", "rain"),
    61: ("Rain", "rain"),    63: ("Rain", "rain"),    65: ("Heavy rain", "rain"),
    71: ("Snow", "snow"),    73: ("Snow", "snow"),    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm", "storm"), 99: ("Thunderstorm", "storm"),
}

def fetch_weather() -> dict | None:
    try:
        resp = requests.get(WEATHER_URL, timeout=10)
        resp.raise_for_status()
        c = resp.json().get("current", {})
        code = c.get("weather_code", 0)
        label, kind = WMO_LABEL.get(code, ("Unknown", "cloudy"))
        return {
            "temp": round(c.get("temperature_2m", 0)),
            "label": label,
            "kind": kind,          # sunny | cloudy | fog | rain | snow | storm
            "precip": c.get("precipitation", 0),
        }
    except Exception as e:
        log.warning("Weather fetch failed: %s", e)
        return None


def refresh_cache():
    # --- To home (from city terminal toward home stop) ---
    city_passes = fetch_departures_from_tpc(TPC_CITY_INBOUND)
    to_home = {r: next_departure(city_passes, r, offset_min=0) for r in ROUTES}

    # --- To city (via outbound proxy stop) ---
    # Proxy stop is PROXY_OFFSET_MIN after the home stop on the outbound route.
    # Subtract that offset so we show estimated home-stop departure time.
    outbound_passes = fetch_departures_from_tpc(TPC_HOME_OUTBOUND)
    to_city = {r: next_departure(outbound_passes, r, offset_min=-PROXY_OFFSET_MIN) for r in ROUTES}

    weather = fetch_weather()

    # Push notifications
    for direction, deps in [("to_home", to_home), ("to_city", to_city)]:
        for route, dep in deps.items():
            if dep:
                maybe_notify(direction, route, dep["minutes"], dep["destination"])

    log.info("to_home=%s | to_city=%s | weather=%s", to_home, to_city, weather)

    with CACHE_LOCK:
        CACHE.update({
            "to_home":      to_home,
            "to_city":      to_city,
            "weather":      weather,
            "last_updated": datetime.now().isoformat(),
        })


def background_poller():
    while True:
        try:
            refresh_cache()
        except Exception as e:
            log.error("Poller error: %s", e)
        time.sleep(int(os.getenv("REFRESH_INTERVAL", 30)))


@app.route("/api/config")
def config():
    return jsonify({
        "home_stop": HOME_STOP_NAME,
        "city_stop": CITY_STOP_NAME,
        "map_lat":   MAP_LAT,
        "map_lon":   MAP_LON,
    })


@app.route("/api/departures")
def departures():
    with CACHE_LOCK:
        if not CACHE:
            return jsonify({"error": "Data not yet available, please retry"}), 503
        return jsonify(CACHE)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_static(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    log.info("Fetching initial data...")
    refresh_cache()
    t = threading.Thread(target=background_poller, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 8888))
    app.run(host="0.0.0.0", port=port)
