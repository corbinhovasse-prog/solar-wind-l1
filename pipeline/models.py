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
    # plasma can arrive less often than the field: carry it forward at most 10 minutes.
    # The grid is whole UTC minutes from first to last, so it never depends on the
    # seconds of whichever record happens to be oldest.
    s.index = s.index.floor("min")
    s = s[~s.index.duplicated(keep="last")]
    s = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="1min", tz="UTC", name="time_tag"))
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
        ab = pd.concat([a["bz_gsm"], b["bz_gsm"]], axis=1, sort=True, keys=["a", "b"]).asfreq("1min") if "bz_gsm" in a else None
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


# Physical thresholds. Checked on EVERY minute, independently of the anomaly model,
# so a real event is never missed just because the model's 1% quota went elsewhere.
RULES = {
    "south": {"bz_max": -10.0, "min_minutes": 15},             # Bz at or below -10 nT for 15+ min
    "speed_jump": {"kms": 50.0},                               # 10-min median after vs before
    "pressure": {"times_normal": 2.5, "floor_npa": 3.0, "min_minutes": 10},
    "field": {"times_normal": 2.5, "floor_nt": 15.0, "min_minutes": 10},
    # Storms are often driven not by one jump but by hours of moderately southward field.
    # 3-hour mean Newell coupling >= 8: calibrated on NASA OMNI 2000-2026, where Kp reached
    # storm level (5+) after 29% of such windows (11x the base rate) and 79% of storms were
    # preceded by one. The dashboard recomputes these numbers from the data on every run.
    "sustained": {"coupling_3h": 8.0},
}


def _runs(mask: pd.Series, min_len: int) -> pd.Series:
    """Keep only stretches of True that last at least min_len consecutive minutes."""
    grp = (mask != mask.shift()).cumsum()
    size = mask.groupby(grp).transform("size")
    return mask & (size >= min_len)


def _group(times: pd.DatetimeIndex, gap_min: int = 30) -> list[tuple]:
    events, start, prev = [], None, None
    for t in times:
        if start is None:
            start = prev = t
        elif t - prev <= pd.Timedelta(minutes=gap_min):
            prev = t
        else:
            events.append((start, prev)); start = prev = t
    if start is not None:
        events.append((start, prev))
    return events


def physical_flags(s: pd.DataFrame) -> pd.DataFrame:
    """One column per rule, True on the minutes where that rule is met."""
    s = s.asfreq("1min")
    bz5 = s["bz_gsm"].rolling(5, min_periods=3).median()
    p5 = s["pdyn_npa"].rolling(5, min_periods=3).median()
    b5 = s["bt"].rolling(5, min_periods=3).median()
    v = s["proton_speed"]
    before = v.rolling(10, min_periods=5).median().shift(1)        # minutes t-10 .. t-1
    after = v.rolling(10, min_periods=5).median().shift(-9)        # minutes t .. t+9
    R = RULES
    p_lim = max(R["pressure"]["floor_npa"], R["pressure"]["times_normal"] * s["pdyn_npa"].median())
    b_lim = max(R["field"]["floor_nt"], R["field"]["times_normal"] * s["bt"].median())
    out = pd.DataFrame(index=s.index)
    out["south"] = _runs((bz5 <= R["south"]["bz_max"]).fillna(False), R["south"]["min_minutes"])
    out["speed_jump"] = ((after - before) >= R["speed_jump"]["kms"]).fillna(False)
    out["pressure"] = _runs((p5 >= p_lim).fillna(False), R["pressure"]["min_minutes"])
    out["field"] = _runs((b5 >= b_lim).fillna(False), R["field"]["min_minutes"])
    c3 = s["newell"].rolling(180, min_periods=120).mean()
    out["sustained"] = (c3 >= R["sustained"]["coupling_3h"]).fillna(False)
    out.attrs.update({"pressure_limit": p_lim, "field_limit": b_lim, "jump": (after - before), "c3": c3})
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


def model_flags(s: pd.DataFrame, contamination: float = 0.01) -> tuple[pd.Series, pd.DataFrame]:
    """Isolation Forest: marks the most unusual 1% of minutes in the week. It always fills
    that quota, calm week or not, so on its own it only ever means 'unusual for this week'."""
    feats = pd.DataFrame(index=s.index)
    for c in ["proton_speed", "proton_density", "bt", "bz_gsm", "pdyn_npa"]:
        feats[c] = s[c]
    for c in ["proton_speed", "proton_density", "bt"]:
        feats[f"d10_{c}"] = s[c].diff(10)
    feats = feats.dropna()
    if len(feats) < 200:
        return pd.Series(False, index=s.index), feats
    X = RobustScaler().fit_transform(feats)
    model = IsolationForest(n_estimators=300, contamination=contamination, random_state=0)
    flag = pd.Series(model.fit_predict(X) == -1, index=feats.index)
    z = pd.DataFrame(X, index=feats.index, columns=feats.columns)
    z["_score"] = -model.score_samples(X)
    return flag.reindex(s.index, fill_value=False), z


def detect_anomalies(s: pd.DataFrame, contamination: float = 0.01) -> tuple[pd.Series, list[dict]]:
    """Significant events come from the physical rules (every minute checked).
    Model-only flags are reported separately as minor, statistically unusual intervals."""
    s = s.asfreq("1min")
    if s["bz_gsm"].notna().sum() < 60:
        return pd.Series(dtype=float), []
    phys = physical_flags(s)
    mflag, z = model_flags(s, contamination)
    jump = phys.attrs["jump"]
    out = []

    any_phys = phys.any(axis=1)
    for a, b in _group(any_phys[any_phys].index):
        w, f = s.loc[a:b], phys.loc[a:b]
        rules, why = [], []
        if f["south"].any():
            mins = int(f["south"].sum())
            rules.append("Bz at or below −10 nT for 15+ min")
            why.append(f"Bz stayed at or below −10 nT for {mins} min (lowest {w['bz_gsm'].min():.1f} nT)")
        if f["speed_jump"].any():
            j = float(jump.loc[a:b].max())
            rules.append("speed jump of 50+ km/s within 10 min")
            why.append(f"speed rose {j:.0f} km/s within 10 minutes")
        if f["pressure"].any():
            rules.append(f"pressure above {phys.attrs['pressure_limit']:.1f} nPa (2.5× normal, at least 3)")
            why.append(f"ram pressure reached {w['pdyn_npa'].max():.1f} nPa")
        if f["field"].any():
            rules.append(f"field strength above {phys.attrs['field_limit']:.1f} nT (2.5× normal, at least 15)")
            why.append(f"field strength reached {w['bt'].max():.1f} nT")
        if f["sustained"].any():
            hrs = f["sustained"].sum() / 60
            rules.append("3-hour average coupling of 8 or more")
            why.append(f"coupling averaged {phys.attrs['c3'].loc[a:b].max():.1f} over 3 hours (Bz mostly south, "
                       f"median {w['bz_gsm'].median():.1f} nT); sustained for {hrs:.1f} h")
        # a shock compresses everything at once: speed, density AND field jump together
        pre, post = s.loc[a - pd.Timedelta(minutes=20):a - pd.Timedelta(minutes=1)], s.loc[a:a + pd.Timedelta(minutes=20)]
        ratio = lambda c: post[c].median() / pre[c].median() if pre[c].median() and np.isfinite(pre[c].median()) else np.nan
        shock = f["speed_jump"].any() and ratio("proton_density") >= 1.3 and ratio("bt") >= 1.3
        kind = ("Shock" if shock else "Strong southward field" if f["south"].any() else
                "Sustained southward driving" if f["sustained"].any() else
                "Speed jump" if f["speed_jump"].any() else "Pressure pulse" if f["pressure"].any() else "Strong field")
        out.append(_event(a, b, w, kind, why, rules, True, bool(mflag.loc[a:b].any())))

    # model-only intervals: unusual, but below every physical threshold
    lone = mflag & ~any_phys.reindex(mflag.index, fill_value=False)
    for a, b in _group(lone[lone].index):
        w = s.loc[a:b]
        zz = z.loc[a:b]
        peak = zz["_score"].idxmax()
        top = zz.loc[peak].drop("_score").abs().sort_values(ascending=False).index[:3]
        src = pd.DataFrame({c: s[c] for c in ["proton_speed", "proton_density", "bt", "bz_gsm", "pdyn_npa"]})
        for c in ["proton_speed", "proton_density", "bt"]:
            src[f"d10_{c}"] = s[c].diff(10)
        why = [_why(c, float(src.loc[peak, c]), float(zz.loc[peak, c])) for c in top]
        out.append(_event(a, b, w, "Unusual for this week", why, [], False, True))
    out.sort(key=lambda e: e["start"])
    score = z["_score"] if "_score" in z else pd.Series(dtype=float)
    return score, out


def _event(a, b, w, kind, why, rules, significant, model_flagged) -> dict:
    return {
        "start": a.isoformat(), "end": b.isoformat(),
        "minutes": int((b - a).total_seconds() // 60) + 1, "type": kind, "why": why,
        "significant": significant, "significance": rules, "model_flagged": model_flagged,
        "max_speed": round(float(w["proton_speed"].max()), 1) if w["proton_speed"].notna().any() else None,
        "min_bz": round(float(w["bz_gsm"].min()), 2) if w["bz_gsm"].notna().any() else None,
        "max_pdyn": round(float(w["pdyn_npa"].max()), 2) if w["pdyn_npa"].notna().any() else None,
        "geoeffective": bool(w["bz_gsm"].min() < -5) if w["bz_gsm"].notna().any() else False,
    }


def kp_link(s: pd.DataFrame, kp: pd.DataFrame) -> dict:
    """Does coupling at L1 track the planetary Kp index? Kp is 3-hourly and
    global, so we compare 3-hour mean coupling to each Kp value. Spearman,
    because the relation is monotonic but not linear."""
    c = s["newell"].resample("3h", origin="epoch").mean().rename("newell")
    k = kp.set_index("time_tag")["kp"]
    j = pd.concat([c, k], axis=1, sort=True).dropna()
    res = {"n": int(len(j)), "pairs": [
        {"t": t.isoformat(), "newell": round(float(r.newell), 2), "kp": float(r.kp)} for t, r in j.iterrows()
    ]}
    if len(j) >= 8:
        res["spearman"] = round(float(j["newell"].corr(j["kp"], method="spearman")), 3)
        lagged = pd.concat([c.shift(1), k], axis=1, sort=True).dropna()
        if len(lagged) >= 8:
            res["spearman_prev_window"] = round(float(lagged.iloc[:, 0].corr(lagged["kp"], method="spearman")), 3)
    return res
