"""Long-term context from NASA's OMNI dataset (hourly, 2000 to ~1 month ago).

Our own archive only goes back as far as the pipeline has been running. OMNI
is NASA's cleaned, merged record of the solar wind near Earth, built from the
same kind of L1 spacecraft and already time-shifted to Earth's bow shock. It
lets the dashboard show years, seasons and the solar cycle, and gives the
Kp analysis thousands of samples instead of a few dozen.

Fetched through NASA CDAWeb's HAPI service, cached, and refreshed weekly.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HAPI = "https://cdaweb.gsfc.nasa.gov/hapi/data"
DATASET = "OMNI2_H0_MRG1HR"
# HAPI requires parameters in the dataset's own order
PARAMS = "BY_GSM1800,BZ_GSM1800,N1800,V1800,R1800,KP1800,DST1800"
COLS = ["time_tag", "by", "bz", "n", "v", "sunspot", "kp", "dst"]
FILL = {"by": 999.9, "bz": 999.9, "n": 999.9, "v": 9999, "sunspot": 999, "kp": 99, "dst": 99999}
START = datetime(2000, 1, 1, tzinfo=timezone.utc)


def parse(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), header=None, names=COLS)
    df["time_tag"] = pd.to_datetime(df["time_tag"], utc=True)
    for c, f in FILL.items():
        df[c] = pd.to_numeric(df[c], errors="coerce")
        df.loc[df[c] >= f * 0.999, c] = np.nan  # fill values mean "no data"
    df["kp"] = df["kp"] / 10  # OMNI stores Kp x 10 (e.g. 33 = 3.3)
    return df


def _download(start: datetime, stop: datetime) -> pd.DataFrame:
    frames, t = [], start
    while t < stop:  # one year per request keeps each response small
        t2 = min(datetime(t.year + 1, 1, 1, tzinfo=timezone.utc), stop)
        r = requests.get(HAPI, params={"id": DATASET, "parameters": PARAMS,
                                       "time.min": f"{t:%Y-%m-%dT%H:%M:%SZ}", "time.max": f"{t2:%Y-%m-%dT%H:%M:%SZ}"},
                         timeout=120)
        r.raise_for_status()
        if r.text.strip():
            frames.append(parse(r.text))
        t = t2
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)


def load_omni(history_dir: Path, offline_dir: str | None = None, max_age_days: int = 7) -> pd.DataFrame:
    """Hourly OMNI since 2000. Re-downloads only the last 90 days once a week
    (OMNI revises recent data), so a normal run costs nothing."""
    if offline_dir:
        p = Path(offline_dir, "omni.csv")
        if p.exists():
            return parse(p.read_text(encoding="utf-8"))
        cache = history_dir / "omni_hourly.csv.gz"   # offline but with a saved cache: use it
        if not cache.exists():
            return pd.DataFrame(columns=COLS)
        df = pd.read_csv(cache, parse_dates=["time_tag"])
        df["time_tag"] = pd.to_datetime(df["time_tag"], utc=True)
        return df
    cache, meta_p = history_dir / "omni_hourly.csv.gz", history_dir / "omni_meta.json"
    now = datetime.now(timezone.utc)
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    fetched = datetime.fromisoformat(meta["fetched_at"]) if "fetched_at" in meta else None
    old = pd.DataFrame(columns=COLS)
    if cache.exists():
        old = pd.read_csv(cache, parse_dates=["time_tag"])
        old["time_tag"] = pd.to_datetime(old["time_tag"], utc=True)
    if fetched and now - fetched < timedelta(days=max_age_days) and len(old):
        return old
    try:
        start = (old["time_tag"].max() - pd.Timedelta(days=90)).to_pydatetime() if len(old) else START
        new = _download(start, now)
        df = pd.concat([old[old["time_tag"] < start], new], ignore_index=True) if len(old) else new
        df = df.drop_duplicates("time_tag", keep="last").sort_values("time_tag")
        history_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False, compression="gzip")
        meta_p.write_text(json.dumps({"fetched_at": now.isoformat()}), encoding="utf-8")
        return df
    except Exception as e:
        print(f"OMNI unavailable this run: {e}")
        return old
