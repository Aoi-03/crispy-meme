# logiswift_server.py
import os
import time
import json
import logging
import requests
import urllib.parse
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from typing import List, Dict, Tuple, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
CORS(app)

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")  # store securely in env var
GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

# Constraints
MIN_STOPS = 5
MAX_STOPS = 10
REQUEST_TIMEOUT = 15  # seconds for external calls

# ---- Utilities ----
def normalize_addresses(raw_text: str) -> List[str]:
    """Trim lines, remove empties, dedupe preserving order."""
    lines = [line.strip() for line in raw_text.splitlines()]
    cleaned = []
    seen = set()
    for l in lines:
        if not l:
            continue
        if l.lower() in seen:
            continue
        seen.add(l.lower())
        cleaned.append(l)
    return cleaned

def build_directions_params(origin: str, destination: str, waypoints: List[str],
                            optimize: bool = False) -> Dict:
    """Return params dict for Google Directions API."""
    params = {
        "origin": origin,
        "destination": destination,
        "key": GOOGLE_MAPS_API_KEY,
        "mode": "driving",
        "units": "metric"
    }
    if waypoints:
        wp = "|".join(waypoints)
        if optimize:
            params["waypoints"] = "optimize:true|" + wp
        else:
            params["waypoints"] = wp
    return params

def call_google_directions(params: Dict) -> Dict:
    """Call Directions API and return parsed JSON. Raises on non-200 or status!=OK (but returns body)."""
    r = requests.get(GOOGLE_DIRECTIONS_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def sum_route_distance_and_time(route: Dict) -> Tuple[int, int]:
    """
    Given a route (one element of 'routes' array), sum legs distance and duration.
    Returns (distance_meters, duration_seconds)
    """
    legs = route.get("legs", [])
    total_m = 0
    total_s = 0
    for leg in legs:
        d = leg.get("distance", {}).get("value")
        t = leg.get("duration", {}).get("value")
        if isinstance(d, (int, float)):
            total_m += int(d)
        if isinstance(t, (int, float)):
            total_s += int(t)
    return total_m, total_s

def meters_to_km_str(m: int) -> str:
    return f"{m/1000:.2f} km"

def seconds_to_human(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m = s // 60
    h = m // 60
    m = m % 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"

def make_google_maps_driver_link(origin: str, dest: str, waypoints_ordered: List[str]) -> str:
    """
    Compose a single Google Maps directions URL that opens the route with ordered waypoints.
    We'll use the universal "https://www.google.com/maps/dir/?api=1" URL.
    """
    base = "https://www.google.com/maps/dir/?api=1"
    params = {
        "origin": origin,
        "destination": dest,
        # waypoints as pipe-separated
    }
    if waypoints_ordered:
        params["waypoints"] = "|".join(waypoints_ordered)
    q = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
    return f"{base}&{q}"

# ---- Mock helpers ----
def deterministic_mock_optimization(addresses: List[str]) -> Dict:
    """
    Create deterministic mock results to use when API key is missing.
    Reorders by simple heuristic (reverse) and gives fake distances.
    """
    # ensure deterministic: reverse order except keep origin same
    origin = addresses[0]
    dest = addresses[-1]
    middle = addresses[1:-1]
    optimized_middle = list(reversed(middle))
    optimized = [origin] + optimized_middle + [dest] if len(addresses) >= 2 else addresses[:]
    # compute fake distances: sum of 3km per leg
    legs = max(1, len(optimized)-1)
    distance_m = legs * 3000
    duration_s = legs * 10 * 60
    return {
        "ok": True,
        "optimized_order": optimized,
        "original_order": addresses,
        "original_distance_m": distance_m,
        "optimized_distance_m": distance_m - int(0.15 * distance_m),  # pretend 15% savings
        "original_duration_s": duration_s,
        "optimized_duration_s": duration_s - int(0.12 * duration_s),
        "driver_link": make_google_maps_driver_link(origin, dest, optimized[1:-1]),
        "note": "mock mode (no Google API key)"
    }

# ---- Main endpoint ----
@app.route("/optimize", methods=["POST", "OPTIONS"])
def optimize_route():
    """
    Expected JSON:
    {
      "addresses_text": "addr1\naddr2\naddr3\n...",   // 5-10 lines
      "roundtrip": false,   // optional: if true, treat route as round-trip (origin=last?)
      "mock": false         // optional override for mock mode
    }
    """
    if request.method == "OPTIONS":
        return make_response("", 200)
    payload = request.get_json(force=True, silent=True) or {}
    addresses_text = payload.get("addresses_text", "")
    roundtrip = bool(payload.get("roundtrip", False))
    force_mock = bool(payload.get("mock", False))

    # normalize and validate
    addresses = normalize_addresses(addresses_text)
    if not addresses:
        return jsonify({"ok": False, "error": "No addresses provided. Paste 5–10 addresses, one per line."}), 400

    # Limit to configured bounds
    if len(addresses) < MIN_STOPS or len(addresses) > MAX_STOPS:
        return jsonify({"ok": False, "error": f"Provide between {MIN_STOPS} and {MAX_STOPS} unique addresses (you gave {len(addresses)})."}), 400

    # Mock mode if no API key or explicitly requested
    if force_mock or not GOOGLE_MAPS_API_KEY:
        logging.info("Running in mock mode (no Google API key or forced).")
        data = deterministic_mock_optimization(addresses)
        return jsonify({"ok": True, "mock": True, **data})

    try:
        # Decide origin/destination/waypoints. Use first as origin, last as destination.
        origin = addresses[0]
        destination = addresses[-1]
        waypoints = addresses[1:-1]  # may be empty if only 2 addresses (but we require >=5)

        # 1) Call Directions WITHOUT optimization to measure "original" distance/time.
        params_original = build_directions_params(origin, destination, waypoints, optimize=False)
        logging.info("Calling Google Directions (non-optimized)...")
        res_orig = call_google_directions(params_original)
        if res_orig.get("status") != "OK":
            # handle permissive statuses
            err_msg = f"Directions API (non-optimized) error: {res_orig.get('status')}"
            # Surface helpful GOOGLE API error messages (over query limit etc)
            if "error_message" in res_orig:
                err_msg += f": {res_orig.get('error_message')}"
            return jsonify({"ok": False, "error": err_msg, "details": res_orig}), 502

        route_orig = res_orig["routes"][0]
        orig_dist_m, orig_dur_s = sum_route_distance_and_time(route_orig)

        # 2) Call Directions WITH optimizeWaypoints=true
        params_opt = build_directions_params(origin, destination, waypoints, optimize=True)
        logging.info("Calling Google Directions (optimized)...")
        res_opt = call_google_directions(params_opt)
        if res_opt.get("status") != "OK":
            err_msg = f"Directions API (optimized) error: {res_opt.get('status')}"
            if "error_message" in res_opt:
                err_msg += f": {res_opt.get('error_message')}"
            return jsonify({"ok": False, "error": err_msg, "details": res_opt}), 502

        route_opt = res_opt["routes"][0]
        opt_dist_m, opt_dur_s = sum_route_distance_and_time(route_opt)

        # Google returns waypoint_order: mapping of indices of supplied waypoints to optimized order
        # waypoint_order is a list of integers referring to position in waypoints list.
        waypoint_order = res_opt["routes"][0].get("waypoint_order", [])

        # Construct resolved addresses: Google may give leg end addresses; prefer geocoded_waypoint->place_id info if available.
        # We'll map optimized order back to full address strings.
        original_order = [origin] + waypoints + [destination]

        # Build optimized order list of addresses
        optimized_order = [origin]
        for idx in waypoint_order:
            # idx refers to the index inside waypoints list (0..len(waypoints)-1)
            if 0 <= idx < len(waypoints):
                optimized_order.append(waypoints[idx])
        optimized_order.append(destination)

        # If waypoint_order length doesn't match, fall back to original
        if len(optimized_order) != len(original_order):
            optimized_order = original_order[:]

        # Create driver-ready Google Maps link with optimized waypoint order (exclude origin/destination)
        driver_link = make_google_maps_driver_link(origin, destination, optimized_order[1:-1])

        result = {
            "ok": True,
            "mock": False,
            "original": {
                "order": original_order,
                "distance_m": orig_dist_m,
                "duration_s": orig_dur_s
            },
            "optimized": {
                "order": optimized_order,
                "distance_m": opt_dist_m,
                "duration_s": opt_dur_s,
                "waypoint_order_indices": waypoint_order
            },
            "savings": {
                "distance_m_saved": max(0, orig_dist_m - opt_dist_m),
                "distance_pct_saved": round(100 * (orig_dist_m - opt_dist_m) / max(1, orig_dist_m), 2),
                "duration_s_saved": max(0, orig_dur_s - opt_dur_s)
            },
            "driver_link": driver_link
        }
        return jsonify(result)
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "Request to Google Directions timed out. Try again."}), 504
    except requests.exceptions.HTTPError as e:
        status_code = getattr(e.response, "status_code", 502)
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text if e.response is not None else str(e)
        return jsonify({"ok": False, "error": "Google Directions HTTP error", "details": body}), status_code
    except Exception as e:
        logging.exception("Unexpected error in /optimize")
        return jsonify({"ok": False, "error": "Unexpected server error", "details": str(e)}), 500

# Simple health route
@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "LogiSwift Route Optimizer", "mock_mode": not bool(GOOGLE_MAPS_API_KEY)})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info("Starting LogiSwift server on 0.0.0.0:%d (mock_mode=%s)", port, not bool(GOOGLE_MAPS_API_KEY))
    app.run(host="0.0.0.0", port=port, debug=False)