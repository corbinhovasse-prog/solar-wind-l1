"""Statistical methods, tested by simulation: plant a known truth, check the method finds it,
and check that error rates and interval coverage are what they claim to be."""
import math

import numpy as np
import pandas as pd
import pytest

from pipeline.features import newell_coupling
from pipeline.insights import (_n_needed, _r2_fit, _shapley, _unitless, autocorr_spearman, kp_drivers,
                               live_kp_link)


def ar1(rng, phi, n, innov=None):
    e = rng.normal(size=n) if innov is None else innov
    x = np.zeros(n)
    x[0] = e[0] / np.sqrt(max(1 - phi ** 2, 1e-9))
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


# ---------------------------------------------------------------- R² and Shapley shares
def test_r2_is_one_for_an_exact_line_and_zero_for_noise():
    rng = np.random.default_rng(0)
    x = rng.normal(size=5000)
    assert _r2_fit(3 * x + 2, x[:, None]) == pytest.approx(1.0)
    assert _r2_fit(rng.normal(size=5000), x[:, None]) < 0.002


def test_out_of_sample_r2_goes_negative_for_a_model_that_fails_on_new_data():
    rng = np.random.default_rng(1)
    x = rng.normal(size=1000)
    assert _r2_fit(x, x[:, None], -x, x[:, None]) < -1     # relation flips in the test period


def test_shapley_shares_sum_to_the_full_r2_and_ignore_order():
    rng = np.random.default_rng(0)
    a = rng.normal(size=2000); b = 0.8 * a + 0.6 * rng.normal(size=2000); c = rng.normal(size=2000)
    y = a + b + 0.5 * c + rng.normal(size=2000)
    s1, r2 = _shapley(y, {"a": a[:, None], "b": b[:, None], "c": c[:, None]})
    s2, _ = _shapley(y, {"c": c[:, None], "b": b[:, None], "a": a[:, None]})
    assert sum(s1.values()) == pytest.approx(r2, abs=1e-12)
    assert all(s1[k] == pytest.approx(s2[k], abs=1e-12) for k in s1)


def test_shapley_gives_independent_predictors_their_own_r2():
    rng = np.random.default_rng(2)
    n = 20000
    a, b = rng.normal(size=n), rng.normal(size=n)
    y = 2 * a + 1 * b + rng.normal(size=n)                 # theory: shares 4/6 and 1/6
    s, _ = _shapley(y, {"a": a[:, None], "b": b[:, None]})
    assert s["a"] == pytest.approx(4 / 6, abs=0.02) and s["b"] == pytest.approx(1 / 6, abs=0.02)


def test_shapley_splits_a_duplicated_predictor_equally():
    rng = np.random.default_rng(3)
    a = rng.normal(size=3000)
    y = a + rng.normal(size=3000)
    s, r2 = _shapley(y, {"a": a[:, None], "copy": a.copy()[:, None]})
    assert s["a"] == pytest.approx(s["copy"], abs=1e-9) == pytest.approx(r2 / 2, abs=1e-9)


@pytest.mark.parametrize("k", [1e-3, 7.0, 1e4])
def test_unit_free_transform_makes_r2_independent_of_units(k):
    rng = np.random.default_rng(1)
    c = pd.Series(np.abs(rng.lognormal(1, 1, 3000)))
    y = np.log1p(c.to_numpy()) + rng.normal(0, 0.3, 3000)
    assert _r2_fit(y, _unitless(c).to_numpy()[:, None]) == pytest.approx(
        _r2_fit(y, _unitless(c * k).to_numpy()[:, None]), abs=1e-12)


# ---------------------------------------------------------------- sample size for a correlation
@pytest.mark.parametrize("rho,n", [(0.3, 85), (0.5, 29), (0.1, 782)])
def test_samples_needed_match_the_textbook_fisher_z_values(rho, n):
    # 5% two-sided, 80% power; the standard table (Hulley et al., Designing Clinical Research)
    assert _n_needed(rho) == n


def test_no_sample_size_for_a_zero_correlation():
    assert _n_needed(0.0) is None and _n_needed(float("nan")) is None


# ---------------------------------------------------------------- autocorrelation-aware test
def test_false_positive_rate_stays_near_5_percent_for_slow_unrelated_series():
    rng = np.random.default_rng(2)
    runs, fp, naive = 300, 0, 0
    for _ in range(runs):
        t = autocorr_spearman(pd.Series(ar1(rng, 0.8, 120)), pd.Series(ar1(rng, 0.7, 120)), n_boot=10)
        fp += t["p"] < 0.05
        naive += t["p_naive"] < 0.05
    assert fp / runs < 0.10
    assert naive / runs > 2 * fp / runs           # the naive test is badly overconfident


def test_a_real_correlation_is_still_detected():
    rng = np.random.default_rng(4)
    hits = 0
    for _ in range(40):
        x = ar1(rng, 0.5, 120)
        y = 0.7 * x + 0.71 * ar1(rng, 0.5, 120)
        hits += autocorr_spearman(pd.Series(x), pd.Series(y), n_boot=10)["p"] < 0.05
    assert hits >= 38


def test_block_bootstrap_interval_covers_the_true_correlation():
    # jointly Gaussian AR(1) pair with correlation r: true Spearman rho = (6/pi) asin(r/2)
    rng = np.random.default_rng(5)
    r, phi, runs = 0.6, 0.5, 60
    true = 6 / math.pi * math.asin(r / 2)
    cover = 0
    for _ in range(runs):
        x = ar1(rng, phi, 160)
        z = ar1(rng, phi, 160)
        y = r * x + math.sqrt(1 - r ** 2) * z
        lo, hi = autocorr_spearman(pd.Series(x), pd.Series(y), n_boot=200, seed=int(rng.integers(1e9)))["ci"]
        cover += lo <= true <= hi
    assert cover / runs >= 0.85                    # nominal 95%; block bootstraps run a little narrow


def test_effective_sample_size_shrinks_with_autocorrelation():
    rng = np.random.default_rng(6)
    n = 400
    white = autocorr_spearman(pd.Series(rng.normal(size=n)), pd.Series(rng.normal(size=n)), n_boot=10)
    slow = autocorr_spearman(pd.Series(ar1(rng, 0.9, n)), pd.Series(ar1(rng, 0.9, n)), n_boot=10)
    assert white["n_eff"] > 0.85 * n
    theory = n * (1 - 0.81) / (1 + 0.81)           # Bretherton et al. (1999): ~42
    assert theory / 2 < slow["n_eff"] < theory * 2.5


def test_too_few_points_returns_no_statistics():
    t = autocorr_spearman(pd.Series(range(5)), pd.Series(range(5)))
    assert t["rho"] is None and t["p"] is None and t["n"] == 5


def test_missing_values_are_dropped_pairwise():
    rng = np.random.default_rng(7)
    a, b = pd.Series(rng.normal(size=100)), pd.Series(rng.normal(size=100))
    a[::10] = np.nan
    assert autocorr_spearman(a, b, n_boot=10)["n"] == 90


# ---------------------------------------------------------------- Kp drivers on synthetic OMNI
@pytest.fixture(scope="module")
def synthetic_omni():
    """Eight years of hourly 'OMNI' in which Kp responds to coupling 2 hours later."""
    rng = np.random.default_rng(8)
    t = pd.date_range("2015-01-01", "2022-12-31 23:00", freq="1h", tz="UTC")
    n = len(t)
    v = 400 + 60 * ar1(rng, 0.99, n) * 0.15
    by, bz = 3 * ar1(rng, 0.9, n) * 0.45, 3 * ar1(rng, 0.9, n) * 0.45
    dens = np.abs(5 + ar1(rng, 0.95, n) * 0.5)
    h = pd.DataFrame({"v": v, "by": by, "bz": bz, "n": dens}, index=t)
    h["newell"] = newell_coupling(h["v"], h["by"], h["bz"])
    drive = np.log1p(h["newell"].rolling(3).mean().shift(2) / h["newell"].median())
    h["kp"] = (1 + 1.5 * drive + 0.3 * rng.normal(size=n)).clip(0, 9)
    return h.dropna()


def test_kp_drivers_finds_the_planted_response_lag(synthetic_omni):
    assert kp_drivers(synthetic_omni)["best_lag_h"] == 2


def test_kp_drivers_shares_add_up_and_wind_dominates(synthetic_omni):
    k = kp_drivers(synthetic_omni)
    assert sum(k["shapley"].values()) == pytest.approx(k["r2_full"], abs=0.005)
    assert k["shapley"]["wind_now"] > k["shapley"]["memory"] > k["shapley"]["season"]
    assert abs(k["shapley"]["season"]) < 0.01                  # no season was planted


def test_kp_drivers_scores_on_years_it_never_saw(synthetic_omni):
    oos = kp_drivers(synthetic_omni)["out_of_sample"]
    assert oos["train"] == "2000–2018" and oos["test"].startswith("2019")
    assert oos["n_test"] > 1000
    assert oos["wind"] > oos["persistence"]                    # the planted driver beats "no change"
    assert oos["wind"] > 0.6


# ---------------------------------------------------------------- live Kp link
def _hourly_and_kp(days=12, seed=9):
    rng = np.random.default_rng(seed)
    t = pd.date_range("2026-11-01", periods=days * 24, freq="1h", tz="UTC")
    bz = 4 * ar1(rng, 0.8, len(t)) * 0.6
    hourly = pd.DataFrame({"time_tag": t, "source": "SOLAR1", "proton_speed": 420.0,
                           "by_gsm": 1.0, "bz_gsm": bz})
    c = newell_coupling(420.0, 1.0, bz)
    arr = pd.Series(c, index=t + pd.Timedelta(minutes=1.45e6 / 420 / 60))
    c3 = arr.resample("3h", origin="epoch").mean()
    kp = pd.DataFrame({"time_tag": c3.index, "kp": (np.log1p(c3.to_numpy()) * 1.5).round(2)})
    return hourly, kp


def test_live_kp_link_recovers_a_planted_relation():
    hourly, kp = _hourly_and_kp()
    r = live_kp_link(hourly, kp)
    assert r["n"] >= 80 and r["rho_shifted"] > 0.9 and r["p_shifted"] < 0.01
    assert r["n_eff"] <= r["n"] and r["ci"][0] <= r["rho_shifted"] <= r["ci"][1]


def test_live_kp_link_uses_the_best_spacecraft_per_hour():
    hourly, kp = _hourly_and_kp()
    junk = hourly.assign(source="ACE", bz_gsm=99.0)          # lower priority than SOLAR1: ignored
    assert live_kp_link(pd.concat([hourly, junk]), kp)["rho_shifted"] == live_kp_link(hourly, kp)["rho_shifted"]


def test_live_kp_link_without_data_is_empty_not_an_error():
    assert live_kp_link(pd.DataFrame(), pd.DataFrame(columns=["time_tag", "kp"]))["n"] == 0
