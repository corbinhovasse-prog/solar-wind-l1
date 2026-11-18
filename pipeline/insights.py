"""Higher-level analyses behind the dashboard's explanations.

    longterm_block   years of OMNI data: trends, seasons, solar cycle, what drives Kp
    live_kp_link     our own L1 data vs Kp, with each reading shifted to its arrival time at Earth
    agreement_block  do the three spacecraft tell the same story? scores per spacecraft
    separation_block how much the wind changes across space, and what that means for Earth
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .features import dynamic_pressure_npa, newell_coupling


def _r(v, d=3):
    return None if v is None or not np.isfinite(v) else round(float(v), d)


def _spearman(a: pd.Series, b: pd.Series) -> float:
    j = pd.concat([a, b], axis=1).dropna()
    return j.iloc[:, 0].corr(j.iloc[:, 1], method="spearman") if len(j) >= 8 else np.nan


def _spearman_test(a: pd.Series, b: pd.Series) -> dict:
    """Spearman's rho plus its p-value: the chance of seeing a rho at least this far
    from zero if there were really no relationship, given this many samples."""
    j = pd.concat([a, b], axis=1).dropna()
    if len(j) < 8:
        return {"rho": None, "p": None, "n": int(len(j))}
    rho, p = stats.spearmanr(j.iloc[:, 0], j.iloc[:, 1])
    return {"rho": _r(rho), "p": _r(p, 4), "n": int(len(j)), "n_needed": _n_needed(rho)}


def _n_needed(rho: float) -> int | None:
    """Samples needed to tell a true correlation of this size from chance
    (5% false-alarm rate, 80% power; Fisher z approximation)."""
    if rho is None or not np.isfinite(rho) or abs(rho) < 0.02:
        return None
    return int(np.ceil(((1.96 + 0.84) / np.arctanh(min(abs(rho), 0.99))) ** 2 + 3))


def _r2(y: np.ndarray, X: np.ndarray) -> float:
    """Share of the variation in y that a straight-line fit on X explains (0 to 1)."""
    A = np.column_stack([np.ones(len(y)), X])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    return 1 - resid.var() / y.var()


# ---------------------------------------------------------------- long term
def _agg(h: pd.DataFrame, rule: str) -> dict:
    """Mean plus the middle-50% band (25th to 75th percentile) per period."""
    cols = ["v", "n", "bz", "pdyn", "newell", "kp"]
    r = h[cols].resample(rule)
    mean, p25, p75 = r.mean(), r.quantile(.25), r.quantile(.75)
    keep = mean.dropna(how="all").index
    mean, p25, p75 = mean.loc[keep], p25.loc[keep], p75.loc[keep]
    return {"t": [t.isoformat() for t in keep],
            **{c: {"mean": [_r(v, 2) for v in mean[c]], "p25": [_r(v, 2) for v in p25[c]],
                   "p75": [_r(v, 2) for v in p75[c]]} for c in cols}}


def longterm_block(omni: pd.DataFrame) -> dict:
    if omni is None or len(omni) < 24 * 60:
        return {"available": False}
    h = omni.set_index("time_tag").sort_index()
    h["pdyn"] = dynamic_pressure_npa(h["n"], h["v"])
    h["newell"] = newell_coupling(h["v"], h["by"], h["bz"])
    last = h.dropna(subset=["v"]).index.max()
    out = {"available": True, "start": h.index.min().isoformat(), "end": last.isoformat()}

    out["daily"] = _agg(h[h.index > last - pd.Timedelta(days=5 * 366)], "1D")   # 1 to 5 year views
    out["monthly"] = _agg(h, "MS")
    sun = h["sunspot"].resample("MS").mean()
    out["monthly"]["sunspot"] = [_r(v, 0) for v in sun.reindex(pd.to_datetime(out["monthly"]["t"]))]

    # 3-hour windows: Kp is defined per 3 hours
    w = h[["newell", "kp", "v", "bz"]].resample("3h", origin="epoch").mean().dropna()
    w["storm"] = w["kp"] >= 5
    yr, mo = w.index.year, w.index.month
    heat = w.groupby([yr, mo])["kp"].mean().unstack()
    out["heatmap"] = {"years": [int(y) for y in heat.index], "kp": [[_r(v, 2) for v in row] for row in heat.to_numpy()]}
    out["by_month"] = {
        "kp_mean": [_r(v, 2) for v in w.groupby(mo)["kp"].mean().reindex(range(1, 13))],
        "storm_pct": [_r(100 * v, 1) for v in w.groupby(mo)["storm"].mean().reindex(range(1, 13))],
    }
    yearly = w.groupby(yr).agg(kp=("kp", "mean"), storms=("storm", "sum"), v=("v", "mean"))
    yearly["sunspot"] = h["sunspot"].groupby(h.index.year).mean()
    out["by_year"] = {"years": [int(y) for y in yearly.index],
                      **{c: [_r(v, 2) for v in yearly[c]] for c in ["kp", "storms", "v", "sunspot"]}}

    # --- what drives Kp? lag curve, then how much each factor explains
    lags = list(range(0, 13))
    roll = h["newell"].rolling(3, min_periods=2).mean()
    kp_h = h["kp"]
    pick = h.index.hour % 3 == 2  # one sample per 3-hour Kp window
    curve = [_spearman(kp_h[pick], roll.shift(L)[pick]) for L in lags]
    best = int(np.nanargmax(curve))
    d = pd.DataFrame({"kp": kp_h, "c": np.log1p(roll.shift(best)), "prev": kp_h.shift(3)})[pick].dropna()
    doy = d.index.dayofyear.to_numpy()
    equinox = np.cos(4 * np.pi * (doy - 80) / 365.25)  # +1 near equinoxes, -1 near solstices
    y = d["kp"].to_numpy()
    r2_c = _r2(y, d[["c"]].to_numpy())
    r2_cp = _r2(y, d[["c", "prev"]].to_numpy())
    r2_all = _r2(y, np.column_stack([d[["c", "prev"]].to_numpy(), equinox]))
    # residual of the coupling-only model, averaged by month: what the solar wind alone misses
    A = np.column_stack([np.ones(len(y)), d["c"].to_numpy()])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = pd.Series(y - A @ coef, index=d.index)
    out["kp_drivers"] = {
        "n_windows": int(len(d)), "lag_hours": lags, "lag_rho": [_r(v) for v in curve], "best_lag_h": best,
        "rho_best": _r(curve[best]),
        "p_best": _r(stats.spearmanr(d["kp"], d["c"]).pvalue, 6),
        "r2_coupling": _r(r2_c), "r2_plus_memory": _r(r2_cp), "r2_plus_season": _r(r2_all),
        "resid_by_month": [_r(v, 2) for v in resid.groupby(resid.index.month).mean().reindex(range(1, 13))],
        "rho_prev_kp": _r(_spearman(d["kp"], d["prev"])),
    }
    return out


# ---------------------------------------------------------------- live Kp link
def live_kp_link(s: pd.DataFrame, kp: pd.DataFrame) -> dict:
    """Shift every L1 reading to when that wind reaches Earth, then compare with Kp."""
    d = s[["newell", "lead_time_min"]].dropna()
    if d.empty or kp.empty:
        return {"n": 0}
    arrival = d.index + pd.to_timedelta(d["lead_time_min"], unit="min")
    c = pd.Series(d["newell"].to_numpy(), index=arrival).sort_index()
    c3 = c.resample("3h", origin="epoch").mean()
    k = kp.set_index("time_tag")["kp"]
    j = pd.concat([c3.rename("newell"), k.rename("kp")], axis=1).dropna()
    raw3 = s["newell"].resample("3h", origin="epoch").mean()
    jr = pd.concat([raw3.rename("newell"), k.rename("kp")], axis=1).dropna()
    t = _spearman_test(j["newell"], j["kp"])
    return {
        "n": int(len(j)),
        "rho_shifted": t["rho"], "p_shifted": t["p"], "n_needed": t.get("n_needed"),
        "rho_unshifted": _r(_spearman(jr["newell"], jr["kp"])),
        "rho_next_window": _r(_spearman(j["newell"], j["kp"].shift(-1))),
        "kp_range": [_r(j["kp"].min(), 2), _r(j["kp"].max(), 2)] if len(j) else None,
        "mean_lead_min": _r(d["lead_time_min"].mean(), 1),
        "pairs": [{"t": t.isoformat(), "newell": _r(r.newell, 2), "kp": float(r.kp)} for t, r in j.iterrows()],
    }


# ---------------------------------------------------------------- agreement
TOL = {"proton_speed": ("rel", 0.04), "proton_density": ("rel", 0.20), "bz_gsm": ("abs", 2.0)}
VAR_NAME = {"proton_speed": "speed", "proton_density": "density", "bz_gsm": "bz"}


def agreement_block(df: pd.DataFrame, tracks: pd.DataFrame | None = None, v_typ: float = 400.0, days: int = 7) -> dict:
    """Two separate questions, because they have different answers and different fixes:

    1. Calibration: does a spacecraft read systematically high or low compared with the
       others (e.g. density 30% low)? That's a steady offset, measured as the median
       difference, and it can be corrected.
    2. Tracking: once that offset is removed, does it rise and fall WITH the others?
       That's what tells you whether to trust its changes, such as a shock arriving.

    Readings are first time-aligned using each spacecraft's position (a spacecraft
    further upstream sees the same wind earlier), then compared in 5-minute bins
    against the median of the OTHER spacecraft."""
    end = df["time_tag"].max()
    d = df[df["time_tag"] > end - pd.Timedelta(days=days)].copy()
    srcs = sorted(d["source"].unique())

    shifts = {s: 0.0 for s in srcs}
    if tracks is not None and not tracks.empty:
        lastx = tracks.sort_values("time_tag").groupby("source")["x_gse"].last()
        ref = float(lastx.reindex(srcs).median())
        for s in srcs:
            if s in lastx:
                shifts[s] = round(float(lastx[s] - ref) / v_typ / 60, 1)  # minutes this spacecraft sees the wind early
    d["time_tag"] = d["time_tag"] + pd.to_timedelta(d["source"].map(shifts), unit="min")

    grid = {c: d.pivot_table(index="time_tag", columns="source", values=c).resample("5min").mean().reindex(columns=srcs)
            for c in TOL}
    idx = grid["bz_gsm"].index.union(grid["proton_speed"].index)
    grid = {c: g.reindex(idx) for c, g in grid.items()}

    # step 1, calibration: each spacecraft's steady offset from the median of ALL reporting spacecraft
    # (the full median is robust: one badly calibrated instrument can't drag the reference)
    corrected, bias_of = {}, {}
    for c, (kind, tol) in TOL.items():
        g = grid[c]
        med = g.median(axis=1).where(g.notna().sum(axis=1) >= 2)
        cg = g.copy()
        for s in srcs:
            both = g[s].notna() & med.notna()
            if both.sum() < 12:
                bias_of[(c, s)] = None
                continue
            if kind == "rel":
                b = float(((g[s] - med) / med)[both].median())
                cg[s] = g[s] / (1 + b)
            else:
                b = float((g[s] - med)[both].median())
                cg[s] = g[s] - b
            bias_of[(c, s)] = b
        corrected[c] = cg

    # step 2, tracking: with offsets removed, is each one within tolerance of the OTHERS?
    ok, sc_out = {}, {}
    for s in srcs:
        sc_out[s] = {"vars": {}, "time_shift_min": shifts[s]}
        for c, (kind, tol) in TOL.items():
            g = corrected[c]
            others = g.drop(columns=s).median(axis=1)
            both = g[s].notna() & others.notna()
            entry = {"n": int(both.sum())}
            b = bias_of.get((c, s))
            if both.sum() >= 12 and b is not None:
                dev = ((g[s] - others).abs() / others.abs()) if kind == "rel" else (g[s] - others).abs()
                dev_ok = dev.le(tol)
                entry["bias"] = _r(100 * b, 1) if kind == "rel" else _r(b, 2)
                entry["track_pct"] = _r(100 * dev_ok[both].mean(), 0)
                entry["r"] = _r(grid[c][s][both].corr(others[both]), 3)
                ok[(c, s)] = dev_ok.astype(float).where(both)
            sc_out[s]["vars"][VAR_NAME[c]] = entry

        rates = [v["track_pct"] for v in sc_out[s]["vars"].values() if v.get("track_pct") is not None]
        compared = max([v["n"] for v in sc_out[s]["vars"].values()] + [0])
        track = float(np.mean(rates)) if rates else np.nan
        sc_out[s]["track_pct"] = _r(track, 0)
        sc_out[s]["compared_bins"] = compared
        sc_out[s]["coverage_pct"] = _r(100 * grid["bz_gsm"][s].notna().mean(), 0) if len(idx) else 0
        V = sc_out[s]["vars"]
        flags = []
        if (V["speed"].get("bias") or 0) and abs(V["speed"]["bias"]) > 4:
            flags.append(f"reads speed {V['speed']['bias']:+.0f}%")
        if (V["density"].get("bias") or 0) and abs(V["density"]["bias"]) > 20:
            flags.append(f"reads density {V['density']['bias']:+.0f}%")
        if (V["bz"].get("bias") or 0) and abs(V["bz"]["bias"]) > 1:
            flags.append(f"reads Bz {V['bz']['bias']:+.1f} nT")
        sc_out[s]["calibration_flags"] = flags
        sc_out[s]["verdict"] = ("not enough data" if compared < 24 or not rates
                                else "in step" if track >= 75 else "partly in step" if track >= 50 else "out of step")
        bz = grid["bz_gsm"]
        oth = bz.drop(columns=s)
        settled = oth.notna().all(axis=1) & (oth.abs() > 2).all(axis=1) & (np.sign(oth).nunique(axis=1) == 1) if oth.shape[1] >= 2 else pd.Series(False, index=bz.index)
        contrarian = settled & (bz[s].abs() > 2) & (np.sign(bz[s]) != np.sign(oth.iloc[:, 0]))
        sc_out[s]["bz_contrarian_pct"] = _r(100 * contrarian.sum() / settled.sum(), 1) if settled.sum() >= 12 else None

    # per bin, per spacecraft: share of its compared variables that are in step
    per_bin = pd.DataFrame({s: pd.concat([ok[k] for k in ok if k[1] == s], axis=1).mean(axis=1)
                            if any(k[1] == s for k in ok) else pd.Series(np.nan, index=idx) for s in srcs})
    in_step = per_bin.ge(2 / 3).astype(float).where(per_bin.notna())
    three = in_step.dropna()
    out = {"window_days": days, "aligned_by_position": any(shifts.values()), "spacecraft": sc_out,
           "tolerance": {"speed_pct": 4, "density_pct": 20, "bz_nt": 2.0},
           "three_way_bins": int(len(three)),
           "three_way_sync_pct": _r(100 * three.min(axis=1).mean(), 0) if len(three) >= 12 else None}
    two = in_step.notna().sum(axis=1) >= 2
    out["any_pair_sync_pct"] = _r(100 * in_step[two].min(axis=1, skipna=True).mean(), 0) if two.sum() >= 12 else None

    hourly = in_step.resample("1h").mean()
    out["strip"] = {"t": [t.isoformat() for t in hourly.index], "status": {}}
    for s in srcs:
        cov_h = grid["bz_gsm"][s].resample("1h").count()
        out["strip"]["status"][s] = [
            "nodata" if cov_h.get(t, 0) == 0 else ("solo" if not np.isfinite(v) else ("agree" if v >= 0.75 else "differ"))
            for t, v in hourly[s].items()]
    return out


# ---------------------------------------------------------------- separation
def separation_block(df: pd.DataFrame, tracks: pd.DataFrame, plasma_src: str | None) -> dict:
    """Each day, for each pair: how far apart were they, and how different was their wind?
    The trend (difference per 100,000 km) estimates how different the wind at Earth is
    from what each spacecraft measures, since Earth also sits off each spacecraft's line."""
    if tracks is None or tracks.empty:
        return {"points": []}
    d = df.copy()
    d["day"] = d["time_tag"].dt.floor("1D")
    pos = tracks.copy()
    pos["day"] = pos["time_tag"].dt.floor("1D")
    pos = pos.groupby(["day", "source"])[["x_gse", "y_gse", "z_gse"]].mean()
    spd = d.pivot_table(index="time_tag", columns="source", values="proton_speed").resample("5min").mean()
    bz = d.pivot_table(index="time_tag", columns="source", values="bz_gsm").resample("5min").mean()
    pts = []
    srcs = list(spd.columns)
    for i, a in enumerate(srcs):
        for b in srcs[i + 1:]:
            for day, g in spd[[a, b]].dropna().groupby(spd[[a, b]].dropna().index.floor("1D")):
                if len(g) < 24 or (day, a) not in pos.index or (day, b) not in pos.index:
                    continue
                sep = float(np.linalg.norm(pos.loc[(day, a)].to_numpy() - pos.loc[(day, b)].to_numpy()))
                pct = float(((g[a] - g[b]).abs() / g[[a, b]].mean(axis=1)).median() * 100)
                zb = bz[[a, b]].dropna()
                zb = zb[zb.index.floor("1D") == day]
                pts.append({"day": day.isoformat(), "pair": f"{a} vs {b}", "sep_km": round(sep),
                            "speed_diff_pct": _r(pct, 2),
                            "bz_r": _r(zb[a].corr(zb[b]), 3) if len(zb) >= 24 else None})
    out = {"points": pts}
    seps = [p["sep_km"] for p in pts]
    out["needed"] = 10
    # a trend needs enough pair-days AND a real spread of separations to be meaningful
    if len(pts) >= 10 and max(seps) - min(seps) >= 1e5:
        x = np.array([p["sep_km"] for p in pts]) / 1e5
        y = np.array([p["speed_diff_pct"] for p in pts])
        slope, icpt = np.polyfit(x, y, 1)
        out["fit"] = {"pct_per_100k_km": _r(slope, 2), "intercept_pct": _r(icpt, 2), "n": len(pts)}
        if plasma_src and not tracks.empty:
            last = tracks[tracks.source == plasma_src].sort_values("time_tag").iloc[-1]
            off = float(np.hypot(last.y_gse, last.z_gse))
            out["earth_estimate"] = {"source": plasma_src, "off_axis_km": round(off),
                                     "expected_speed_diff_pct": _r(max(icpt + slope * off / 1e5, 0), 1)}
    return out
