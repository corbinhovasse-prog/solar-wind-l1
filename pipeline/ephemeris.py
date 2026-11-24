"""Where are the spacecraft? Positions from NASA JPL Horizons.

NOAA's solar wind feed says WHAT each spacecraft measures, not WHERE it is.
JPL Horizons (the system NASA uses for spacecraft navigation) gives each
one's position. We ask for positions relative to Earth's centre, then turn
them into GSE coordinates, the standard frame for solar wind work:

    X = toward the Sun    Y = along Earth's orbit (dusk side)    Z = ecliptic north

In GSE, X is "how far upstream" a spacecraft sits, which sets its warning
time, and Y/Z show how far it is off the Sun-Earth line on its halo orbit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HORIZONS = "https://ssd.jpl.nasa.gov/api/horizons.api"
SPACECRAFT = {"SOLAR1": "-231", "IMAP": "-43", "ACE": "-92"}  # Horizons IDs (SOLAR-1 is listed as SWFO-L1)
SUN = "10"


def parse_vectors(text: str) -> pd.DataFrame:
    """Rows between $$SOE and $$EOE look like:
    2461361.500000000, A.D. 2026-Nov-17 00:00:00.0000, -8.63E+07, -1.20E+08,  7.91E+03,"""
    body = text.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    rows = []
    for line in body.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        t = datetime.strptime(parts[1].replace("A.D. ", ""), "%Y-%b-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        rows.append((t, float(parts[2]), float(parts[3]), float(parts[4])))
    return pd.DataFrame(rows, columns=["time_tag", "x", "y", "z"])


def query(target: str, start: datetime, stop: datetime, step: str = "1h") -> pd.DataFrame:
    params = {
        "format": "text", "COMMAND": f"'{target}'", "OBJ_DATA": "NO", "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "VECTORS", "CENTER": "'500@399'",  # 500@399 = Earth's centre
        "START_TIME": f"'{start:%Y-%m-%d %H:%M}'", "STOP_TIME": f"'{stop:%Y-%m-%d %H:%M}'",
        "STEP_SIZE": f"'{step}'", "VEC_TABLE": "'1'", "REF_PLANE": "'ECLIPTIC'",
        "OUT_UNITS": "'KM-S'", "CSV_FORMAT": "'YES'",
    }
    r = requests.get(HORIZONS, params=params, timeout=60)
    r.raise_for_status()
    return parse_vectors(r.text)


def to_gse(sc: pd.DataFrame, sun: pd.DataFrame) -> pd.DataFrame:
    """Rotate Earth-centred ecliptic positions into GSE using the Sun's direction at each time."""
    m = sc.merge(sun, on="time_tag", suffixes=("", "_sun"))
    s = m[["x_sun", "y_sun", "z_sun"]].to_numpy()
    xhat = s / np.linalg.norm(s, axis=1, keepdims=True)                 # toward the Sun
    zhat = np.tile([0.0, 0.0, 1.0], (len(m), 1))                        # ecliptic north
    yhat = np.cross(zhat, xhat)
    yhat /= np.linalg.norm(yhat, axis=1, keepdims=True)                 # completes the right-handed frame
    zhat = np.cross(xhat, yhat)
    p = m[["x", "y", "z"]].to_numpy()
    return pd.DataFrame({
        "time_tag": m["time_tag"],
        "x_gse": (p * xhat).sum(1), "y_gse": (p * yhat).sum(1), "z_gse": (p * zhat).sum(1),
    })


BODIES = {"MOON": "301", "VENUS": "299", "MARS": "499"}


def fetch_positions(offline_dir: str | None = None, days: int = 180) -> tuple[pd.DataFrame, dict]:
    """Returns (tracks, bodies).
    tracks: 6-hourly GSE positions (km) of each spacecraft over the last `days`,
            long enough to trace a whole halo-orbit loop (about 6 months).
    bodies: current GSE positions of the Sun, Moon, Venus and Mars, for distances.
    Never raises: positions are a bonus, so the pipeline still runs without them."""
    if offline_dir:
        p = Path(offline_dir, "positions.json")
        if not p.exists():
            return pd.DataFrame(), {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        df = pd.DataFrame(raw["tracks"])
        df["time_tag"] = pd.to_datetime(df["time_tag"], utc=True)
        return df, raw["bodies"]
    now = datetime.now(timezone.utc)
    stop = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    start = stop - timedelta(days=days)
    try:
        sun = query(SUN, start, stop, "6h")
        frames = []
        for name, sc_id in SPACECRAFT.items():
            g = to_gse(query(sc_id, start, stop, "6h"), sun)
            g["source"] = name
            frames.append(g)
        tracks = pd.concat(frames, ignore_index=True)
        t0 = stop
        sun_now = query(SUN, t0, t0 + timedelta(hours=1), "1h").head(1)
        bodies = {"SUN": {"x_gse": float(np.linalg.norm(sun_now[["x", "y", "z"]].to_numpy())), "y_gse": 0.0, "z_gse": 0.0}}
        for name, bid in BODIES.items():
            g = to_gse(query(bid, t0, t0 + timedelta(hours=1), "1h").head(1), sun_now).iloc[0]
            bodies[name] = {"x_gse": float(g.x_gse), "y_gse": float(g.y_gse), "z_gse": float(g.z_gse)}
        return tracks, bodies
    except Exception as e:  # network trouble, format change: skip this run's positions
        print(f"positions unavailable this run: {e}")
        return pd.DataFrame(), {}
