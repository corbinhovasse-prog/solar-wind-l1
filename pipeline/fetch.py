"""Fetch and parse NOAA SWPC real-time solar wind (RTSW) feeds.

The RTSW 1-minute feeds carry measurements from every spacecraft currently
reporting from the Sun-Earth L1 point (as of late 2026: NOAA SOLAR-1,
NASA IMAP and NASA ACE), one row per spacecraft per minute. The `active`
flag marks the spacecraft SWPC is using operationally at that minute.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

URLS = {
    "wind": "https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json",
    "mag": "https://services.swpc.noaa.gov/json/rtsw/rtsw_mag_1m.json",
    "kp": "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
}

WIND_COLS = ["proton_speed", "proton_density", "proton_temperature"]
MAG_COLS = ["bt", "bx_gsm", "by_gsm", "bz_gsm"]
HEADERS = {"User-Agent": "solar-wind-l1 student pipeline (github.com)"}


def get_json(url: str, timeout: int = 90):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def to_frame(raw) -> pd.DataFrame:
    """SWPC uses two layouts: a list of dicts, or a list of lists whose first
    row is the header. Accept either so a format change doesn't break us."""
    if not raw:
        return pd.DataFrame()
    if isinstance(raw[0], dict):
        return pd.DataFrame(raw)
    return pd.DataFrame(raw[1:], columns=raw[0])


def _clean(df: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
    keep = ["time_tag", "source", "active", "overall_quality"] + value_cols
    df = df[[c for c in keep if c in df.columns]].copy()
    if "source" not in df.columns:
        df["source"] = "UNKNOWN"
    if "active" not in df.columns:
        df["active"] = True
    df["time_tag"] = pd.to_datetime(df["time_tag"], utc=True, errors="coerce")
    for c in value_cols + ["overall_quality"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["active"] = df["active"].astype(str).str.lower().eq("true")
    present = [c for c in value_cols if c in df.columns]
    df = df.dropna(subset=["time_tag"]).dropna(subset=present, how="all")
    return df.drop_duplicates(subset=["time_tag", "source"], keep="last")


def merge_wind_mag(wind_raw, mag_raw) -> pd.DataFrame:
    """One row per (minute, spacecraft) with plasma + magnetic field columns."""
    wind = _clean(to_frame(wind_raw), WIND_COLS)
    mag = _clean(to_frame(mag_raw), MAG_COLS)
    wind = wind.rename(columns={"active": "active_wind", "overall_quality": "q_wind"})
    mag = mag.rename(columns={"active": "active_mag", "overall_quality": "q_mag"})
    df = pd.merge(wind, mag, on=["time_tag", "source"], how="outer")
    df["active"] = df.get("active_wind", False).fillna(False) | df.get("active_mag", False).fillna(False)
    return df.sort_values(["time_tag", "source"]).reset_index(drop=True)


def load_kp(raw) -> pd.DataFrame:
    df = to_frame(raw)
    df.columns = [c.lower() for c in df.columns]
    df["time_tag"] = pd.to_datetime(df["time_tag"], utc=True, errors="coerce")
    df["kp"] = pd.to_numeric(df["kp"], errors="coerce")
    return df[["time_tag", "kp"]].dropna()


def fetch_all(offline_dir: str | None = None) -> dict:
    """Return raw JSON for each feed, from the web or from saved files."""
    out = {}
    for name, url in URLS.items():
        if offline_dir:
            out[name] = json.loads(Path(offline_dir, f"{name}.json").read_text())
        else:
            out[name] = get_json(url)
    return out
