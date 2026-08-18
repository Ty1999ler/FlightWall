"""FlightWall — a local display of aircraft overhead right now.

Data sources (all free, no API keys):
  - api.adsb.lol      : live ADS-B positions within a radius of a point
  - api.adsbdb.com    : callsign -> airline + route, hex -> aircraft type/photo
  - ip-api.com        : one-time IP geolocation to auto-fill your location

Run:  py server.py   then open http://127.0.0.1:8484
"""

import asyncio
import json
import math
import os
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
# In the container this points at the bind-mounted /data volume so the saved
# location survives image updates; locally it sits next to server.py.
CONFIG_PATH = Path(os.environ.get("FLIGHTWALL_CONFIG", BASE_DIR / "config.json"))

DEFAULT_CONFIG = {
    "lat": None,
    "lon": None,
    "location_name": "",
    "radius_nm": 30,
    "refresh_seconds": 8,
    "units": "imperial",  # imperial | metric
    "display_mode": "closest",  # closest | all
}

ADSB_POINT_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{hex}"
IP_GEO_URL = "http://ip-api.com/json/"

POINT_CACHE_SECONDS = 4       # dedupe rapid refreshes against adsb.lol
ROUTE_TTL_HIT = 6 * 3600      # callsign routes rarely change within a day
ROUTE_TTL_MISS = 15 * 60      # retry unknown callsigns occasionally
LOOKUP_CONCURRENCY = 6

# Fallback airline names when adsbdb has no route for a callsign.
AIRLINE_PREFIXES = {
    "AAL": "American Airlines", "DAL": "Delta Air Lines", "UAL": "United Airlines",
    "SWA": "Southwest Airlines", "JBU": "JetBlue", "ASA": "Alaska Airlines",
    "NKS": "Spirit Airlines", "FFT": "Frontier Airlines", "AAY": "Allegiant Air",
    "SKW": "SkyWest", "RPA": "Republic Airways", "EDV": "Endeavor Air",
    "ENY": "Envoy Air", "JIA": "PSA Airlines", "ACA": "Air Canada",
    "JZA": "Air Canada Jazz", "ROU": "Air Canada Rouge", "WJA": "WestJet",
    "WEN": "WestJet Encore", "TSC": "Air Transat", "POE": "Porter Airlines",
    "FLE": "Flair Airlines", "CJT": "Cargojet", "FDX": "FedEx Express",
    "UPS": "UPS Airlines", "GTI": "Atlas Air", "BAW": "British Airways",
    "AFR": "Air France", "DLH": "Lufthansa", "KLM": "KLM", "UAE": "Emirates",
    "QTR": "Qatar Airways", "EIN": "Aer Lingus", "VIR": "Virgin Atlantic",
    "SWG": "Sunwing Airlines", "NRL": "Nolinor Aviation", "PSC": "Pascan Aviation",
    "PVL": "PAL Airlines", "EJA": "NetJets", "LXJ": "Flexjet",
}


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    # hand-edited files may hold wrong types; coerce or fall back to defaults
    try:
        lat, lon = float(cfg["lat"]), float(cfg["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
        cfg["lat"], cfg["lon"] = lat, lon
    except (TypeError, ValueError):
        cfg["lat"] = cfg["lon"] = None
    try:
        cfg["radius_nm"] = max(1, min(250, float(cfg["radius_nm"])))
    except (TypeError, ValueError):
        cfg["radius_nm"] = DEFAULT_CONFIG["radius_nm"]
    try:
        cfg["refresh_seconds"] = max(3, min(120, int(cfg["refresh_seconds"])))
    except (TypeError, ValueError):
        cfg["refresh_seconds"] = DEFAULT_CONFIG["refresh_seconds"]
    if cfg.get("units") not in ("imperial", "metric"):
        cfg["units"] = DEFAULT_CONFIG["units"]
    if cfg.get("display_mode") not in ("closest", "all"):
        cfg["display_mode"] = DEFAULT_CONFIG["display_mode"]
    cfg["location_name"] = str(cfg.get("location_name") or "")
    return cfg


def save_config(cfg: dict) -> bool:
    # atomic write; a locked/read-only file must not kill startup or a request
    try:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        return True
    except OSError as exc:
        print(f"Warning: could not write {CONFIG_PATH} ({exc}); "
              f"settings stay in effect until the server stops.")
        return False


state: dict = {
    "config": load_config(),
    "client": None,
    "point_cache": {},  # (lat4, lon4, radius) -> (fetched_monotonic, data)
    "route_cache": {},      # callsign -> (expires_monotonic, route|None)
    "aircraft_cache": {},   # hex -> info|None
    "lookup_sem": asyncio.Semaphore(LOOKUP_CONCURRENCY),
    "adsbdb_down_until": 0.0,  # backoff when adsbdb times out, so polls stay fast
}


async def geolocate(client: httpx.AsyncClient) -> dict | None:
    try:
        r = await client.get(IP_GEO_URL, timeout=10)
        d = r.json()
        if d.get("status") == "success":
            name = ", ".join(x for x in (d.get("city"), d.get("region")) if x)
            return {"lat": d["lat"], "lon": d["lon"], "location_name": name}
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


# ---------------------------------------------------------------- geo math

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def route_is_plausible(route: dict, ac_lat: float, ac_lon: float) -> bool:
    """A stale adsbdb route puts the plane nowhere near the O->D great circle.
    Accept the route only if the detour through the aircraft is small."""
    o, d = route.get("origin") or {}, route.get("destination") or {}
    try:
        o_lat, o_lon = float(o["latitude"]), float(o["longitude"])
        d_lat, d_lon = float(d["latitude"]), float(d["longitude"])
    except (KeyError, TypeError, ValueError):
        return True  # can't judge; give benefit of the doubt
    if o.get("iata_code") and o.get("iata_code") == d.get("iata_code"):
        return False
    od = haversine_nm(o_lat, o_lon, d_lat, d_lon)
    detour = haversine_nm(o_lat, o_lon, ac_lat, ac_lon) + haversine_nm(ac_lat, ac_lon, d_lat, d_lon)
    return detour <= od * 1.25 + 250


# ---------------------------------------------------------------- lookups

def _airport(a: dict | None) -> dict | None:
    if not a:
        return None
    return {
        "iata": a.get("iata_code"),
        "icao": a.get("icao_code"),
        "name": a.get("name"),
        "city": a.get("municipality"),
        "country": a.get("country_iso_name"),
        "lat": a.get("latitude"),
        "lon": a.get("longitude"),
    }


async def lookup_route(callsign: str) -> dict | None:
    loop = asyncio.get_running_loop()
    cached = state["route_cache"].get(callsign)
    if cached and cached[0] > loop.time():
        return cached[1]
    route = None
    async with state["lookup_sem"]:
        if loop.time() < state["adsbdb_down_until"]:
            return cached[1] if cached else None
        try:
            r = await state["client"].get(
                ADSBDB_CALLSIGN_URL.format(callsign=callsign), timeout=5)
            if r.status_code == 200:
                fr = r.json().get("response", {}).get("flightroute")
                if fr:
                    airline = fr.get("airline") or {}
                    flight_iata = fr.get("callsign_iata")
                    if flight_iata and (flight_iata.isdigit() or flight_iata == callsign):
                        flight_iata = None  # adsbdb sometimes returns junk here
                    route = {
                        "flight_iata": flight_iata,
                        "airline": airline.get("name"),
                        "airline_iata": airline.get("iata"),
                        "origin": fr.get("origin"),
                        "destination": fr.get("destination"),
                    }
        except (httpx.HTTPError, ValueError):
            # adsbdb down/slow: back off globally and re-arm this entry briefly
            # so the next polls don't re-attempt every callsign
            state["adsbdb_down_until"] = loop.time() + 60
            state["route_cache"][callsign] = (loop.time() + 60, cached[1] if cached else None)
            return cached[1] if cached else None
    ttl = ROUTE_TTL_HIT if route else ROUTE_TTL_MISS
    state["route_cache"][callsign] = (loop.time() + ttl, route)
    return route


async def lookup_aircraft(hex_code: str) -> dict | None:
    if hex_code in state["aircraft_cache"]:
        return state["aircraft_cache"][hex_code]
    loop = asyncio.get_running_loop()
    info = None
    async with state["lookup_sem"]:
        if loop.time() < state["adsbdb_down_until"]:
            return None
        try:
            r = await state["client"].get(
                ADSBDB_AIRCRAFT_URL.format(hex=hex_code), timeout=5)
            if r.status_code == 200:
                a = r.json().get("response", {}).get("aircraft")
                if a:
                    info = {
                        "type_name": a.get("type"),
                        "manufacturer": a.get("manufacturer"),
                        "registration": a.get("registration"),
                        "owner": a.get("registered_owner"),
                        "photo_thumb": a.get("url_photo_thumbnail"),
                        "photo": a.get("url_photo"),
                    }
        except (httpx.HTTPError, ValueError):
            state["adsbdb_down_until"] = loop.time() + 60
            return None  # not cached; the backoff prevents hammering
    state["aircraft_cache"][hex_code] = info
    return info


async def fetch_point(lat: float, lon: float, radius_nm: float) -> list[dict]:
    loop = asyncio.get_running_loop()
    key = (round(lat, 4), round(lon, 4), radius_nm)
    pc = state["point_cache"]
    hit = pc.get(key)
    if hit and loop.time() - hit[0] < POINT_CACHE_SECONDS:
        return hit[1]
    url = ADSB_POINT_URL.format(lat=lat, lon=lon, radius=int(radius_nm))
    r = await state["client"].get(url, timeout=15)
    r.raise_for_status()
    data = r.json().get("ac") or []
    if len(pc) > 32:  # several viewers at different spots, not a real cache
        pc.clear()
    pc[key] = (loop.time(), data)
    return data


def prune_caches() -> None:
    """Keep memory flat across weeks of 24/7 running."""
    now = asyncio.get_running_loop().time()
    rc = state["route_cache"]
    if len(rc) > 512:
        state["route_cache"] = {k: v for k, v in rc.items() if v[0] > now - 3600}
    ac = state["aircraft_cache"]
    while len(ac) > 20000:
        ac.pop(next(iter(ac)))  # dicts preserve insertion order: FIFO


def fallback_airline(callsign: str) -> str | None:
    prefix = callsign[:3]
    if prefix.isalpha() and callsign[3:4].isdigit():
        return AIRLINE_PREFIXES.get(prefix)
    return None


async def enrich(ac: dict, home_lat: float, home_lon: float) -> dict:
    callsign = (ac.get("flight") or "").strip().upper()
    hex_code = (ac.get("hex") or "").strip().lower()
    lat, lon = ac.get("lat"), ac.get("lon")

    # "~"-prefixed hexes are synthetic TIS-B/MLAT addresses adsbdb can't know
    route_task = lookup_route(callsign) if callsign else None
    info_task = lookup_aircraft(hex_code) if hex_code and not hex_code.startswith("~") else None
    route = await route_task if route_task else None
    info = await info_task if info_task else None

    dst = ac.get("dst")
    direction = ac.get("dir")
    if lat is not None and lon is not None:
        if dst is None:
            dst = haversine_nm(home_lat, home_lon, lat, lon)
        if direction is None:
            direction = bearing_deg(home_lat, home_lon, lat, lon)

    alt = ac.get("alt_baro", ac.get("alt_geom"))
    on_ground = alt == "ground"
    # look-up angle from home: 0 = on the horizon, 90 = straight overhead
    elev = None
    if not on_ground and isinstance(alt, (int, float)) and dst is not None:
        elev = math.degrees(math.atan2(alt / 6076.115, dst))

    squawk = ac.get("squawk")
    is_emergency = (ac.get("emergency") not in (None, "none")
                    or squawk in ("7500", "7600", "7700"))

    # is the aircraft's track pointed roughly at the viewer?
    approaching = None
    track = ac.get("track")
    if not on_ground and track is not None and direction is not None \
            and (ac.get("gs") or 0) > 40:
        to_viewer = (direction + 180) % 360
        diff = abs((track - to_viewer + 180) % 360 - 180)
        approaching = diff < 60
    plausible = True
    if route and lat is not None and lon is not None:
        plausible = route_is_plausible(route, lat, lon)

    # how far along the origin->destination route the aircraft is (0..1)
    progress = None
    if route and plausible and lat is not None and lon is not None:
        o, d = route.get("origin") or {}, route.get("destination") or {}
        try:
            d_from = haversine_nm(float(o["latitude"]), float(o["longitude"]), lat, lon)
            d_to = haversine_nm(lat, lon, float(d["latitude"]), float(d["longitude"]))
            if d_from + d_to > 0:
                progress = round(d_from / (d_from + d_to), 3)
        except (KeyError, TypeError, ValueError):
            pass

    registration = ac.get("r") or (info or {}).get("registration")
    is_ga = bool(callsign and registration and callsign == registration.replace("-", ""))

    return {
        "hex": hex_code,
        "callsign": callsign or None,
        "flight_iata": (route or {}).get("flight_iata"),
        # curated prefix map first: adsbdb occasionally maps shared ICAO
        # prefixes (e.g. ROU) to the wrong carrier
        "airline": (fallback_airline(callsign) if callsign else None)
                   or (route or {}).get("airline")
                   or ((info or {}).get("owner") if is_ga else None),
        "origin": _airport((route or {}).get("origin")),
        "destination": _airport((route or {}).get("destination")),
        "route_plausible": plausible,
        "route_progress": progress,
        "approaching": approaching,
        "squawk": squawk,
        "emergency": is_emergency,
        "is_ga": is_ga,
        "registration": registration,
        "type_code": ac.get("t"),
        "type_name": (info or {}).get("type_name"),
        "manufacturer": (info or {}).get("manufacturer"),
        "photo_thumb": (info or {}).get("photo_thumb"),
        "photo": (info or {}).get("photo"),
        "lat": lat,
        "lon": lon,
        "alt_ft": None if on_ground or alt is None else alt,
        "on_ground": on_ground,
        "gs_kt": ac.get("gs"),
        "track": ac.get("track"),
        "vert_rate": ac.get("baro_rate", ac.get("geom_rate")),
        "dst_nm": dst,
        "dir_deg": direction,
        "elev_deg": elev,
        "seen": ac.get("seen"),
    }


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app: FastAPI):
    state["client"] = httpx.AsyncClient(
        headers={"User-Agent": "FlightWall-local/1.0 (personal hobby display)"})
    if not os.access(CONFIG_PATH.parent, os.W_OK):
        print(f"WARNING: {CONFIG_PATH.parent} is not writable — settings will "
              f"not survive restarts. On the NAS run: "
              f"sudo chown 1000 /volume1/docker/flightwall/data")
    cfg = state["config"]
    if cfg["lat"] is None or cfg["lon"] is None:
        found = await geolocate(state["client"])
        if found:
            cfg.update(found)
            save_config(cfg)
            print(f"Auto-detected location: {cfg['location_name']} "
                  f"({cfg['lat']:.4f}, {cfg['lon']:.4f}) — edit in the app if wrong.")
    yield
    await state["client"].aclose()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # browsers heuristically cache static files without this, so edits to the
    # frontend would not show up in an open tab even after a reload
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/config")
async def get_config():
    cfg = state["config"]
    return {**cfg, "configured": cfg["lat"] is not None and cfg["lon"] is not None}


@app.post("/api/config")
async def set_config(request: Request):
    body = await request.json()
    cfg = state["config"]
    updates = {}  # validate everything first; apply atomically or not at all
    try:
        if "lat" in body:
            lat = float(body["lat"])
            if not -90 <= lat <= 90:
                raise ValueError
            updates["lat"] = lat
        if "lon" in body:
            lon = float(body["lon"])
            if not -180 <= lon <= 180:
                raise ValueError
            updates["lon"] = lon
        if "radius_nm" in body:
            updates["radius_nm"] = max(1, min(250, float(body["radius_nm"])))
        if "refresh_seconds" in body:
            updates["refresh_seconds"] = max(3, min(120, int(body["refresh_seconds"])))
        if "units" in body and body["units"] in ("imperial", "metric"):
            updates["units"] = body["units"]
        if "display_mode" in body and body["display_mode"] in ("closest", "all"):
            updates["display_mode"] = body["display_mode"]
        if "location_name" in body:
            updates["location_name"] = str(body["location_name"])[:80]
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid values"}, status_code=400)
    cfg.update(updates)
    persisted = save_config(cfg)
    state["point_cache"] = {}
    return {**cfg, "configured": cfg["lat"] is not None and cfg["lon"] is not None,
            "persisted": persisted}


@app.post("/api/locate")
async def locate():
    found = await geolocate(state["client"])
    if not found:
        return JSONResponse({"error": "IP geolocation failed"}, status_code=502)
    cfg = state["config"]
    cfg.update(found)
    persisted = save_config(cfg)
    state["point_cache"] = {}
    return {**cfg, "configured": True, "persisted": persisted}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.api_route("/api/aircraft", methods=["GET", "POST"])
async def aircraft(request: Request):
    cfg = state["config"]
    lat, lon, radius = cfg["lat"], cfg["lon"], cfg["radius_nm"]
    viewer = "home"
    if request.method == "POST":
        # a phone can send its own GPS position (in the body, never the URL);
        # it is used for this one response and never persisted
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            if body.get("lat") is not None and body.get("lon") is not None:
                blat, blon = float(body["lat"]), float(body["lon"])
                if -90 <= blat <= 90 and -180 <= blon <= 180:
                    lat, lon, viewer = blat, blon, "device"
            if body.get("radius_nm") is not None:
                radius = max(1, min(250, float(body["radius_nm"])))
        except (TypeError, ValueError):
            pass  # malformed viewer coords: fall back to the saved home location
    if lat is None or lon is None:
        return JSONResponse({"error": "not configured", "aircraft": []}, status_code=200)
    try:
        raw = await fetch_point(lat, lon, radius)
    except (httpx.HTTPError, ValueError) as exc:
        return JSONResponse(
            {"error": f"aircraft feed unavailable ({type(exc).__name__})", "aircraft": []},
            status_code=200)
    raw = [a for a in raw if a.get("hex")]
    prune_caches()
    enriched = await asyncio.gather(*(enrich(a, lat, lon) for a in raw))
    enriched.sort(key=lambda a: a["dst_nm"] if a["dst_nm"] is not None else 9999)
    return {"aircraft": enriched, "radius_nm": radius, "viewer": viewer, "error": None}


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app,
                host=os.environ.get("FLIGHTWALL_HOST", "127.0.0.1"),
                port=int(os.environ.get("FLIGHTWALL_PORT")
                         or os.environ.get("PORT")  # dev harness auto-port
                         or 8484))
