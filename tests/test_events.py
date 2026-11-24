"""Combining spacecraft into one series, and detecting events in it.

Event rules are tested at their boundaries (just below vs just at the threshold), against
false alarms on quiet data, and for independence from the anomaly model's settings."""
import json

import numpy as np
import pandas as pd
import pytest

from conftest import T0, quiet_week, three_spacecraft
from pipeline.features import add_features
from pipeline.models import RULES, _group, _runs, active_series, calibrate_to_reference, detect_anomalies
from pipeline.run import kp_response

MID = 5 * 1440          # minute index where events are planted (day 5 of the week)


def significant(s, contamination=0.01):
    _, ev = detect_anomalies(add_features(s), contamination)
    return [e for e in ev if e["significant"]]


def near(events, t, minutes=30):
    return [e for e in events if pd.Timestamp(e["start"]) - pd.Timedelta(minutes=minutes) <= t
            <= pd.Timestamp(e["end"]) + pd.Timedelta(minutes=minutes)]


# ---------------------------------------------------------------- helpers
def test_runs_keeps_only_long_enough_stretches():
    m = pd.Series([0, 1, 1, 0, 1, 1, 1, 1, 0], dtype=bool)
    assert list(_runs(m, 3)) == [0, 0, 0, 0, 1, 1, 1, 1, 0]


def test_group_merges_flags_closer_than_the_gap():
    t = pd.DatetimeIndex([T0 + pd.Timedelta(minutes=m) for m in (0, 1, 20, 100, 101)])
    assert _group(t, gap_min=30) == [(t[0], t[2]), (t[3], t[4])]


# ---------------------------------------------------------------- combining spacecraft
def test_active_spacecraft_is_preferred():
    df = three_spacecraft(hours=4)
    s = active_series(df)
    assert (s["mag_source"] == "SOLAR1").all()


def test_falls_back_to_the_next_spacecraft_when_the_active_one_has_no_plasma():
    df = three_spacecraft(hours=4)
    df.loc[df.source == "SOLAR1", ["proton_speed", "proton_density"]] = np.nan
    s = active_series(df)
    assert (s["plasma_source"] == "IMAP").all() and s["proton_speed"].notna().all()


def test_sparse_plasma_is_carried_forward_at_most_10_minutes():
    df = three_spacecraft(hours=2)
    df = df[df.source == "SOLAR1"].copy()
    keep = df["time_tag"] == T0                                  # one plasma reading, then none
    df.loc[~keep, ["proton_speed", "proton_density"]] = np.nan
    s = active_series(df)
    filled = s["proton_speed"].notna()
    assert filled.sum() == 11                                    # the reading + 10 minutes


@pytest.mark.parametrize("factor", [0.5, 1.6, 2.1])
def test_calibration_recovers_a_planted_density_offset(factor):
    df = three_spacecraft(hours=24, scale={"IMAP": factor})
    _, info = calibrate_to_reference(df)
    assert info["reference"] == "SOLAR1"
    assert info["factors"]["IMAP"]["proton_density"] == pytest.approx(1 / factor, rel=0.03)


def test_switching_spacecraft_after_calibration_creates_no_fake_jump():
    df = three_spacecraft(hours=24, scale={"IMAP": 2.1})
    # SOLAR1 plasma drops out every other hour, so the series alternates between spacecraft
    gap = (df.source == "SOLAR1") & (df.time_tag.dt.hour % 2 == 1)
    df.loc[gap, ["proton_speed", "proton_density"]] = np.nan
    s = active_series(df)
    jumps = s["proton_density"].diff().abs()
    assert jumps.max() < 0.5                                   # 5 /cc wind: no 100% jumps at switches


# ---------------------------------------------------------------- false alarms
@pytest.mark.parametrize("seed", range(4))
def test_quiet_week_produces_no_significant_events(seed):
    assert significant(quiet_week(seed)) == []


# ---------------------------------------------------------------- southward field rule
@pytest.mark.parametrize("storm_day", [None, 1])
@pytest.mark.parametrize("seed", range(3))
def test_bz_below_minus_10_for_45_min_is_always_reported(seed, storm_day):
    s = quiet_week(seed, storm_day=storm_day)
    s.iloc[MID:MID + 45, s.columns.get_loc("bz_gsm")] = -10.5
    assert near(significant(s), s.index[MID])


def test_southward_rule_boundary_14_vs_15_minutes():
    hits = {}
    for mins in (14, 15):
        s = quiet_week(1)
        s.iloc[MID:MID + mins, s.columns.get_loc("bz_gsm")] = -12.0
        hits[mins] = bool(near(significant(s), s.index[MID]))
    assert hits == {14: False, 15: True}


def test_southward_rule_boundary_on_the_threshold_value():
    hits = {}
    for bz in (-9.9, -10.0):
        s = quiet_week(1)
        s.iloc[MID:MID + 60, s.columns.get_loc("bz_gsm")] = bz
        hits[bz] = bool(near(significant(s), s.index[MID]))
    assert hits == {-9.9: False, -10.0: True}


# ---------------------------------------------------------------- speed jump rule
@pytest.mark.parametrize("storm_day", [None, 1])
@pytest.mark.parametrize("seed", range(3))
def test_55_kms_speed_jump_is_always_reported(seed, storm_day):
    s = quiet_week(seed, storm_day=storm_day)
    s.iloc[MID:, s.columns.get_loc("proton_speed")] += 55
    assert near(significant(s), s.index[MID])


def test_speed_jump_boundary_45_vs_55_kms():
    hits = {}
    for dv in (45, 55):
        s = quiet_week(2)
        s.iloc[MID:, s.columns.get_loc("proton_speed")] += dv
        hits[dv] = bool(near(significant(s), s.index[MID]))
    assert hits == {45: False, 55: True}


# ---------------------------------------------------------------- event types
def test_speed_alone_is_a_speed_jump_not_a_shock():
    s = quiet_week(3)
    s.iloc[MID:, s.columns.get_loc("proton_speed")] += 80
    ev = near(significant(s), s.index[MID])
    assert ev and ev[0]["type"] == "Speed jump"


def test_speed_density_and_field_jumping_together_is_a_shock():
    s = quiet_week(3)
    for c, k in [("proton_density", 1.6), ("bt", 1.6)]:
        s.iloc[MID:, s.columns.get_loc(c)] *= k
    s.iloc[MID:, s.columns.get_loc("proton_speed")] += 80
    ev = near(significant(s), s.index[MID])
    assert ev and ev[0]["type"] == "Shock"


def test_density_pulse_is_a_pressure_pulse():
    s = quiet_week(4)
    s.iloc[MID:MID + 30, s.columns.get_loc("proton_density")] *= 4
    ev = near(significant(s), s.index[MID])
    assert ev and ev[0]["type"] == "Pressure pulse"


def test_hours_of_moderate_southward_field_are_sustained_driving():
    # Bz -8 nT never crosses the -10 nT rule, but 4 hours of it drives storms
    s = quiet_week(5)
    s.iloc[MID:MID + 240, s.columns.get_loc("bz_gsm")] = -8.0
    s.iloc[MID:MID + 240, s.columns.get_loc("by_gsm")] = 0.0
    ev = near(significant(s), s.index[MID + 120], minutes=120)
    assert ev and ev[0]["type"] == "Sustained southward driving"
    assert any("3-hour" in r for r in ev[0]["significance"])


def test_90_minutes_of_moderate_southward_field_is_not_sustained():
    s = quiet_week(5)
    s.iloc[MID:MID + 90, s.columns.get_loc("bz_gsm")] = -8.0
    s.iloc[MID:MID + 90, s.columns.get_loc("by_gsm")] = 0.0
    assert not near(significant(s), s.index[MID], minutes=120)


def test_rules_do_not_depend_on_the_anomaly_model_setting():
    s = quiet_week(6)
    s.iloc[MID:MID + 45, s.columns.get_loc("bz_gsm")] = -11.0
    s.iloc[MID + 2000:, s.columns.get_loc("proton_speed")] += 60
    strip = lambda ev: [(e["start"], e["end"], e["type"]) for e in ev]
    assert strip(significant(s, 0.001)) == strip(significant(s, 0.05))


# ---------------------------------------------------------------- event records
def test_event_records_are_complete_sorted_and_serialisable():
    s = quiet_week(7)
    s.iloc[MID:MID + 45, s.columns.get_loc("bz_gsm")] = -11.0
    _, ev = detect_anomalies(add_features(s))
    assert ev and json.loads(json.dumps(ev)) == ev
    assert [e["start"] for e in ev] == sorted(e["start"] for e in ev)
    for e in ev:
        span = (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).total_seconds() / 60 + 1
        assert e["minutes"] == span and e["why"]
        assert e["significant"] == bool(e["significance"])
    hit = near([e for e in ev if e["significant"]], s.index[MID])[0]
    assert hit["min_bz"] <= -11.0 and hit["geoeffective"]


def test_too_little_data_gives_no_events_rather_than_an_error():
    s = quiet_week(0, days=1).iloc[:30]
    _, ev = detect_anomalies(add_features(s))
    assert ev == []


def test_thresholds_are_the_documented_ones():
    assert RULES["south"] == {"bz_max": -10.0, "min_minutes": 15}
    assert RULES["speed_jump"]["kms"] == 50.0 and RULES["sustained"]["coupling_3h"] == 8.0


# ---------------------------------------------------------------- what Kp did next
K = pd.Series([1.0, 2.0, 5.0, 6.0, 3.0],
              index=pd.date_range("2026-11-10T00:00", periods=5, freq="3h", tz="UTC"))


def test_kp_before_is_the_last_window_that_ended_before_arrival():
    r = kp_response(pd.Timestamp("2026-11-10T05:00", tz="UTC"), 60, K)   # arrives 06:00
    assert r["arrival"] == "2026-11-10T06:00:00+00:00"
    assert r["kp_before"] == 2.0                                           # 03-06 window ended at arrival


def test_kp_after_is_the_highest_in_the_next_6_hours():
    r = kp_response(pd.Timestamp("2026-11-10T05:00", tz="UTC"), 60, K)
    assert r["kp_after"] == 6.0 and r["kp_after_complete"]


def test_a_storm_already_underway_is_not_credited_as_before():
    # arrival in the middle of the 06-09 window: that window counts as "after", not "before"
    r = kp_response(pd.Timestamp("2026-11-10T06:30", tz="UTC"), 60, K)
    assert r["kp_before"] == 2.0 and r["kp_after"] == 6.0


def test_kp_after_is_marked_incomplete_until_6_hours_have_passed():
    r = kp_response(pd.Timestamp("2026-11-10T13:00", tz="UTC"), 60, K)    # arrives 14:00; data end 15:00
    assert r["kp_after"] == 3.0 and not r["kp_after_complete"]


@pytest.mark.parametrize("lead", [np.nan, None])
def test_unknown_lead_time_assumes_one_hour(lead):
    r = kp_response(pd.Timestamp("2026-11-10T05:00", tz="UTC"), lead, K)
    assert r["arrival"] == "2026-11-10T06:00:00+00:00"


def test_no_kp_yet_gives_none_not_an_error():
    r = kp_response(pd.Timestamp("2026-11-10T05:00", tz="UTC"), 60, K.iloc[:0])
    assert r["kp_before"] is None and r["kp_after"] is None and not r["kp_after_complete"]
