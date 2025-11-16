# righttrack_server.py
import os
import re
import time
import json
from typing import Dict, Any, List, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

# Optional Gemini (Google) client
try:
    import google.generativeai as genai
    HAS_GENAI = True
except Exception:
    HAS_GENAI = False

app = Flask(__name__)
CORS(app)

# ---------- Configuration ----------
BASE_URL = "https://indiarailinfo.com"
HEADERS = {
    "User-Agent": "RightTrack/1.0 (contact: ajashiatechnologies@gmail.com)",
    "Accept-Language": "en-US,en;q=0.9",
}

# Overpass / Nominatim config
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE: Dict[str, Any] = {}
CACHE_TTL_SECONDS = 60 * 60 * 6  # 6 hours

# Gemini config
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GENAI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "models/gemini-2.5-flash")  # override if needed
if GEMINI_API_KEY and HAS_GENAI:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
else:
    model = None

# ---------- Utilities ----------
def cache_get(key: str) -> Optional[Dict]:
    hit = CACHE.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > CACHE_TTL_SECONDS:
        del CACHE[key]
        return None
    return value

def cache_set(key: str, value: Dict) -> None:
    CACHE[key] = (time.time(), value)

def slugify(text: str) -> str:
    s = text.lower()
    s = s.replace("(", "").replace(")", "")
    s = s.replace("/", "-").replace(" ", "-").replace("&", "and")
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s

# ---------- indiarailinfo endpoints ----------
@app.route("/station_search", methods=["POST"])
def station_search():
    data = request.get_json(silent=True)
    if not data or "q" not in data:
        return jsonify({"error": "JSON body must contain 'q'"}), 400
    query = str(data["q"]).strip()
    if not query:
        return jsonify({"error": "q cannot be empty"}), 400

    url = f"{BASE_URL}/shtml/list.shtml?LappGetStationList/{query}/0/1/0?"
    try:
        r = requests.get(url, timeout=12, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("table.dropdowntable tr")
        results = []
        for i in range(0, max(0, len(rows)-2), 2):
            main = rows[i].find_all("td")
            sub = rows[i + 1].find_all("td")
            if len(main) < 5 or len(sub) < 3:
                continue
            station_id = main[0].text.strip()
            code = main[1].text.strip()
            name = main[2].text.strip()
            division = main[3].text.strip()
            full = main[4].text.strip()
            extra = sub[2].text.strip()
            results.append({
                "id": station_id,
                "code": code,
                "name": name,
                "division": division,
                "full": full,
                "location": extra
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/departures", methods=["POST"])
def departures():
    data = request.get_json(silent=True)
    if not data or "id" not in data:
        return jsonify({"error": "JSON body must contain 'id'"}), 400
    station_id = str(data["id"]).strip()
    if not station_id:
        return jsonify({"error": "id cannot be empty"}), 400
    dest = str(data.get("dest", "")).strip() or "0"

    url = f"{BASE_URL}/search/{station_id}/0/{dest}?src=&dest=&locoClass=undefined&bedroll=undefined&"
    try:
        r = requests.get(url, timeout=12, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        trains = []
        blocks = soup.select('div[style*="line-height:20px;"]')
        for block in blocks:
            timetable_href = None
            timetable_slug = None
            internal_id = None
            name_anchor = None

            # Find anchor containing "/train/timetable/"
            for a in block.find_all("a", href=True):
                if "/train/timetable/" in a["href"]:
                    name_anchor = a
                    break
            if not name_anchor:
                a = block.select_one("a[href*='/train/timetable/']")
                if a:
                    name_anchor = a

            if name_anchor:
                href = name_anchor.get("href", "")
                m = re.search(r"/train/timetable/([^/]+)/(\d+)", href)
                if m:
                    timetable_href = href
                    timetable_slug = m.group(1)
                    internal_id = m.group(2)

            if not internal_id:
                trnsumm = block.find_next_sibling("div", class_="reg trnsumm")
                if trnsumm and trnsumm.has_attr("t"):
                    internal_id = trnsumm["t"]

            train_no = name = ttype = zone = platform = from_stn = dep_time = to_stn = arr_time = None

            if dest != "0":
                cells = block.find_all("div", recursive=False)
                def t(i): return cells[i].get_text(strip=True) if i < len(cells) else None
                train_no = t(0)
                if name_anchor and name_anchor.has_attr("title"):
                    name = name_anchor["title"].split("|")[0].strip()
                else:
                    name = t(1)
                ttype = t(2); zone = t(3); from_stn = t(4); platform = t(5); dep_time = t(6); to_stn = t(7); arr_time = t(9)
            else:
                cells = block.select("div.tdborder, div.tdborderhighlight, div.tdborderlast")
                def t(i): return cells[i].get_text(strip=True) if i < len(cells) else None
                train_no = t(0)
                if cells and len(cells) > 1:
                    name_cell = cells[1]
                    a_in_name = name_cell.find("a", href=True)
                    if a_in_name and a_in_name.has_attr("title"):
                        name = a_in_name["title"].split("|")[0].strip()
                    else:
                        name = t(1)
                else:
                    name = t(1)
                ttype = t(2); zone = t(3); platform = t(4); from_stn = t(6); dep_time = t(7); to_stn = t(8); arr_time = t(9)

            if not train_no or not name:
                continue

            slug = timetable_slug or slugify(f"{name}-{train_no}")
            train_url = None
            if internal_id:
                train_url = f"/train/timetable/{slug}/{internal_id}/{station_id}/{dest}"

            trains.append({
                "train_no": train_no,
                "name": name,
                "type": ttype,
                "zone": zone,
                "platform": platform,
                "from": from_stn,
                "departure_time": arr_time,
                "to": to_stn,
                "arrival_time": dep_time,
                "train_url": train_url
            })
        return jsonify(trains)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/timetable", methods=["POST"])
def timetable():
    data = request.get_json(silent=True)
    if not data or "train_url" not in data:
        return jsonify({"error": "JSON must contain 'train_url'"}), 400
    train_url = str(data["train_url"]).strip()
    if not train_url.startswith("/train/timetable/"):
        return jsonify({"error": "Invalid train_url format"}), 400
    url = BASE_URL + train_url
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select('div[style*="width:35px;"]')
        results = []
        for row in rows:
            parent = row.parent
            cells = parent.find_all("div", recursive=False)
            if len(cells) < 17:
                continue
            code = cells[2].get_text(strip=True)
            name = cells[3].get_text(strip=True)
            arrives = cells[6].get_text(strip=True)
            departs = cells[8].get_text(strip=True)
            platform = cells[11].get_text(strip=True)
            results.append({
                "code": code,
                "station_name": name,
                "arrives": arrives,
                "departs": departs,
                "platform": platform
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- OSM / Overpass endpoints ----------
def geocode_station(station: str) -> Optional[Dict[str, float]]:
    params = {"q": station, "format": "json", "limit": 1, "addressdetails": 0}
    r = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=12)
    if r.status_code != 200:
        return None
    arr = r.json()
    if not arr:
        return None
    return {"lat": float(arr[0]["lat"]), "lon": float(arr[0]["lon"])}

def build_overpass_query(lat: float, lon: float, radius: int) -> str:
    q = f"""
[out:json][timeout:25];
(
  nwr(around:{radius},{lat},{lon})[railway~"station|platform|halt|subway_entrance"];
  nwr(around:{radius},{lat},{lon})[public_transport=platform];
  nwr(around:{radius},{lat},{lon})[amenity~"police|clinic|hospital|toilets|doctors|pharmacy"];
  nwr(around:{radius},{lat},{lon})[office~"station"];
  nwr(around:{radius},{lat},{lon})[building=station];
  nwr(around:{radius},{lat},{lon})[entrance];
  nwr(around:{radius},{lat},{lon})[railway=signal];
  nwr(around:{radius},{lat},{lon})[highway=bus_stop];
);
out center;
"""
    return q

def parse_overpass_result(data: Dict) -> List[Dict]:
    pois = []
    for el in data.get("elements", []):
        if el.get("type") == "node":
            lat = float(el.get("lat", 0))
            lon = float(el.get("lon", 0))
        else:
            center = el.get("center")
            if not center:
                continue
            lat = float(center.get("lat", 0))
            lon = float(center.get("lon", 0))
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("ref") or tags.get("operator") or ""
        type_parts = []
        for k in ("railway", "public_transport", "amenity", "office", "building", "highway", "entrance"):
            if k in tags:
                type_parts.append(f"{k}={tags[k]}")
        type_str = ", ".join(type_parts) if type_parts else "other"
        emergency = False
        if "police" in tags.get("amenity", "") or "hospital" in tags.get("amenity", ""):
            emergency = True
        if tags.get("emergency") or tags.get("emergency_phone") or tags.get("contact:phone"):
            emergency = True
        pois.append({
            "id": el.get("id"),
            "osm_type": el.get("type"),
            "name": name,
            "type": type_str,
            "lat": lat,
            "lon": lon,
            "tags": tags,
            "emergency": emergency,
        })
    return pois

@app.route("/station_map", methods=["GET"])
def station_map():
    station = request.args.get("station", "").strip()
    lat = request.args.get("lat", "").strip()
    lon = request.args.get("lon", "").strip()
    radius = int(request.args.get("radius", "700"))
    if not station and (not lat or not lon):
        return jsonify({"success": False, "error": "Provide station name or lat & lon"}), 400
    cache_key = f"station:{station.lower()}:{radius}" if station else f"coords:{lat}:{lon}:{radius}"
    cached = cache_get(cache_key)
    if cached:
        cached["cached"] = True
        return jsonify(cached)
    if not station:
        try:
            lat_f = float(lat); lon_f = float(lon)
        except ValueError:
            return jsonify({"success": False, "error": "Invalid lat/lon"}), 400
    else:
        geo = geocode_station(station)
        if not geo:
            return jsonify({"success": False, "error": "Geocoding failed"}), 502
        lat_f = geo["lat"]; lon_f = geo["lon"]
    q = build_overpass_query(lat_f, lon_f, radius)
    try:
        r = requests.post(OVERPASS_URL, data=q.encode("utf-8"), headers=HEADERS, timeout=40)
        if r.status_code != 200:
            return jsonify({"success": False, "error": f"Overpass returned {r.status_code}"}), 502
        data = r.json()
        pois = parse_overpass_result(data)
        result = {
            "success": True,
            "center": {"lat": lat_f, "lon": lon_f},
            "radius": radius,
            "source": "openstreetmap/overpass",
            "pois_count": len(pois),
            "pois": pois,
            "queried_at": int(time.time()),
        }
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ---------- Gemini / AI endpoint ----------
@app.route("/ask_ai", methods=["POST"])
def ask_ai():
    data = request.get_json(silent=True)
    if not data or "query" not in data:
        return jsonify({"ok": False, "error": "JSON must contain 'query'"}), 400
    query = str(data["query"]).strip()
    if not query:
        return jsonify({"ok": False, "error": "query cannot be empty"}), 400

    if not GEMINI_API_KEY or not HAS_GENAI or model is None:
        return jsonify({
            "ok": False,
            "error": "Gemini client not configured on server. Set GEMINI_API_KEY and install google-generativeai package."
        }), 502

    try:
        # simple generation call - you can customize prompts / system messages here
        resp = model.generate_content(query)
        text = ""
        # `resp` may contain multiple fields; we grab text if available
        if hasattr(resp, "text") and resp.text:
            text = resp.text
        elif isinstance(resp, dict) and "candidates" in resp:
            # older shaped response
            candidates = resp.get("candidates", [])
            if candidates:
                text = candidates[0].get("content", "")
        else:
            # fallback: try to str() the response
            text = str(resp)
        return jsonify({"ok": True, "response": text})
    except Exception as e:
        return jsonify({"ok": False, "error": f"OpenAI/Gemini call failed: {str(e)}"}), 500

# ---------- health ----------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": int(time.time())})

# ---------- run ----------
if __name__ == "__main__":
    # single port (5000) for everything
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)