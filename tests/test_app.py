"""FlightWall test suite. No network: every upstream call is stubbed."""

import json

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient with a known config, isolated config file, and no geolocation."""
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")

    async def no_geolocate(_client):
        return None

    monkeypatch.setattr(server, "geolocate", no_geolocate)
    original = dict(server.state["config"])
    server.state["config"].update(
        {"lat": 45.4348, "lon": -73.8629, "location_name": "Test Home",
         "radius_nm": 30, "refresh_seconds": 8, "units": "imperial",
         "display_mode": "closest"})
    server.state["point_cache"] = {}
    server.state["route_cache"] = {}
    server.state["aircraft_cache"] = {}
    with TestClient(server.app) as c:
        yield c
    server.state["config"].clear()
    server.state["config"].update(original)


def stub_feed(monkeypatch, aircraft_list, seen_coords=None):
    async def fake_fetch_point(lat, lon, radius_nm):
        if seen_coords is not None:
            seen_coords.append((lat, lon, radius_nm))
        return aircraft_list

    async def fake_route(callsign):
        return None

    aircraft_lookups = []

    async def fake_aircraft(hex_code):
        aircraft_lookups.append(hex_code)
        return None

    monkeypatch.setattr(server, "fetch_point", fake_fetch_point)
    monkeypatch.setattr(server, "lookup_route", fake_route)
    monkeypatch.setattr(server, "lookup_aircraft", fake_aircraft)
    return aircraft_lookups


# ---------------------------------------------------------------- geo math

def test_haversine_known_distance():
    # YUL to YYZ is ~275 nm
    d = server.haversine_nm(45.4706, -73.7408, 43.6772, -79.6306)
    assert 265 < d < 285


def test_route_plausibility_rejects_stale_route():
    # adsbdb once returned ONT->SJC for a plane flying near Washington DC
    route = {"origin": {"latitude": 34.056, "longitude": -117.601, "iata_code": "ONT"},
             "destination": {"latitude": 37.363, "longitude": -121.929, "iata_code": "SJC"}}
    assert server.route_is_plausible(route, 38.85, -77.87) is False


def test_route_plausibility_accepts_mid_route_aircraft():
    route = {"origin": {"latitude": 40.64, "longitude": -73.78, "iata_code": "JFK"},
             "destination": {"latitude": 33.94, "longitude": -118.41, "iata_code": "LAX"}}
    assert server.route_is_plausible(route, 39.0, -95.0) is True


def test_route_plausibility_rejects_same_airport():
    route = {"origin": {"latitude": 45.47, "longitude": -73.74, "iata_code": "YUL"},
             "destination": {"latitude": 45.47, "longitude": -73.74, "iata_code": "YUL"}}
    assert server.route_is_plausible(route, 45.5, -73.8) is False


def test_fallback_airline():
    assert server.fallback_airline("ROU1781") == "Air Canada Rouge"
    assert server.fallback_airline("CGOVG") is None      # registration-style callsign
    assert server.fallback_airline("N126JH") is None     # GA tail number


# ---------------------------------------------------------------- config

def test_load_config_coerces_hand_edited_types(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "lat": "45.5", "lon": "-73.6",       # strings must become floats
        "radius_nm": "not a number",          # junk must fall back to default
        "refresh_seconds": 99999,             # out of range must clamp
        "units": "furlongs",                  # unknown must fall back
    }), encoding="utf-8")
    monkeypatch.setattr(server, "CONFIG_PATH", path)
    cfg = server.load_config()
    assert cfg["lat"] == 45.5 and cfg["lon"] == -73.6
    assert cfg["radius_nm"] == server.DEFAULT_CONFIG["radius_nm"]
    assert cfg["refresh_seconds"] == 120
    assert cfg["units"] == "imperial"


def test_load_config_out_of_range_coords_reset(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"lat": 91, "lon": 0}), encoding="utf-8")
    monkeypatch.setattr(server, "CONFIG_PATH", path)
    cfg = server.load_config()
    assert cfg["lat"] is None and cfg["lon"] is None


def test_set_config_is_atomic_on_invalid_input(client):
    before = dict(server.state["config"])
    r = client.post("/api/config", json={"lat": 12.34, "lon": "oops"})
    assert r.status_code == 400
    assert server.state["config"] == before  # nothing half-applied


def test_set_config_saves_and_round_trips(client):
    r = client.post("/api/config", json={"radius_nm": 45, "units": "metric"})
    assert r.status_code == 200
    assert r.json()["radius_nm"] == 45
    saved = json.loads(server.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["radius_nm"] == 45 and saved["units"] == "metric"


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ---------------------------------------------------------------- aircraft API

RAW_JET = {"hex": "abf96c", "flight": "SWA2309 ", "r": "N8705Q", "t": "B38M",
           "alt_baro": 20000, "gs": 400.0, "track": 250.0, "baro_rate": 1600,
           "lat": 45.5, "lon": -73.9, "dst": 5.0, "dir": 300.0, "seen": 0.1}
RAW_TISB = {"hex": "~29a6f0", "alt_baro": 3000, "gs": 100.0,
            "lat": 45.4, "lon": -73.8, "dst": 2.0, "dir": 100.0, "seen": 1.0}


def test_aircraft_sorted_and_enriched(client, monkeypatch):
    stub_feed(monkeypatch, [RAW_JET, RAW_TISB])
    r = client.post("/api/aircraft", json={})
    body = r.json()
    assert body["error"] is None and body["viewer"] == "home"
    assert [a["hex"] for a in body["aircraft"]] == ["~29a6f0", "abf96c"]  # by distance
    jet = body["aircraft"][1]
    assert jet["callsign"] == "SWA2309"
    assert jet["airline"] == "Southwest Airlines"  # prefix fallback, no route stub
    assert jet["alt_ft"] == 20000 and jet["dst_nm"] == 5.0
    # elevation: 20000 ft at 5 nm ground distance is ~33 degrees up
    assert 30 < jet["elev_deg"] < 36


def test_tisb_hex_skips_aircraft_lookup(client, monkeypatch):
    lookups = stub_feed(monkeypatch, [RAW_JET, RAW_TISB])
    client.post("/api/aircraft", json={})
    assert "abf96c" in lookups and "~29a6f0" not in lookups


def test_viewer_coords_override_home(client, monkeypatch):
    seen = []
    stub_feed(monkeypatch, [], seen_coords=seen)
    r = client.post("/api/aircraft", json={"lat": 51.5, "lon": -0.12})
    assert r.json()["viewer"] == "device"
    assert seen[0][0] == 51.5 and seen[0][1] == -0.12


def test_malformed_viewer_coords_fall_back_to_home(client, monkeypatch):
    seen = []
    stub_feed(monkeypatch, [], seen_coords=seen)
    r = client.post("/api/aircraft", json={"lat": "junk", "lon": None})
    assert r.json()["viewer"] == "home"
    assert seen[0][0] == pytest.approx(45.4348)


def test_out_of_range_viewer_coords_fall_back_to_home(client, monkeypatch):
    seen = []
    stub_feed(monkeypatch, [], seen_coords=seen)
    r = client.post("/api/aircraft", json={"lat": 95, "lon": 200})
    assert r.json()["viewer"] == "home"
    assert seen[0][0] == pytest.approx(45.4348)


def test_ground_aircraft_has_no_altitude_or_elevation(client, monkeypatch):
    grounded = dict(RAW_JET, alt_baro="ground", baro_rate=None)
    stub_feed(monkeypatch, [grounded])
    a = client.post("/api/aircraft", json={}).json()["aircraft"][0]
    assert a["on_ground"] is True and a["alt_ft"] is None and a["elev_deg"] is None


def test_not_configured(client, monkeypatch):
    server.state["config"]["lat"] = None
    server.state["config"]["lon"] = None
    r = client.post("/api/aircraft", json={})
    assert r.json()["error"] == "not configured"
