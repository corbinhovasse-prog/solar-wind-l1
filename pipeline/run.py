"""End-to-end batch run: fetch -> archive -> features -> analysis -> dashboard JSON.

    python -m pipeline.run                        # live data
    python -m pipeline.run --offline sample_data  # saved files (no network)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .ephemeris import fetch_positions
from .features import add_features
from .fetch import fetch_all, load_kp, merge_wind_mag
from .insights import agreement_block, live_kp_link, longterm_block, separation_block
from .models import active_series, coverage, cross_spacecraft, detect_anomalies
from .omni import load_omni

MINUTE_DAYS = 30     # full 1-minute detail kept this long
KP_DAYS = 730        # Kp history kept this long
LIGHT_KMS = 299_792.458


# ---------------------------------------------------------------- archives
def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["time_tag"] = pd.to_datetime(df["time_tag"], utc=True)
    return df


def _write(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def update_archives(minute: pd.DataFrame, kp: pd.DataFrame, hist_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The live feeds only cover about a day. Each run appends to:
      rtsw_1m.csv.gz      every spacecraft-minute, last 30 days
      rtsw_hourly.csv.gz  hourly means per spacecraft, kept forever (small)
      kp.csv.gz           Kp history, last 2 years"""
    p = hist_dir / "rtsw_1m.csv.gz"
    m = pd.concat([_read(p), minute], ignore_index=True).drop_duplicates(["time_tag", "source"], keep="last")
    m = m[m["time_tag"] >= m["time_tag"].max() - pd.Timedelta(days=MINUTE_DAYS)].sort_values(["time_tag", "source"])
    _write(m, p)

    hp = hist_dir / "rtsw_hourly.csv.gz"
    cols = ["proton_speed", "proton_density", "bz_gsm", "bt"]
    hourly = (m.set_index("time_tag").groupby("source")[cols].resample("1h").mean()
              .dropna(how="all").reset_index())
    old_h = _read(hp)
    hh = pd.concat([old_h, hourly], ignore_index=True).drop_duplicates(["time_tag", "source"], keep="last")
    _write(hh.sort_values(["time_tag", "source"]), hp)

    kpp = hist_dir / "kp.csv.gz"
    k = pd.concat([_read(kpp), kp], ignore_index=True).drop_duplicates("time_tag", keep="last")
    k = k[k["time_tag"] >= k["time_tag"].max() - pd.Timedelta(days=KP_DAYS)].sort_values("time_tag")
    _write(k, kpp)
    return m.reset_index(drop=True), k.reset_index(drop=True)


# ---------------------------------------------------------------- helpers
def _c(v, d=3):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else round(float(v), d)
    return v


def _series(frame: pd.DataFrame, cols: dict) -> dict:
    out = {"t": [t.isoformat() for t in frame.index]}
    for key, col in cols.items():
        out[key] = [_c(v, 2) for v in frame[col]] if col in frame else []
    return out


def live_series(m: pd.DataFrame, s: pd.DataFrame, kp: pd.DataFrame) -> dict:
    """Two resolutions: 5-minute for the last 3 days, hourly for the last 30."""
    end = s.index.max()
    out = {}
    for name, rule, days in [("fine", "5min", 3), ("hourly", "1h", 30)]:
        lo = end - pd.Timedelta(days=days)
        per = {}
        for src, g in m[m.time_tag >= lo].groupby("source"):
            g = g.set_index("time_tag")[["proton_speed", "proton_density", "bz_gsm"]].resample(rule).mean()
            per[src] = _series(g, {"speed": "proton_speed", "density": "proton_density", "bz": "bz_gsm"})
        a = s[s.index >= lo][["proton_speed", "proton_density", "bz_gsm", "pdyn_npa", "newell"]].resample(rule).mean()
        out[name] = {"per_source": per, "active": _series(a, {"speed": "proton_speed", "density": "proton_density",
                                                               "bz": "bz_gsm", "pdyn": "pdyn_npa", "newell": "newell"})}
    k = kp.set_index("time_tag")["kp"]
    out["kp"] = {"t": [t.isoformat() for t in k.index], "kp": [float(v) for v in k]}
    return out


def positions_block(tracks: pd.DataFrame, bodies: dict, v_now: float, v_typ: float, cross: list[dict]) -> dict:
    if tracks.empty:
        return {"available": False}
    out = {"available": True, "speed_used": round(v_now, 1), "tracks": {}, "latest": {}, "bodies": {}}
    for k, b in bodies.items():
        out["bodies"][k] = {kk: round(vv) for kk, vv in b.items()}
    for src, g in tracks.sort_values("time_tag").groupby("source"):
        out["tracks"][src] = {
            "t": [t.isoformat() for t in g["time_tag"]],
            "x": [round(v / 1e3, 1) for v in g["x_gse"]],   # thousands of km
            "y": [round(v / 1e3, 1) for v in g["y_gse"]],
            "z": [round(v / 1e3, 1) for v in g["z_gse"]],
        }
        last = g.iloc[-1]
        p = np.array([last.x_gse, last.y_gse, last.z_gse])
        dist = {"EARTH": float(np.linalg.norm(p))}
        for name, b in bodies.items():
            dist[name] = float(np.linalg.norm(p - np.array([b["x_gse"], b["y_gse"], b["z_gse"]])))
        planets = {k: v for k, v in dist.items() if k in ("VENUS", "MARS")}
        out["latest"][src] = {
            "x_km": round(float(last.x_gse)), "y_km": round(float(last.y_gse)), "z_km": round(float(last.z_gse)),
            "off_axis_km": round(float(np.hypot(last.y_gse, last.z_gse))),
            "lead_time_min": round(float(last.x_gse) / v_now / 60, 1),
            "dist_km": {k: round(v) for k, v in dist.items()},
            "nearest_planet": min(planets, key=planets.get) if planets else None,
        }
    L = out["latest"]
    for row in cross:
        a, b = row["pair"].split(" vs ")
        if a in L and b in L:
            # minutes by which b sees the wind before a (same sign as bz_best_lag_min)
            row["position_lag_min"] = round((L[b]["x_km"] - L[a]["x_km"]) / v_typ / 60, 1)
            row["separation_km"] = round(float(np.linalg.norm(
                [L[a][k] - L[b][k] for k in ("x_km", "y_km", "z_km")])))
    out["light_kms"] = LIGHT_KMS
    return out


# ---------------------------------------------------------------- run
def run(offline: str | None, out_dir: Path, hist_dir: Path) -> dict:
    raw = fetch_all(offline)
    minute = merge_wind_mag(raw["wind"], raw["mag"])
    m, kp = update_archives(minute, load_kp(raw["kp"]), hist_dir)

    s = add_features(active_series(m))
    latest_t = s.index.max()
    week = s[s.index >= latest_t - pd.Timedelta(days=7)]
    _, anomalies = detect_anomalies(week)

    def recent(col, minutes=60):
        v = s[col].dropna() if col in s else pd.Series(dtype=float)
        if v.empty or v.index[-1] < latest_t - pd.Timedelta(minutes=minutes):
            return None, None
        return v.iloc[-1], v.index[-1]

    speed, plasma_t = recent("proton_speed")
    bz, mag_t = recent("bz_gsm")
    plasma_src = s.loc[plasma_t, "plasma_source"] if plasma_t is not None else None
    sp = s["proton_speed"].dropna()
    v_now = float(speed) if speed else (float(sp.iloc[-1]) if len(sp) else 400.0)
    v_typ = float(week["proton_speed"].median()) if week["proton_speed"].notna().any() else 400.0

    # what happened at Earth after each flagged event (Kp in the following 6 hours)
    k = kp.set_index("time_tag")["kp"]
    for e in anomalies:
        t0 = pd.Timestamp(e["start"])
        after = k[(k.index >= t0.floor("3h")) & (k.index <= t0 + pd.Timedelta(hours=6))]
        e["kp_after"] = float(after.max()) if len(after) else None

    tracks, bodies = fetch_positions(offline)
    cross = cross_spacecraft(m[m.time_tag >= latest_t - pd.Timedelta(days=7)])
    positions = positions_block(tracks, bodies, v_now, v_typ, cross)

    lead = recent("lead_time_min")[0]
    if positions.get("available") and plasma_src in positions["latest"] and speed:
        # warning time from the measuring spacecraft's real distance, not a fixed 1.5 million km
        lead = positions["latest"][plasma_src]["x_km"] / float(speed) / 60
        s["lead_time_min"] = positions["latest"][plasma_src]["x_km"] / s["proton_speed"] / 60

    # recent southward-field summary for the headline
    bz24 = s["bz_gsm"][s.index >= latest_t - pd.Timedelta(hours=24)].dropna()
    south = (bz24 < 0).astype(int)
    runs = south.groupby((south != south.shift()).cumsum()).agg(["first", "size"])
    longest_south = int(runs[runs["first"] == 1]["size"].max()) if (runs["first"] == 1).any() else 0

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "history": {"start": m["time_tag"].min().isoformat(), "end": m["time_tag"].max().isoformat(),
                    "rows": int(len(m)), "sources": sorted(m["source"].unique().tolist())},
        "latest": {
            "time": latest_t.isoformat(),
            "plasma_source": plasma_src,
            "plasma_time": plasma_t.isoformat() if plasma_t is not None else None,
            "mag_source": s.loc[mag_t, "mag_source"] if mag_t is not None else None,
            "speed": _c(speed), "density": _c(recent("proton_density")[0]), "bz": _c(bz),
            "bt": _c(recent("bt")[0]), "pdyn": _c(recent("pdyn_npa")[0]), "newell": _c(recent("newell")[0]),
            "lead_time_min": _c(lead, 1), "kp": _c(float(k.iloc[-1])) if len(k) else None,
            "bz_south_pct_24h": _c(100 * south.mean(), 0) if len(south) else None,
            "longest_south_min_24h": longest_south,
            "speed_median_7d": _c(v_typ, 0),
        },
        "calibration": s.attrs.get("calibration", {}),
        "coverage_24h": coverage(m),
        "cross_spacecraft": cross,
        "agreement": agreement_block(m, tracks, v_typ),
        "anomalies": anomalies,
        "positions": positions,
        "separation": separation_block(m, tracks, plasma_src),
        "kp_live": live_kp_link(s, kp),
        "longterm": longterm_block(load_omni(hist_dir, offline)),
        "series": live_series(m, s, kp),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(result, default=str))
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--offline", help="folder with wind.json, mag.json, kp.json (+ positions.json, omni.csv)")
    p.add_argument("--out", default="docs/data")
    p.add_argument("--history", default="data/history", help="archive folder")
    a = p.parse_args()
    hist = Path(a.history)
    if hist.suffix == ".gz":  # accept the old file-style argument
        hist = hist.parent
    r = run(a.offline, Path(a.out), hist)
    L = r["latest"]
    print(f"[{L['time']}] plasma from {L['plasma_source']}, field from {L['mag_source']}: "
          f"v={L['speed']} km/s  n={L['density']} /cm3  Bz={L['bz']} nT  Pdyn={L['pdyn']} nPa  "
          f"warning={L['lead_time_min']} min  Kp={L['kp']}")
    lt = r["longterm"]
    print(f"archive rows={r['history']['rows']}  sources={r['history']['sources']}  "
          f"events (7d)={len(r['anomalies'])}  positions={'yes' if r['positions'].get('available') else 'no'}  "
          f"long-term={'through ' + lt['end'][:10] if lt.get('available') else 'no'}")


if __name__ == "__main__":
    main()
