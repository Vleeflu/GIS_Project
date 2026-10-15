import hashlib
import json
import os
import tempfile
import threading
import time
from bisect import bisect_right
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

WAQI_TOKEN = "7e523e79db14272c189daac209fd4cca28b7975f"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Bounding box covering the 11 ASEAN member states (Myanmar to Papua).
LAT_MIN = -11.5
LAT_MAX = 28.6
LON_MIN = 92
LON_MAX = 141.5

# Open-Meteo Air Quality (CAMS global model, CC-BY 4.0) is sampled at the city
# list in data/asean_cities.json (GeoNames, CC-BY 4.0). Indonesia is fetched
# first so it gets the free quota (10,000 location-calls/day) before the other
# ASEAN countries; any country whose Open-Meteo fetch fails (e.g. quota
# exhausted, HTTP 429) falls back to WAQI stations.
OM_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OM_CHUNK = 400            # locations per request, keeps the URL under ~8 KB
OM_HOURS = 24             # hourly series per fetch; served hour by hour
OM_TTL = 12 * 3600        # refetch twice a day: ~800 location-calls/day
OM_RETRY = 15 * 60        # back-off after a failed fetch
OM_CACHE_FILE = os.path.join(tempfile.gettempdir(), "gis_open_meteo_asean.json")

# Coastal WAQI stations can sit just outside the 1:50m Natural Earth coastline.
COAST_TOLERANCE_DEG = 0.1


def _load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


CITIES = _load_json("asean_cities.json")
OM_GROUPS = [
    ("ID", [c for c in CITIES if c["country"] == "ID"]),
    ("ASEAN", [c for c in CITIES if c["country"] != "ID"]),
]

COUNTRY_NAMES = {}
COUNTRY_POLYGONS = []  # (iso2, (lon0, lat0, lon1, lat1), [outer ring, *holes])
for _feature in _load_json("asean_countries.geojson")["features"]:
    _iso2 = _feature["properties"]["iso2"]
    COUNTRY_NAMES[_iso2] = _feature["properties"]["name"]
    _geom = _feature["geometry"]
    _parts = _geom["coordinates"] if _geom["type"] == "MultiPolygon" else [_geom["coordinates"]]
    for _rings in _parts:
        _lons = [p[0] for p in _rings[0]]
        _lats = [p[1] for p in _rings[0]]
        COUNTRY_POLYGONS.append(
            (_iso2, (min(_lons), min(_lats), max(_lons), max(_lats)), _rings)
        )

_om_cache = {}
_om_failed_at = {}
_om_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/air")
def air_api():
    now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    points = []
    waqi_countries = set()

    for group, cities in OM_GROUPS:
        entry = _om_series(group, cities, now_hour)
        if entry is None:
            waqi_countries.update(c["country"] for c in cities)
        else:
            points.extend(_om_points(entry, cities, now_hour))

    if waqi_countries:
        try:
            points.extend(_waqi_points(waqi_countries))
        except Exception as e:
            print("[WAQI] ERROR:", e)

    print(
        "[air] open-meteo:", sum(p["source"] == "open-meteo" for p in points),
        "waqi:", sum(p["source"] == "waqi" for p in points),
        "waqi countries:", sorted(waqi_countries),
    )
    return jsonify(points)


# ---------------------------------------------------------------- Open-Meteo

def _cities_key(cities):
    raw = json.dumps([[c["name"], c["lat"], c["lon"]] for c in cities])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _om_covers(entry, cities, now_hour):
    return (
        entry is not None
        and entry.get("key") == _cities_key(cities)
        and entry["times"][0] <= now_hour <= entry["times"][-1]
    )


def _om_fresh(entry, cities, now_hour):
    return _om_covers(entry, cities, now_hour) and time.time() - entry["fetched_at"] < OM_TTL


def _read_om_cache_file():
    try:
        with open(OM_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_om_cache_file(cache):
    tmp = OM_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, OM_CACHE_FILE)


def _fetch_open_meteo(cities):
    times = None
    series = []
    for start in range(0, len(cities), OM_CHUNK):
        chunk = cities[start:start + OM_CHUNK]
        r = requests.get(OM_URL, params={
            "latitude": ",".join(str(c["lat"]) for c in chunk),
            "longitude": ",".join(str(c["lon"]) for c in chunk),
            "hourly": "us_aqi",
            "past_hours": 1,
            "forecast_hours": OM_HOURS,
            "timezone": "GMT",
        }, timeout=30)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        if isinstance(body, dict):  # a single location is not wrapped in a list
            body = [body]
        for loc in body:
            times = loc["hourly"]["time"]
            series.append(loc["hourly"]["us_aqi"])

    if len(series) != len(cities):
        raise ValueError(f"expected {len(cities)} locations, got {len(series)}")
    return {
        "key": _cities_key(cities),
        "fetched_at": time.time(),
        "times": times,
        "series": series,
    }


def _om_series(group, cities, now_hour):
    """Hourly US AQI series for `cities`, or None if Open-Meteo has nothing usable."""
    with _om_lock:
        entry = _om_cache.get(group)
        if not _om_fresh(entry, cities, now_hour):
            entry = _read_om_cache_file().get(group) or entry
        if _om_fresh(entry, cities, now_hour):
            _om_cache[group] = entry
            return entry

        if time.time() - _om_failed_at.get(group, 0) >= OM_RETRY:
            try:
                fetched = _fetch_open_meteo(cities)
            except Exception as e:
                print(f"[Open-Meteo] {group} fetch failed: {e}")
                _om_failed_at[group] = time.time()
            else:
                print(f"[Open-Meteo] {group}: {len(cities)} cities, "
                      f"{fetched['times'][0]}..{fetched['times'][-1]} UTC")
                _om_cache[group] = fetched
                _write_om_cache_file(_om_cache)
                return fetched

        # Fetch failed or backing off: an older series that still covers this
        # hour beats dropping to WAQI.
        return entry if _om_covers(entry, cities, now_hour) else None


def _om_points(entry, cities, now_hour):
    t = bisect_right(entry["times"], now_hour) - 1
    points = []
    for city, values in zip(cities, entry["series"]):
        aqi = values[t]
        if aqi is None:
            continue
        points.append({
            "lat": city["lat"],
            "lon": city["lon"],
            "aqi": int(round(aqi)),
            "name": f"{city['name']}, {COUNTRY_NAMES[city['country']]}",
            "country": city["country"],
            "source": "open-meteo",
        })
    return points


# ---------------------------------------------------------------------- WAQI

def _waqi_points(countries):
    url = (
        f"https://api.waqi.info/map/bounds/"
        f"?latlng={LAT_MIN},{LON_MIN},{LAT_MAX},{LON_MAX}"
        f"&token={WAQI_TOKEN}"
    )
    raw = requests.get(url, timeout=10).json().get("data", [])
    points = []
    for p in raw:
        try:
            aqi = int(p.get("aqi"))
        except (TypeError, ValueError):
            continue
        lat = p.get("lat")
        lon = p.get("lon")
        country = country_at(lat, lon)
        if country not in countries:
            continue
        points.append({
            "lat": lat,
            "lon": lon,
            "aqi": aqi,
            "name": p.get("station", {}).get("name", ""),
            "country": country,
            "source": "waqi",
        })
    return points


# ----------------------------------------------------------------- Geometry

def _in_ring(lon, lat, ring):
    inside = False
    x0, y0 = ring[-1]
    for x1, y1 in ring:
        if (y1 > lat) != (y0 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
        x0, y0 = x1, y1
    return inside


def _seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = 0.0
    if dx or dy:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    ex, ey = ax + t * dx - px, ay + t * dy - py
    return ex * ex + ey * ey


def country_at(lat, lon):
    """ISO2 of the ASEAN country containing (lat, lon), or None."""
    for iso2, (x0, y0, x1, y1), rings in COUNTRY_POLYGONS:
        if (x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, rings[0])
                and not any(_in_ring(lon, lat, hole) for hole in rings[1:])):
            return iso2

    tol = COAST_TOLERANCE_DEG
    best, best_d2 = None, tol * tol
    for iso2, (x0, y0, x1, y1), rings in COUNTRY_POLYGONS:
        if not (x0 - tol <= lon <= x1 + tol and y0 - tol <= lat <= y1 + tol):
            continue
        ring = rings[0]
        for (ax, ay), (bx, by) in zip(ring, ring[1:]):
            d2 = _seg_dist2(lon, lat, ax, ay, bx, by)
            if d2 < best_d2:
                best, best_d2 = iso2, d2
    return best


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
