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
    j = pd.concat([a, b], axis=1, sort=True).dropna()
    return j.iloc[:, 0].corr(j.iloc[:, 1], method="spearman") if len(j) >= 8 else np.nan


def _spearman_test(a: pd.Series, b: pd.Series) -> dict:
    """Spearman's rho plus its p-value: the chance of seeing a rho at least this far
    from zero if there were really no relationship, given this many samples."""
    j = pd.concat([a, b], axis=1, sort=True).dropna()
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

    # weekly bands for the 3-5 year views: true 25th-75th percentiles of the HOURS in each week
    out["weekly"] = _agg(h[h.index > last - pd.Timedelta(days=5 * 366)], "W")

    # 3-hour Kp windows, counted from Kp alone: a storm still counts when solar wind data are missing
    kp3 = h["kp"].resample("3h", origin="epoch").mean().dropna()
    storm = kp3 >= 5
    yr, mo = kp3.index.year, kp3.index.month
    out["by_month"] = {
        "kp_mean": [_r(v, 2) for v in kp3.groupby(mo).mean().reindex(range(1, 13))],
        "storm_pct": [_r(100 * v, 1) for v in storm.groupby(mo).mean().reindex(range(1, 13))],
    }
    yearly = pd.DataFrame({"kp": kp3.groupby(yr).mean(), "storms": storm.groupby(yr).sum()})
    yearly["v"] = h["v"].groupby(h.index.year).mean()
    yearly["sunspot"] = h["sunspot"].groupby(h.index.year).mean()
    out["by_year"] = {"years": [int(y) for y in yearly.index],
                      **{c: [_r(v, 2) for v in yearly[c]] for c in ["kp", "storms", "v", "sunspot"]}}

    # --- how well the "sustained driving" event rule (3-h mean coupling >= 8) flags storms
    c3 = h["newell"].rolling(3, min_periods=2).mean().shift(1)
    cal = pd.DataFrame({"c3": c3, "kp": h["kp"]}).dropna()
    cal = cal[cal.index.hour % 3 == 2]
    hit = cal["c3"] >= 8
    out["storm_rule"] = {
        "threshold": 8, "windows": int(hit.sum()),
        "p_storm_after": _r((cal.loc[hit, "kp"] >= 5).mean()), "base_rate": _r((cal["kp"] >= 5).mean(), 4),
        "share_of_storms_flagged": _r(hit[cal["kp"] >= 5].mean()),
    }

    out["kp_drivers"] = kp_drivers(h)
    return out


def _unitless(x: pd.Series) -> pd.Series:
    """log(1 + x / median): the model then doesn't depend on the units or scale of x
    (multiplying coupling by any constant leaves every R-squared unchanged)."""
    return np.log1p(x / x.median())


def _r2_fit(y_tr, X_tr, y_te=None, X_te=None) -> float:
    A = np.column_stack([np.ones(len(y_tr)), X_tr])
    coef, *_ = np.linalg.lstsq(A, y_tr, rcond=None)
    if y_te is None:
        res = y_tr - A @ coef
        return float(1 - res.var() / y_tr.var())
    B = np.column_stack([np.ones(len(y_te)), X_te])
    res = y_te - B @ coef
    return float(1 - (res ** 2).sum() / ((y_te - y_te.mean()) ** 2).sum())


def _shapley(y: np.ndarray, groups: dict) -> tuple[dict, float]:
    """Order-independent share of R-squared for each group of predictors (Shapley values,
    a.k.a. LMG): every group's average gain over all orders in which groups could be added.
    Adding variables one after another instead makes the split depend on the order."""
    from itertools import combinations
    from math import factorial
    names = list(groups)
    k = len(names)
    cache = {}

    def r2(subset):
        if not subset:
            return 0.0
        if subset not in cache:
            cache[subset] = _r2_fit(y, np.column_stack([groups[g] for g in subset]))
        return cache[subset]
    shares = {}
    for g in names:
        rest = [x for x in names if x != g]
        tot = 0.0
        for size in range(len(rest) + 1):
            w = factorial(size) * factorial(k - size - 1) / factorial(k)
            for sub in combinations(rest, size):
                tot += w * (r2(tuple(sorted(sub + (g,)))) - r2(tuple(sorted(sub))))
        shares[g] = tot
    return shares, r2(tuple(sorted(names)))


def kp_drivers(h: pd.DataFrame) -> dict:
    """What drives Kp, measured on 26 years of hourly OMNI data (one sample per 3-hour Kp window).

    Predictors, all built from data available BEFORE the Kp window they predict:
      solar wind now      Newell coupling over the 3 h ending `best lag` hours earlier,
                          plus a viscous term (n^1/2 v^2; Newell et al. 2008)
      solar wind history  coupling over the 6 hours before that
      Kp memory           Kp of the previous 3-hour window
      season              equinox term cos(4 pi (day - 80) / 365.25)
    """
    h = h.copy()
    h["visc"] = np.sqrt(h["n"]) * h["v"] ** 2
    roll3 = h["newell"].rolling(3, min_periods=2).mean()
    roll6 = h["newell"].rolling(6, min_periods=4).mean()
    pick = h.index.hour % 3 == 2
    lags = list(range(0, 13))
    curve = [_spearman(h["kp"][pick], roll3.shift(L)[pick]) for L in lags]
    best = int(np.nanargmax(curve))

    d = pd.DataFrame({
        "kp": h["kp"],
        "c_now": _unitless(roll3.shift(best)),
        "visc": _unitless(h["visc"].rolling(3, min_periods=2).mean().shift(best)),
        "c_hist": _unitless(roll6.shift(best + 3)),
        "prev": h["kp"].shift(3),
    })[pick].dropna()
    doy = d.index.day_of_year.to_numpy()
    d["season"] = np.cos(4 * np.pi * (doy - 80) / 365.25)
    y = d["kp"].to_numpy()

    groups = {"wind_now": d[["c_now", "visc"]].to_numpy(), "wind_history": d[["c_hist"]].to_numpy(),
              "memory": d[["prev"]].to_numpy(), "season": d[["season"]].to_numpy()}
    shap, r2_full = _shapley(y, groups)

    # sequential (order-dependent) R² gains, shown next to Shapley to illustrate why order matters
    seq_cm = {"coupling_first": [_r2_fit(y, d[["c_now"]].to_numpy()),
                                 _r2_fit(y, d[["c_now", "prev"]].to_numpy())],
              "memory_first": [_r2_fit(y, d[["prev"]].to_numpy()),
                               _r2_fit(y, d[["prev", "c_now"]].to_numpy())]}
    memory_gain_simple = seq_cm["coupling_first"][1] - seq_cm["coupling_first"][0]
    memory_gain_full = r2_full - _r2_fit(y, d[["c_now", "visc", "c_hist", "season"]].to_numpy())

    # honest skill: fit on 2000-2018, test on 2019 onward (a period the model never saw)
    split = pd.Timestamp("2019-01-01", tz="UTC")
    tr, te = d.index < split, d.index >= split
    oos = {}
    if tr.sum() > 1000 and te.sum() > 1000:
        def oo(cols):
            return _r2_fit(y[tr], d.loc[tr, cols].to_numpy(), y[te], d.loc[te, cols].to_numpy())
        oos = {"persistence": oo(["prev"]), "coupling": oo(["c_now"]),
               "wind": oo(["c_now", "visc", "c_hist"]),
               "wind_memory_season": oo(["c_now", "visc", "c_hist", "prev", "season"])}
        try:
            from sklearn.ensemble import HistGradientBoostingRegressor
            gb = HistGradientBoostingRegressor(random_state=0, max_iter=300)
            X = d[["c_now", "visc", "c_hist", "season"]]
            gb.fit(X[tr], y[tr])
            pred = gb.predict(X[te])
            oos["boosting_wind"] = float(1 - ((y[te] - pred) ** 2).sum() / ((y[te] - y[te].mean()) ** 2).sum())
        except Exception:
            pass
        oos = {k: _r(v) for k, v in oos.items()}
        oos["train"] = "2000–2018"; oos["test"] = f"2019–{d.index.max().year}"
        oos["n_test"] = int(te.sum())

    # what the full solar wind model still misses, by month (the equinoctial effect)
    A = np.column_stack([np.ones(len(y)), d[["c_now", "visc", "c_hist"]].to_numpy()])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = pd.Series(y - A @ coef, index=d.index)

    return {
        "n_windows": int(len(d)), "lag_hours": lags, "lag_rho": [_r(v) for v in curve], "best_lag_h": best,
        "rho_best": _r(curve[best]),
        "r2_full": _r(r2_full), "shapley": {k: _r(v) for k, v in shap.items()},
        "sequential": {k: [_r(x) for x in v] for k, v in seq_cm.items()},
        "memory_gain_coupling_only": _r(memory_gain_simple), "memory_gain_after_wind": _r(memory_gain_full),
        "out_of_sample": oos,
        "resid_by_month": [_r(v, 2) for v in resid.groupby(resid.index.month).mean().reindex(range(1, 13))],
        "rho_prev_kp": _r(_spearman(d["kp"], d["prev"])),
    }


# ---------------------------------------------------------------- live Kp link
def autocorr_spearman(a: pd.Series, b: pd.Series, n_boot: int = 2000, seed: int = 0) -> dict:
    """Spearman's rho with uncertainty that respects autocorrelation.

    Kp and coupling change slowly, so neighbouring 3-hour windows are not independent
    samples. Treating them as independent makes p-values far too small (two unrelated
    slow series 'pass' a 5% test much more than 5% of the time). Two corrections:
      effective sample size  n_eff = n (1 - r1a r1b) / (1 + r1a r1b), with r1 the lag-1
                             autocorrelation of each ranked series (Bretherton et al. 1999)
      block bootstrap        resample whole one-day blocks (8 windows) to get a 95% interval
    """
    j = pd.concat([a, b], axis=1, sort=True).dropna()
    n = len(j)
    out = {"n": int(n), "rho": None, "p": None, "p_naive": None, "n_eff": None, "ci": None, "n_needed": None}
    if n < 8:
        return out
    x, y = j.iloc[:, 0].rank().to_numpy(), j.iloc[:, 1].rank().to_numpy()
    rho = float(np.corrcoef(x, y)[0, 1])
    r1 = lambda z: float(np.corrcoef(z[:-1], z[1:])[0, 1]) if np.std(z) > 0 else 0.0
    ra, rb = max(r1(x), 0.0), max(r1(y), 0.0)
    n_eff = float(np.clip(n * (1 - ra * rb) / (1 + ra * rb), 3, n))
    t_stat = rho * np.sqrt(max(n_eff - 2, 1) / max(1 - rho ** 2, 1e-12))
    p = float(2 * stats.t.sf(abs(t_stat), df=max(n_eff - 2, 1)))
    rng = np.random.default_rng(seed)
    L = 8 if n >= 40 else 4
    starts = np.arange(0, n - L + 1)
    boots = []
    for _ in range(n_boot):
        idx = np.concatenate([np.arange(s0, s0 + L) for s0 in rng.choice(starts, int(np.ceil(n / L)))])[:n]
        bx, by = x[idx], y[idx]
        if np.std(bx) > 0 and np.std(by) > 0:
            boots.append(stats.spearmanr(bx, by)[0])
    need = None
    if abs(rho) >= 0.02:
        need_eff = ((1.96 + 0.84) / np.arctanh(min(abs(rho), 0.99))) ** 2 + 3
        need = int(np.ceil(need_eff * n / n_eff))          # convert back to actual windows
    out.update({"rho": _r(rho), "p": _r(p, 4), "p_naive": _r(float(stats.spearmanr(x, y).pvalue), 4),
                "n_eff": _r(n_eff, 1), "autocorr": [_r(ra, 2), _r(rb, 2)],
                "ci": [_r(float(np.percentile(boots, 2.5))), _r(float(np.percentile(boots, 97.5)))] if boots else None,
                "n_needed": need})
    return out


PRIORITY_HOURLY = ["SOLAR1", "IMAP", "ACE"]


def hourly_coupling(hourly: pd.DataFrame) -> pd.DataFrame:
    """Best available spacecraft per hour (SOLAR-1, then IMAP, then ACE) from the hourly
    archive, which is kept forever, so this comparison keeps growing past 30 days.
    Coupling is computed from hourly averages, the same way NASA OMNI's hourly data are."""
    need = ["proton_speed", "by_gsm", "bz_gsm"]
    if hourly is None or hourly.empty or not set(need) <= set(hourly.columns):
        return pd.DataFrame()
    hh = hourly.dropna(subset=need).copy()
    hh["rank"] = hh["source"].map({s: i for i, s in enumerate(PRIORITY_HOURLY)}).fillna(9)
    hh = hh.sort_values(["time_tag", "rank"]).drop_duplicates("time_tag").set_index("time_tag")
    hh["newell"] = newell_coupling(hh["proton_speed"], hh["by_gsm"], hh["bz_gsm"])
    hh["lead_min"] = 1.45e6 / hh["proton_speed"] / 60
    return hh


def live_kp_link(hourly: pd.DataFrame, kp: pd.DataFrame) -> dict:
    """This pipeline's own data vs Kp. Each hour's coupling is moved to the time that wind
    reaches Earth, then 3-hour arrival windows are compared with Kp for the same window."""
    hh = hourly_coupling(hourly)
    if hh.empty or kp.empty:
        return {"n": 0}
    arrival = hh.index + pd.to_timedelta(hh["lead_min"], unit="min")
    c = pd.Series(hh["newell"].to_numpy(), index=arrival).sort_index()
    c3 = c.resample("3h", origin="epoch").mean()
    k = kp.set_index("time_tag")["kp"]
    j = pd.concat([c3.rename("newell"), k.rename("kp")], axis=1, sort=True).dropna()
    raw3 = hh["newell"].resample("3h", origin="epoch").mean()
    jr = pd.concat([raw3.rename("newell"), k.rename("kp")], axis=1, sort=True).dropna()
    t = autocorr_spearman(j["newell"], j["kp"])
    span_days = (hh.index.max() - hh.index.min()).total_seconds() / 86400 if len(hh) else 0
    return {
        "n": t["n"], "rho_shifted": t["rho"], "p_shifted": t["p"], "p_naive": t["p_naive"],
        "n_eff": t["n_eff"], "ci": t["ci"], "autocorr": t.get("autocorr"), "n_needed": t["n_needed"],
        "rho_unshifted": _r(_spearman(jr["newell"], jr["kp"])),
        "kp_range": [_r(j["kp"].min(), 2), _r(j["kp"].max(), 2)] if len(j) else None,
        "mean_lead_min": _r(hh["lead_min"].mean(), 1), "archive_days": _r(span_days, 1),
        "pairs": [{"t": ti.isoformat(), "newell": _r(r.newell, 2), "kp": float(r.kp)} for ti, r in j.iterrows()],
    }


# ---------------------------------------------------------------- agreement
AGREE_VARS = {"proton_speed": "speed", "proton_density": "density", "bz_gsm": "bz"}
BIN = "15min"            # readings averaged into 15-minute bins (smooths 1-minute noise)
CHANGE_BINS = 4          # tracking = hour-to-hour changes (4 bins). Faster wiggles are
                         # turbulence that genuinely differs 100,000+ km apart; hourly
                         # changes are what drives Earth's response.
SHIFT_MIN_BINS = 12      # chance level: others shifted 3+ hours out of sync


def _change_corr(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    da, db = a.diff(CHANGE_BINS), b.diff(CHANGE_BINS)
    j = pd.concat([da, db], axis=1, sort=True).dropna()
    if len(j) < 20 or j.iloc[:, 0].std() == 0 or j.iloc[:, 1].std() == 0:
        return np.nan, len(j)
    return float(j.iloc[:, 0].corr(j.iloc[:, 1])), len(j)


def _chance_level(a: pd.Series, b: pd.Series, n_shifts: int = 40) -> float:
    """95th percentile of the change correlation when b is shifted by 3+ hours, i.e. when the
    two series CAN'T be measuring the same wind. A real match has to beat this."""
    da, db = a.diff(CHANGE_BINS), b.diff(CHANGE_BINS).to_numpy()
    n = len(db)
    if n < 4 * SHIFT_MIN_BINS:
        return np.nan
    shifts = np.unique(np.linspace(SHIFT_MIN_BINS, n - SHIFT_MIN_BINS, n_shifts).astype(int))
    vals = []
    for k in shifts:
        r = pd.concat([da.reset_index(drop=True), pd.Series(np.roll(db, k))], axis=1, sort=True).dropna()
        if len(r) >= 20 and r.iloc[:, 0].std() > 0 and r.iloc[:, 1].std() > 0:
            vals.append(r.iloc[:, 0].corr(r.iloc[:, 1]))
    return float(np.nanpercentile(vals, 95)) if vals else np.nan


def agreement_block(df: pd.DataFrame, tracks: pd.DataFrame | None = None, v_typ: float = 400.0, days: int = 7) -> dict:
    """Two separate questions:

    1. Calibration (level): does a spacecraft read steadily high or low? Median ratio (or
       difference for Bz) against the median of the other spacecraft.
    2. Tracking (movement): does it rise and fall WITH the others? Correlation of 15-minute
       CHANGES against the others, after lining readings up by time. Closeness of values
       can't answer this: a flat-lined sensor sitting near the average would look 'close'.
       Each correlation is compared with its chance level: the same calculation with the
       others shifted 3+ hours out of sync.

    Times are aligned using the measured lag (best Bz cross-correlation against the
    reference spacecraft), so a spacecraft 15 minutes upstream is compared fairly."""
    end = df["time_tag"].max()
    d = df[df["time_tag"] > end - pd.Timedelta(days=days)].copy()
    srcs = sorted(d["source"].unique())
    ref = "SOLAR1" if "SOLAR1" in srcs else srcs[0]

    one = {c: d.pivot_table(index="time_tag", columns="source", values=c).asfreq("1min") for c in AGREE_VARS}
    shifts = {s: 0 for s in srcs}
    for s in srcs:
        if s != ref and s in one["bz_gsm"] and ref in one["bz_gsm"]:
            pair = one["bz_gsm"][[ref, s]].dropna()
            if len(pair) >= 120:
                from .models import best_lag_minutes
                lag, r = best_lag_minutes(one["bz_gsm"][ref], one["bz_gsm"][s], max_lag=40)
                if np.isfinite(r) and r >= 0.3:
                    # lag > 0: s.shift(lag) matches the reference, i.e. s saw the same wind `lag`
                    # minutes EARLIER; delaying s by `lag` lines it up with the reference
                    shifts[s] = int(lag)
    grid = {}
    for c, g in one.items():
        g = g.copy()
        for s in srcs:
            if s in g and shifts[s]:
                g[s] = g[s].shift(shifts[s])
        grid[c] = g.resample(BIN).mean().reindex(columns=srcs)

    sc_out = {}
    for s in srcs:
        V, flags = {}, []
        for c, name in AGREE_VARS.items():
            g = grid[c]
            others = g.drop(columns=s).median(axis=1)
            both = g[s].notna() & others.notna()
            e = {"n": int(both.sum())}
            if both.sum() >= 20:
                if name == "bz":
                    e["offset"] = _r(float((g[s] - others)[both].median()), 2)
                else:
                    e["offset"] = _r(100 * float(((g[s] - others) / others)[both].median()), 1)
                r, n = _change_corr(g[s].where(both), others.where(both))
                ch = _chance_level(g[s].where(both), others.where(both))
                e.update({"r_change": _r(r), "chance": _r(ch), "n_changes": n})
                moving = g[s][both].diff(CHANGE_BINS)
                e["flat"] = bool(moving.abs().fillna(0).le(1e-9).mean() > 0.8)
                if e["flat"]:
                    flags.append(f"{name} barely changes (possible stuck sensor)")
            V[name] = e
            if e.get("offset") is not None:
                lim = 1.0 if name == "bz" else (4 if name == "speed" else 20)
                if abs(e["offset"]) > lim:
                    unit = " nT" if name == "bz" else "%"
                    flags.append(f"reads {name} {e['offset']:+.1f}{unit}" if name == "bz" else f"reads {name} {e['offset']:+.0f}%")
        for v in V.values():
            r, ch = v.get("r_change"), v.get("chance")
            if v.get("flat"):
                v["verdict"] = "flat signal"
            elif r is None or not np.isfinite(r) or ch is None or not np.isfinite(ch):
                v["verdict"] = "not enough data"
            elif r <= ch:
                v["verdict"] = "no better than chance"
            else:
                v["verdict"] = "strong" if r >= 0.7 else "moderate" if r >= 0.4 else "weak"
        # the overall verdict rests on speed and Bz, the two inputs that drive Earth's response;
        # density is reported on its own (real-time density is the noisiest measurement)
        key = [V[k]["verdict"] for k in ("speed", "bz")]
        scored = [v for v in V.values() if v.get("r_change") is not None and np.isfinite(v["r_change"])]
        if "not enough data" in key:
            verdict = "not enough data"
        elif all(k in ("strong", "moderate") for k in key):
            verdict = "in step"
        elif any(k in ("strong", "moderate", "weak") for k in key):
            verdict = "partly in step"
        else:
            verdict = "out of step"
        sc_out[s] = {"vars": V, "verdict": verdict, "time_shift_min": int(shifts[s]),
                     "track_r": _r(float(np.mean([v["r_change"] for v in scored])) if scored else np.nan),
                     "coverage_pct": _r(100 * grid["bz_gsm"][s].notna().mean(), 0),
                     "calibration_flags": flags}

    # headline: how well every pair tracks, per measurement (median over pairs)
    head = {}
    for c, name in AGREE_VARS.items():
        rs = []
        for i, a in enumerate(srcs):
            for b in srcs[i + 1:]:
                r, _ = _change_corr(grid[c][a], grid[c][b])
                if np.isfinite(r):
                    rs.append(r)
        head[name] = _r(float(np.median(rs))) if rs else None

    # hourly strip: within the 6 hours around each hour, do this spacecraft's Bz changes
    # correlate with the others' (r >= 0.4)? Bz, because its sign decides what Earth gets.
    strip_t, status = [], {s: [] for s in srcs}
    gb = grid["bz_gsm"]
    for t in gb.resample("1h").count().index:
        strip_t.append(t.isoformat())
        win = gb.loc[t - pd.Timedelta(hours=3):t + pd.Timedelta(hours=3)]
        for s in srcs:
            if gb[s].loc[t:t + pd.Timedelta(minutes=59)].notna().sum() == 0:
                status[s].append("nodata"); continue
            oth = win.drop(columns=s).median(axis=1)
            if oth.notna().sum() == 0:
                status[s].append("solo"); continue
            pair = pd.concat([win[s].diff(), oth.diff()], axis=1, sort=True).dropna()
            if len(pair) < 12 or pair.iloc[:, 0].std() == 0 or pair.iloc[:, 1].std() == 0:
                status[s].append("quiet"); continue
            status[s].append("agree" if pair.iloc[:, 0].corr(pair.iloc[:, 1]) >= 0.4 else "differ")
    return {"window_days": days, "reference": ref, "spacecraft": sc_out, "headline_r_change": head,
            "change_minutes": CHANGE_BINS * 15, "bin_minutes": 15, "strip": {"t": strip_t, "status": status}}


# ---------------------------------------------------------------- timing
def front_normals(df: pd.DataFrame, ref: str = "SOLAR1", hours: int = 1) -> tuple[np.ndarray, str]:
    """Orientation of solar wind 'phase fronts' from minimum variance analysis (MVA) of the
    reference spacecraft's magnetic field: in each window the direction in which the field
    varies least approximates the normal of the structures sweeping past (Weimer et al. 2003).
    Only well-defined windows are kept (eigenvalue ratio >= 3 within the constrained plane) and the
    normal must lean toward the Sun-Earth line (|n_x| >= 0.3), or the predicted delays blow up."""
    if df.empty or "source" not in df:
        return np.empty((0, 3)), "none"
    d = df[df["source"] == ref].set_index("time_tag").sort_index()
    gse = {"by_gse", "bz_gse"} <= set(d.columns) and d["by_gse"].notna().sum() > 600
    cols = ["bx_gsm", "by_gse", "bz_gse"] if gse else ["bx_gsm", "by_gsm", "bz_gsm"]
    if not set(cols) <= set(d.columns):
        return np.empty((0, 3)), "none"
    d = d[cols].dropna()
    normals = []
    if len(d) < 120:
        return np.empty((0, 3)), "GSE" if gse else "GSM"
    end = d.index.max()
    t = end - pd.Timedelta(days=7)
    while t < end:
        w = d.loc[t:t + pd.Timedelta(hours=hours)]
        t += pd.Timedelta(minutes=30)
        if len(w) < 45:
            continue
        # constrained MVA ("MVAB-0"): the normal must be perpendicular to the mean field.
        # Plain MVA on solar wind turbulence finds the mean-field direction instead (the field
        # mostly swings around it), which points the wrong way for timing.
        B = w.to_numpy()
        bhat = B.mean(axis=0) / np.linalg.norm(B.mean(axis=0))
        P = np.eye(3) - np.outer(bhat, bhat)
        vals, vecs = np.linalg.eigh(P @ np.cov(B.T) @ P)
        keep = [i for i in range(3) if abs(vecs[:, i] @ bhat) < 0.5]       # drop the mean-field axis
        if len(keep) < 2:
            continue
        i_min, i_mid = sorted(keep, key=lambda i: vals[i])[:2]
        if vals[i_min] <= 0 or vals[i_mid] / vals[i_min] < 3:
            continue
        n = vecs[:, i_min] * (1 if vecs[0, i_min] >= 0 else -1)
        if abs(n[0]) >= 0.3:
            normals.append(n)
    return np.array(normals), "GSE" if gse else "GSM (approximate)"


def timing_block(df: pd.DataFrame, tracks: pd.DataFrame, v_typ: float, cross: list[dict]) -> dict:
    """Predicted minutes by which spacecraft b sees the wind before a, under three
    assumptions about how solar wind structures are oriented, next to the measured lag:
      flat fronts      perpendicular to the Sun-Earth line: only the X separation counts
      Parker spiral    fronts along the average 45-degree spiral field: X and Y both count
      measured fronts  normals from minimum variance analysis (median over windows)
    Lead of b over a = n.(r_b - r_a) / (v n_x)."""
    if tracks is None or tracks.empty:
        return {"available": False}
    last = tracks.sort_values("time_tag").groupby("source")[["x_gse", "y_gse", "z_gse"]].last()
    normals, frame = front_normals(df)
    models = {"flat": np.array([[1.0, 0, 0]]), "parker": np.array([[1 / np.sqrt(2), 1 / np.sqrt(2), 0]])}
    if len(normals):
        models["measured"] = normals
    for row in cross:
        a, b = row["pair"].split(" vs ")
        if a not in last.index or b not in last.index:
            continue
        dr = (last.loc[b] - last.loc[a]).to_numpy()
        for name, N in models.items():
            leads = (N @ dr) / (v_typ * N[:, 0]) / 60
            row[f"lag_{name}_min"] = _r(float(np.median(leads)), 1)
            if name == "measured" and len(leads) >= 4:
                row["lag_measured_iqr"] = [_r(float(np.percentile(leads, 25)), 1), _r(float(np.percentile(leads, 75)), 1)]
    errs = {}
    for name in models:
        e = [abs(r[f"lag_{name}_min"] - r["bz_best_lag_min"]) for r in cross
             if r.get(f"lag_{name}_min") is not None and r.get("bz_best_lag_min") is not None]
        errs[name] = _r(float(np.mean(e)), 1) if e else None
    return {"available": True, "frame": frame, "windows": int(len(normals)), "mean_abs_error_min": errs,
            "speed_used": _r(v_typ, 0)}


# ---------------------------------------------------------------- separation
MIN_WITHIN_PAIR_RANGE_KM = 50_000


def separation_block(hourly: pd.DataFrame, tracks: pd.DataFrame, plasma_src: str | None,
                     n_boot: int = 2000, seed: int = 0) -> dict:
    """How much does the wind differ between two points as they get further apart?

    One point per spacecraft pair per day: separation (JPL Horizons) vs the median hourly
    speed difference (%). Uses the hourly archive, which is kept forever, so the record grows
    as the orbits slowly move the spacecraft closer together and further apart.

    Model: difference = (pair's own offset) + slope x separation. Each pair gets its own
    offset, which soaks up instrument calibration differences, so the slope is learned only
    from how each pair's difference changes as ITS separation changes. That needs real
    movement within pairs, so no estimate is shown until a pair has moved 50,000+ km.
    Uncertainty: bootstrap over days."""
    out = {"points": [], "needed_range_km": MIN_WITHIN_PAIR_RANGE_KM}
    if tracks is None or tracks.empty or hourly is None or hourly.empty:
        return out
    pos = tracks.copy()
    pos["day"] = pos["time_tag"].dt.floor("1D")
    pos = pos.groupby(["day", "source"])[["x_gse", "y_gse", "z_gse"]].mean()
    spd = hourly.pivot_table(index="time_tag", columns="source", values="proton_speed")
    srcs = list(spd.columns)
    rows = []
    for i, a in enumerate(srcs):
        for b in srcs[i + 1:]:
            pair = spd[[a, b]].dropna()
            pct = ((pair[a] - pair[b]).abs() / pair.mean(axis=1) * 100)
            for day, g in pct.groupby(pct.index.floor("1D")):
                if len(g) < 12 or (day, a) not in pos.index or (day, b) not in pos.index:
                    continue
                sep = float(np.linalg.norm(pos.loc[(day, a)].to_numpy() - pos.loc[(day, b)].to_numpy()))
                rows.append({"day": day.isoformat(), "pair": f"{a} vs {b}", "sep_km": round(sep),
                             "speed_diff_pct": _r(float(g.median()), 2)})
    out["points"] = rows
    if not rows:
        return out
    df = pd.DataFrame(rows)
    ranges = df.groupby("pair")["sep_km"].agg(lambda x: x.max() - x.min())
    out["within_pair_range_km"] = {k: int(v) for k, v in ranges.items()}
    if len(df) < 10 or ranges.max() < MIN_WITHIN_PAIR_RANGE_KM:
        return out
    pairs = sorted(df["pair"].unique())

    def fit(d):
        X = np.column_stack([(d["pair"] == p).astype(float) for p in pairs] + [d["sep_km"] / 1e5])
        coef, *_ = np.linalg.lstsq(X, d["speed_diff_pct"].to_numpy(), rcond=None)
        return coef
    coef = fit(df)
    rng = np.random.default_rng(seed)
    days = df["day"].unique()
    boots = []
    for _ in range(n_boot):
        pick = rng.choice(days, len(days))
        d = pd.concat([df[df["day"] == x] for x in pick])
        if d["pair"].nunique() == len(pairs):
            boots.append(fit(d)[-1])
    lo, hi = (np.percentile(boots, [2.5, 97.5]) if boots else (np.nan, np.nan))
    out["fit"] = {"pct_per_100k_km": _r(float(coef[-1]), 2), "ci": [_r(float(lo), 2), _r(float(hi), 2)],
                  "pair_offsets_pct": {p: _r(float(c), 2) for p, c in zip(pairs, coef[:-1])}, "n": len(df)}
    if plasma_src and plasma_src in set(tracks["source"]):
        last = tracks[tracks.source == plasma_src].sort_values("time_tag").iloc[-1]
        off = float(np.hypot(last.y_gse, last.z_gse)) / 1e5
        out["earth_estimate"] = {"source": plasma_src, "off_axis_km": round(off * 1e5),
                                 "expected_speed_diff_pct": _r(max(coef[-1] * off, 0), 1),
                                 "ci": [_r(max(lo * off, 0), 1), _r(max(hi * off, 0), 1)]}
    return out
