import requests
from flask import Flask, jsonify, render_template
from flask_cors import CORS
import random

app = Flask(__name__)
CORS(app)

WAQI_TOKEN = "7e523e79db14272c189daac209fd4cca28b7975f"

LAT_MIN = 24
LAT_MAX = 46
LON_MIN = 123
LON_MAX = 146

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/air")
def air_api():
    url = (
        f"https://api.waqi.info/map/bounds/"
        f"?latlng={LAT_MIN},{LON_MIN},{LAT_MAX},{LON_MAX}"
        f"&token={WAQI_TOKEN}"
    )

    try:
        r = requests.get(url, timeout=6).json()
        raw = r.get("data", [])
        cleaned = []

        japan_keywords = [
            "japan","tokyo","osaka","kyoto","nagoya","sapporo","fukuoka",
            "yokohama","nara","kobe","hiroshima","sendai","okinawa"
        ]

        for p in raw:
            aqi_raw = p.get("aqi")
            try:
                aqi = int(aqi_raw)
            except:
                continue

            lat = p.get("lat")
            lon = p.get("lon")

            name = p.get("station", {}).get("name", "").lower()

            if not any(kw in name for kw in japan_keywords):
                continue

            cleaned.append({
                "lat": lat,
                "lon": lon,
                "aqi": aqi,
                "name": name.title()
            })

        print("[WAQI] Japan-only points:", len(cleaned))

        if cleaned:
            return jsonify(cleaned)
        else:
            return jsonify(generate_fallback())

    except Exception as e:
        print("[WAQI] ERROR:", e)
        return jsonify(generate_fallback())


def generate_fallback():
    points = []
    for _ in range(250):
        lat = random.uniform(LAT_MIN, LAT_MAX)
        lon = random.uniform(LON_MIN, LON_MAX)
        points.append({
            "lat": lat,
            "lon": lon,
            "aqi": random.randint(20, 160),
            "name": "Synthetic"
        })
    return points


if __name__ == "__main__":
    app.run(debug=True, port=5000)
