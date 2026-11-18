"""Analysis: spacecraft agreement, anomaly detection, and the Kp link."""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

COMPARE_COLS = ["proton_speed", "proton_density", "bz_gsm", "bt"]


PRIORITY = {"SOLAR1": 0, "IMAP": 1, "ACE": 2}
PLASMA_COLS = ["proton_speed", "proton_density", "proton_temperature"]
MAG_COLS = ["bt", "bx_gsm", "by_gsm", "bz_gsm"]


def _best_rows(df: pd.DataFrame, value_col: str, active_col: str, cols: list[str]) -> pd.DataFrame:
    """For each minute, the best row that actually HAS a value in `value_col`:
    the spacecraft SWPC marked active first, then a fixed priority order."""
    d = df[df[value_col].notna()].copy()
    active = d[active_col] if active_col in d else d["active"]
    d["_rank"] = (~active.fillna(False).astype(bool)).astype(int) * 10 + d["source"].map(PRIORITY).fillna(9)
    d = d.sort_values(["time_tag", "_rank"]).drop_duplicates("time_tag", keep="first")
    return d.set_index("time_tag")[[c for c in cols if c in d] + ["source"]]


def calibrate_to_reference(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Put every spacecraft's plasma on the operational spacecraft's scale.

    Real-time instruments read differently (one may report density twice as high
    as another). When the best-available series switches spacecraft from minute to
    minute, those offsets show up as fake jumps. So each spacecraft's speed and
    density are scaled by its median ratio to the reference over the minutes
    where both report (compared as 5-minute averages)."""
    act = df["active_wind"] if "active_wind" in df else df["active"]
    counts = df[act.fillna(False).astype(bool)]["source"].value_counts()
    ref = counts.index[0] if len(counts) else ("SOLAR1" if "SOLAR1" in set(df["source"]) else df["source"].iloc[0])
    out, factors = df.copy(), {}
    for col in ["proton_speed", "proton_density"]:
        piv = df.pivot_table(index="time_tag", columns="source", values=col).resample("5min").mean()
        if ref not in piv:
            continue
        for src in piv.columns:
            if src == ref:
                continue
            both = piv[[ref, src]].dropna()
            if len(both) < 12:
                continue
            f = float((both[ref] / both[src]).median())
            if np.isfinite(f) and f > 0:
                out.loc[out["source"] == src, col] = out.loc[out["source"] == src, col] * f
                factors.setdefault(src, {})[col] = round(f, 3)
    return out, {"reference": ref, "factors": factors}


def active_series(df: pd.DataFrame) -> pd.DataFrame:
    """One row per minute, combining the best plasma reading and the best
    magnetic-field reading. They are chosen separately because the live feed
    often has the field from one spacecraft but plasma (speed, density) only
    from another, or only every few minutes."""
    cal, info = calibrate_to_reference(df)
    plasma = _best_rows(cal, "proton_speed", "active_wind", PLASMA_COLS).rename(columns={"source": "plasma_source"})
    mag = _best_rows(df, "bz_gsm", "active_mag", MAG_COLS).rename(columns={"source": "mag_source"})
    s = plasma.join(mag, how="outer").sort_index()
    # plasma can arrive less often than the field: carry it forward at most 10 minutes
    s = s.asfreq("1min")
    for c in PLASMA_COLS + ["plasma_source"]:
        if c in s:
            s[c] = s[c].ffill(limit=10)
    s = s.dropna(subset=["proton_speed", "bz_gsm"], how="all")
    s["source"] = s["mag_source"].fillna(s["plasma_source"])
    s.attrs["calibration"] = info
    return s


def best_lag_minutes(a: pd.Series, b: pd.Series, max_lag: int = 30):
    """Lag (minutes) that maximizes correlation of b shifted against a.
    Spacecraft sit at different spots in their L1 orbits, so the same
    solar wind parcel can reach them minutes apart."""
    best = (0, np.nan)
    for lag in range(-max_lag, max_lag + 1):
        r = a.corr(b.shift(lag))
        if np.isfinite(r) and (not np.isfinite(best[1]) or r > best[1]):
            best = (lag, r)
    return best


def cross_spacecraft(df: pd.DataFrame, min_overlap: int = 12) -> list[dict]:
    """For every pair of spacecraft: how well do they agree on the same wind?
    Values are compared as 5-minute averages, because the spacecraft report on
    slightly different schedules (exact 1-minute matches are rare for some pairs)."""
    sources = sorted(df["source"].dropna().unique())
    results = []
    for s1, s2 in combinations(sources, 2):
        a = df[df.source == s1].set_index("time_tag")
        b = df[df.source == s2].set_index("time_tag")
        row = {"pair": f"{s1} vs {s2}"}
        for col in COMPARE_COLS:
            if col not in a or col not in b:
                continue
            j = pd.concat([a[col].resample("5min").mean(), b[col].resample("5min").mean()],
                          axis=1, keys=["a", "b"]).dropna()
            if len(j) < min_overlap:
                row[col] = {"n": int(len(j))}
                continue
            diff = j["b"] - j["a"]
            row[col] = {
                "n": int(len(j)),
                "r": round(float(j["a"].corr(j["b"])), 3),
                "median_bias": round(float(diff.median()), 3),
                "median_abs_diff": round(float(diff.abs().median()), 3),
            }
        # timing offset using Bz, which has the most minute-scale structure
        ab = pd.concat([a["bz_gsm"], b["bz_gsm"]], axis=1, keys=["a", "b"]).asfreq("1min") if "bz_gsm" in a else None
        if ab is not None and ab.dropna().shape[0] >= 60:
            lag, r = best_lag_minutes(ab["a"], ab["b"])
            row["bz_best_lag_min"] = int(lag)
            row["bz_r_at_lag"] = round(float(r), 3)
        results.append(row)
    return results


def coverage(df: pd.DataFrame, hours: int = 24) -> list[dict]:
    """Share of minutes in the last `hours` with valid speed and Bz, per spacecraft."""
    end = df["time_tag"].max()
    recent = df[df["time_tag"] > end - pd.Timedelta(hours=hours)]
    total = hours * 60
    out = []
    for src, g in recent.groupby("source"):
        out.append({
            "source": src,
            "speed_pct": round(100 * g["proton_speed"].notna().sum() / total, 1),
            "bz_pct": round(100 * g["bz_gsm"].notna().sum() / total, 1),
            "active_pct": round(100 * g["active"].sum() / total, 1),
        })
    return out


FEATURE_WORDS = {
    "proton_speed": "wind speed", "proton_density": "density", "bt": "field strength",
    "bz_gsm": "Bz", "pdyn_npa": "ram pressure", "d10_proton_speed": "10-minute speed change",
    "d10_proton_density": "10-minute density change", "d10_bt": "10-minute field change",
}


def _why(feat: str, value: float, z: float) -> str:
    """Turn one unusual feature into a short phrase a person can read."""
    up = value > 0
    if feat == "d10_proton_speed":
        return f"speed {'jumped' if up else 'dropped'} {abs(value):.0f} km/s in 10 min"
    if feat == "d10_proton_density":
        return f"density {'jumped' if up else 'dropped'} {abs(value):.1f} /cm³ in 10 min"
    if feat == "d10_bt":
        return f"field strength {'rose' if up else 'fell'} {abs(value):.1f} nT in 10 min"
    if feat == "bz_gsm":
        return f"Bz {value:.1f} nT ({'strongly south' if value < 0 else 'strongly north'})"
    unit = {"proton_speed": "km/s", "proton_density": "/cm³", "bt": "nT", "pdyn_npa": "nPa"}[feat]
    fmt = "{:.0f}" if feat == "proton_speed" else "{:.1f}"
    return f"{FEATURE_WORDS[feat]} {fmt.format(value)} {unit} ({'far above' if z > 0 else 'far below'} this week's normal)"


def detect_anomalies(s: pd.DataFrame, contamination: float = 0.01) -> tuple[pd.Series, list[dict]]:
    """Isolation Forest on physical features plus their 10-minute changes.

    It learns what a normal minute looks like this week and scores how easy each
    minute is to isolate from the rest; the easiest 1% are flagged. For each
    flagged event we also report WHICH features were most unusual (robust
    z-scores: distance from the median in units of the typical spread), so the
    flag can be explained instead of just trusted."""
    feats = pd.DataFrame(index=s.index)
    for c in ["proton_speed", "proton_density", "bt", "bz_gsm", "pdyn_npa"]:
        feats[c] = s[c]
    for c in ["proton_speed", "proton_density", "bt"]:
        feats[f"d10_{c}"] = s[c].diff(10)
    feats = feats.dropna()
    if len(feats) < 200:
        return pd.Series(dtype=float), []
    scaler = RobustScaler().fit(feats)
    X = scaler.transform(feats)
    Z = pd.DataFrame(X, index=feats.index, columns=feats.columns)
    model = IsolationForest(n_estimators=300, contamination=contamination, random_state=0)
    flags = model.fit_predict(X) == -1
    score = pd.Series(-model.score_samples(X), index=feats.index)

    flagged = feats.index[flags]
    events, start, prev = [], None, None
    for t in flagged:
        if start is None:
            start = prev = t
        elif t - prev <= pd.Timedelta(minutes=30):
            prev = t
        else:
            events.append((start, prev))
            start = prev = t
    if start is not None:
        events.append((start, prev))

    med_p = s["pdyn_npa"].median()
    out = []
    for a, b in events:
        w = s.loc[a:b]
        peak = score.loc[a:b].idxmax()
        z = Z.loc[peak]
        top = z.abs().sort_values(ascending=False).index[:3]
        why = [_why(f, float(feats.loc[peak, f]), float(z[f])) for f in top]
        d_v = float(feats.loc[a:b, "d10_proton_speed"].max())
        d_n = float(feats.loc[a:b, "d10_proton_density"].max())
        if d_v > 40 and d_n > 0:
            kind = "Shock arrival"
        elif w["bz_gsm"].min() < -10:
            kind = "Strong southward field"
        elif w["pdyn_npa"].max() > 2 * med_p:
            kind = "Pressure pulse"
        elif abs(float(z.get("d10_proton_density", 0))) > 4:
            kind = "Density jump"
        else:
            kind = "Unusual combination"
        # Isolation Forest always flags its quota (1%), even in a calm week. Only events that
        # also cross a physical threshold are called significant; the rest are minor fluctuations.
        med_bt = s["bt"].median()
        reasons = []
        if d_v >= 50:
            reasons.append("speed jump of 50+ km/s")
        if w["bz_gsm"].min() <= -10:
            reasons.append("Bz at or below −10 nT")
        if w["pdyn_npa"].max() >= max(3.0, 2.5 * med_p):
            reasons.append("pressure 2.5× normal")
        if w["bt"].max() >= max(15.0, 2.5 * med_bt):
            reasons.append("field strength 2.5× normal")
        out.append({
            "significant": bool(reasons), "significance": reasons,
            "start": a.isoformat(), "end": b.isoformat(), "peak": peak.isoformat(),
            "minutes": int((b - a).total_seconds() // 60) + 1, "type": kind, "why": why,
            "max_speed": round(float(w["proton_speed"].max()), 1),
            "min_bz": round(float(w["bz_gsm"].min()), 2),
            "max_pdyn": round(float(w["pdyn_npa"].max()), 2),
            "geoeffective": bool(w["bz_gsm"].min() < -5),
            "score": round(float(score.loc[a:b].max()), 3),
        })
    return score, out


def kp_link(s: pd.DataFrame, kp: pd.DataFrame) -> dict:
    """Does coupling at L1 track the planetary Kp index? Kp is 3-hourly and
    global, so we compare 3-hour mean coupling to each Kp value. Spearman,
    because the relation is monotonic but not linear."""
    c = s["newell"].resample("3h", origin="epoch").mean().rename("newell")
    k = kp.set_index("time_tag")["kp"]
    j = pd.concat([c, k], axis=1).dropna()
    res = {"n": int(len(j)), "pairs": [
        {"t": t.isoformat(), "newell": round(float(r.newell), 2), "kp": float(r.kp)} for t, r in j.iterrows()
    ]}
    if len(j) >= 8:
        res["spearman"] = round(float(j["newell"].corr(j["kp"], method="spearman")), 3)
        lagged = pd.concat([c.shift(1), k], axis=1).dropna()
        if len(lagged) >= 8:
            res["spearman_prev_window"] = round(float(lagged.iloc[:, 0].corr(lagged["kp"], method="spearman")), 3)
    return res
