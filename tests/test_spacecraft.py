"""Comparing the three spacecraft: timing offsets, calibration vs tracking, front orientation
and how differences grow with separation. Each test plants a known answer."""
import numpy as np
import pandas as pd
import pytest

from conftest import T0, three_spacecraft
from pipeline.insights import agreement_block, front_normals, separation_block, timing_block
from pipeline.models import best_lag_minutes, cross_spacecraft


# ---------------------------------------------------------------- timing offsets
@pytest.mark.parametrize("lag", [-25, -7, 0, 4, 18])
def test_best_lag_recovers_a_planted_offset(lag):
    rng = np.random.default_rng(abs(lag))
    a = pd.Series(np.convolve(rng.normal(size=2000), np.ones(10) / 10, "same"))
    b = a.shift(-lag) + rng.normal(0, 0.02, 2000)       # b sees each feature `lag` minutes earlier
    assert best_lag_minutes(a, b)[0] == lag


def test_cross_spacecraft_reports_every_pair_with_lag_and_bias():
    df = three_spacecraft(hours=24, lags={"ACE": 12})
    df.loc[df.source == "IMAP", "proton_speed"] += 10
    rows = {r["pair"]: r for r in cross_spacecraft(df)}
    assert set(rows) == {"ACE vs IMAP", "ACE vs SOLAR1", "IMAP vs SOLAR1"}
    assert rows["ACE vs SOLAR1"]["bz_best_lag_min"] == -12      # SOLAR1 sees it 12 min after ACE
    assert rows["IMAP vs SOLAR1"]["proton_speed"]["median_bias"] == pytest.approx(-10, abs=1.5)


# ---------------------------------------------------------------- agreement: calibration vs tracking
def test_matching_spacecraft_are_in_step():
    A = agreement_block(three_spacecraft())["spacecraft"]
    assert all(v["verdict"] == "in step" for v in A.values())


def test_a_spacecraft_upstream_is_lined_up_before_comparing():
    A = agreement_block(three_spacecraft(lags={"ACE": 15}))["spacecraft"]["ACE"]
    assert A["time_shift_min"] == 15 and A["verdict"] == "in step"


def test_a_steady_calibration_offset_is_flagged_but_still_in_step():
    A = agreement_block(three_spacecraft(scale={"ACE": 0.62}))["spacecraft"]["ACE"]
    assert A["verdict"] == "in step"
    assert A["vars"]["density"]["offset"] == pytest.approx(-38, abs=3)
    assert any("density" in f for f in A["calibration_flags"])


def test_a_bz_offset_is_reported_in_nanotesla():
    df = three_spacecraft()
    df.loc[df.source == "IMAP", "bz_gsm"] += 2.0
    A = agreement_block(df)["spacecraft"]["IMAP"]
    assert A["vars"]["bz"]["offset"] == pytest.approx(2.0, abs=0.3)
    assert any("bz" in f and "nT" in f for f in A["calibration_flags"])


def test_a_flat_lined_sensor_is_called_flat_not_in_step():
    df = three_spacecraft()
    df.loc[df.source == "SOLAR1", "proton_density"] = 3.0
    v = agreement_block(df)["spacecraft"]["SOLAR1"]["vars"]["density"]
    assert v["verdict"] == "flat signal"


def test_scrambled_data_scores_no_better_than_chance():
    df = three_spacecraft()
    sel = df.source == "IMAP"
    df.loc[sel, "bz_gsm"] = df.loc[sel, "bz_gsm"].sample(frac=1, random_state=1).to_numpy()
    A = agreement_block(df)["spacecraft"]["IMAP"]
    assert A["vars"]["bz"]["verdict"] == "no better than chance"
    assert A["verdict"] != "in step"


def test_a_bz_sign_flip_is_out_of_step_even_though_magnitudes_match():
    df = three_spacecraft()
    sel = df.source == "IMAP"
    df.loc[sel, "bz_gsm"] *= -1
    A = agreement_block(df)["spacecraft"]["IMAP"]
    assert A["vars"]["bz"]["r_change"] < 0 and A["vars"]["bz"]["verdict"] == "no better than chance"


def test_headline_and_strip_are_well_formed():
    out = agreement_block(three_spacecraft())
    assert all(v is not None and v > 0.7 for v in out["headline_r_change"].values())
    allowed = {"agree", "differ", "quiet", "solo", "nodata"}
    for s, st in out["strip"]["status"].items():
        assert len(st) == len(out["strip"]["t"]) and set(st) <= allowed
    assert out["change_minutes"] == 60 and out["bin_minutes"] == 15


def test_a_spacecraft_with_too_little_overlap_is_not_judged():
    df = three_spacecraft(hours=48)
    df = df[(df.source != "ACE") | (df.time_tag < T0 + pd.Timedelta(hours=3))]
    assert agreement_block(df)["spacecraft"]["ACE"]["verdict"] == "not enough data"


# ---------------------------------------------------------------- front orientation and timing
def _tracks(pos):
    return pd.DataFrame([{"time_tag": T0, "source": s, "x_gse": x, "y_gse": y, "z_gse": z}
                         for s, (x, y, z) in pos.items()])


def test_flat_fronts_predict_lag_from_upstream_distance_only():
    cross = [{"pair": "ACE vs SOLAR1", "bz_best_lag_min": None}]
    timing_block(pd.DataFrame(columns=["time_tag", "source"]),
                 _tracks({"SOLAR1": (1.45e6, 0, 0), "ACE": (1.45e6 + 36_000, 5e5, 0)}), 400, cross)
    # ACE is 36,000 km further upstream, so SOLAR1 (second in the pair) sees the wind 1.5 min LATER
    assert cross[0]["lag_flat_min"] == pytest.approx(-36_000 / 400 / 60, abs=0.05)


def test_tilted_fronts_predict_a_lead_from_sideways_separation():
    cross = [{"pair": "ACE vs SOLAR1", "bz_best_lag_min": -17}]
    out = timing_block(pd.DataFrame(columns=["time_tag", "source"]),
                       _tracks({"SOLAR1": (1.45e6, -3.0e5, 0), "ACE": (1.45e6, 2.6e5, 0)}), 400, cross)
    assert abs(cross[0]["lag_flat_min"]) < 0.1
    # Parker spiral normal (1,1,0)/sqrt2: lead = dy / v = 560,000 / 400 s = 23.3 min
    assert cross[0]["lag_parker_min"] == pytest.approx(-23.3, abs=0.1)
    assert out["mean_abs_error_min"]["flat"] == pytest.approx(17, abs=0.1)


def test_timing_without_positions_is_unavailable():
    assert timing_block(pd.DataFrame(), pd.DataFrame(), 400, [])["available"] is False


def test_minimum_variance_recovers_a_planted_front_normal():
    # planar structures with normal n sweep past: the field varies across the front, not along n,
    # and the mean field lies in the front plane (perpendicular to n)
    rng = np.random.default_rng(0)
    n = np.array([1.0, 1.0, 0.0]) / np.sqrt(2)
    e_mean = np.array([1.0, -1.0, 0.0]) / np.sqrt(2)
    e_var = np.array([0.0, 0.0, 1.0])
    t = pd.date_range(T0, periods=7 * 1440, freq="1min", tz="UTC")
    B = (5 * e_mean + np.outer(np.convolve(rng.normal(0, 3, len(t)), np.ones(5) / 5, "same"), e_var)
         + np.outer(rng.normal(0, 0.4, len(t)), e_mean) + np.outer(rng.normal(0, 0.1, len(t)), n))
    df = pd.DataFrame({"time_tag": t, "source": "SOLAR1", "bx_gsm": B[:, 0], "by_gse": B[:, 1],
                       "bz_gse": B[:, 2], "by_gsm": B[:, 1], "bz_gsm": B[:, 2]})
    normals, frame = front_normals(df)
    assert frame == "GSE" and len(normals) > 100
    med = np.median(normals, axis=0)
    assert np.degrees(np.arccos(abs(med @ n) / np.linalg.norm(med))) < 5


# ---------------------------------------------------------------- separation
def _separation_data(slope_pct_per_100k=2.0, offset=1.0, sep_range=(1e5, 4e5), days=60, seed=0):
    rng = np.random.default_rng(seed)
    hours = pd.date_range(T0, periods=days * 24, freq="1h", tz="UTC")
    day_idx = (hours - hours[0]).days
    sep = np.interp(day_idx, [0, days - 1], sep_range)
    diff_pct = offset + slope_pct_per_100k * sep / 1e5 + rng.normal(0, 0.3, len(hours))
    a = 400 + 20 * np.sin(np.arange(len(hours)) / 30)
    d = diff_pct / 100
    b = a * (1 + d / 2) / (1 - d / 2)            # so |a - b| / mean(a, b) is exactly diff_pct
    hourly = pd.concat([pd.DataFrame({"time_tag": hours, "source": "SOLAR1", "proton_speed": a}),
                        pd.DataFrame({"time_tag": hours, "source": "ACE", "proton_speed": b})])
    days_t = pd.date_range(T0, periods=days, freq="1D", tz="UTC")
    sep_d = np.interp(np.arange(days), [0, days - 1], sep_range)
    tracks = pd.concat([pd.DataFrame({"time_tag": days_t, "source": "SOLAR1", "x_gse": 1.5e6, "y_gse": 0.0, "z_gse": 0.0}),
                        pd.DataFrame({"time_tag": days_t, "source": "ACE", "x_gse": 1.5e6, "y_gse": sep_d, "z_gse": 0.0})])
    return hourly, tracks


def test_separation_slope_is_recovered_with_an_interval_that_contains_it():
    hourly, tracks = _separation_data()
    fit = separation_block(hourly, tracks, "SOLAR1", n_boot=300)["fit"]
    assert fit["pct_per_100k_km"] == pytest.approx(2.0, abs=0.15)
    assert fit["ci"][0] <= 2.0 <= fit["ci"][1]


def test_separation_waits_until_a_pair_has_moved_enough():
    hourly, tracks = _separation_data(sep_range=(2.0e5, 2.2e5))   # only 20,000 km of movement
    out = separation_block(hourly, tracks, "SOLAR1", n_boot=50)
    assert "fit" not in out and out["within_pair_range_km"]["ACE vs SOLAR1"] < 50_000


def test_separation_without_data_is_empty_not_an_error():
    assert separation_block(pd.DataFrame(), pd.DataFrame(), None)["points"] == []
