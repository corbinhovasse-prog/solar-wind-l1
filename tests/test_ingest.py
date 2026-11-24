"""Parsing the raw feeds: NOAA SWPC JSON, JPL Horizons vectors and NASA OMNI text.
Feeds are messy (two layouts, strings for numbers, stamps a few seconds off the minute,
fill values), so each quirk has a test."""
import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings, strategies as st

from pipeline.ephemeris import parse_vectors, to_gse
from pipeline.fetch import collapse_minutes, load_kp, merge_wind_mag, to_frame, to_minute
from pipeline.omni import load_omni, parse


# ---------------------------------------------------------------- SWPC layouts
def test_both_swpc_layouts_parse_to_the_same_frame():
    a = to_frame([{"time_tag": "2026-01-01T00:00:00", "Kp": 2.0}])
    b = to_frame([["time_tag", "Kp"], ["2026-01-01T00:00:00", "2.0"]])
    assert list(a.columns) == list(b.columns) == ["time_tag", "Kp"]


@pytest.mark.parametrize("raw", [[], None])
def test_empty_feed_gives_empty_frame(raw):
    assert to_frame(raw).empty


def test_kp_feed_parses_strings_and_drops_garbage():
    kp = load_kp([["time_tag", "Kp"], ["2026-01-01T00:00:00", "2.33"], ["2026-01-01T03:00:00", "n/a"],
                  ["not a time", "4"]])
    assert len(kp) == 1 and kp["kp"].iloc[0] == pytest.approx(2.33)
    assert str(kp["time_tag"].dt.tz) == "UTC"


# ---------------------------------------------------------------- minute alignment
def _w(t, src="IMAP", **kw):
    return {"time_tag": t, "source": src, "active": False, "proton_speed": 400.0, "proton_density": 5.0, **kw}


def _m(t, src="IMAP", **kw):
    return {"time_tag": t, "source": src, "active": False, "bt": 5.0, "bx_gsm": 1.0, "by_gsm": 1.0,
            "bz_gsm": -2.0, **kw}


def test_plasma_and_field_stamped_seconds_apart_are_paired():
    wind = [_w(f"2026-11-10T00:{m:02d}:01", proton_speed=400.0 + m) for m in range(10)]
    mag = [_m(f"2026-11-10T00:{m:02d}:03") for m in range(10)]
    df = merge_wind_mag(wind, mag)
    assert len(df) == 10
    assert (df["proton_speed"].notna() & df["bz_gsm"].notna()).all()
    assert (df["time_tag"].dt.second == 0).all()


@given(st.integers(0, 59))
def test_any_second_within_a_minute_floors_to_that_minute(sec):
    t = to_minute(pd.Series([f"2026-11-10T05:17:{sec:02d}"]))
    assert t.iloc[0] == pd.Timestamp("2026-11-10T05:17:00", tz="UTC")


def test_two_records_in_one_minute_are_averaged_not_duplicated():
    df = pd.DataFrame({"time_tag": pd.to_datetime(["2026-01-01T00:00:00Z"] * 2), "source": ["ACE", "ACE"],
                       "proton_speed": [400.0, 410.0]})
    out = collapse_minutes(df, ["proton_speed"])
    assert len(out) == 1 and out["proton_speed"].iloc[0] == 405.0


def test_non_numeric_values_become_missing_not_errors():
    df = merge_wind_mag([_w("2026-01-01T00:00:00", proton_speed="—"), _w("2026-01-01T00:01:00")],
                        [_m("2026-01-01T00:00:00"), _m("2026-01-01T00:01:00")])
    assert df["proton_speed"].isna().sum() == 1 and df["bz_gsm"].notna().all()


def test_active_flag_accepts_strings_and_booleans():
    wind = [_w("2026-01-01T00:00:00", src="SOLAR1", active="true"), _w("2026-01-01T00:00:00", src="ACE", active=False)]
    df = merge_wind_mag(wind, [])
    assert df.set_index("source").loc["SOLAR1", "active"] and not df.set_index("source").loc["ACE", "active"]


def test_field_only_minutes_are_kept():
    # the live feed often has the field without plasma: those minutes must not be dropped
    df = merge_wind_mag([_w("2026-01-01T00:00:00")], [_m("2026-01-01T00:00:00"), _m("2026-01-01T00:01:00")])
    assert len(df) == 2 and df["proton_speed"].isna().sum() == 1


def test_rows_with_no_values_at_all_are_dropped():
    df = merge_wind_mag([_w("2026-01-01T00:00:00", proton_speed=None, proton_density=None)],
                        [_m("2026-01-01T00:01:00")])
    assert len(df) == 1


# ---------------------------------------------------------------- JPL Horizons
SUN_ROW = "2461361.500000000, A.D. 2026-Nov-17 00:00:00.0000, -8.638147671941963e07, -1.200917024777759e08, 7.915319478347898e03,"
SC_ROW = "2461361.500000000, A.D. 2026-Nov-17 00:00:00.0000, -1.092321430902997e06, -1.003392401594134e06, -1.014824458381130e05,"


def test_horizons_parser_ignores_header_and_malformed_lines():
    text = "header junk\n$$SOE\n" + SUN_ROW + "\nbroken line\n$$EOE\ntrailer"
    v = parse_vectors(text)
    assert len(v) == 1 and v["x"].iloc[0] == pytest.approx(-8.638147671941963e07)


def test_real_solar1_position_is_at_l1():
    g = to_gse(parse_vectors(f"$$SOE\n{SC_ROW}\n$$EOE"), parse_vectors(f"$$SOE\n{SUN_ROW}\n$$EOE")).iloc[0]
    assert 1.4e6 < g.x_gse < 1.6e6
    assert abs(g.y_gse) < 5e5 and abs(g.z_gse) < 5e5


vec = st.floats(-2e8, 2e8)


@settings(max_examples=60)
@given(vec, vec, st.floats(-1e7, 1e7), vec, vec, vec)
def test_gse_rotation_preserves_distance_and_puts_the_sun_on_plus_x(sx, sy, sz, px, py, pz):
    if np.hypot(sx, sy) < 1e6:            # the Sun is never near the ecliptic pole as seen from Earth
        return
    t = pd.Timestamp("2026-01-01", tz="UTC")
    sun = pd.DataFrame({"time_tag": [t], "x": [sx], "y": [sy], "z": [sz]})
    sc = pd.DataFrame({"time_tag": [t, t], "x": [px, sx], "y": [py, sy], "z": [pz, sz]})
    g = to_gse(sc, sun)
    assert np.hypot.reduce(g.iloc[0][["x_gse", "y_gse", "z_gse"]].to_numpy(float)) == \
        pytest.approx(np.linalg.norm([px, py, pz]), rel=1e-9, abs=1e-3)
    s = g.iloc[1]
    assert s.x_gse == pytest.approx(np.linalg.norm([sx, sy, sz]), rel=1e-9)
    assert abs(s.y_gse) < 1e-3 * abs(s.x_gse) and abs(s.z_gse) < 1e-3 * abs(s.x_gse)


def test_gse_frame_is_right_handed():
    # Sun along ecliptic +x: GSE y must point along ecliptic +y (dusk), z along ecliptic north
    t = pd.Timestamp("2026-01-01", tz="UTC")
    sun = pd.DataFrame({"time_tag": [t], "x": [1.5e8], "y": [0.0], "z": [0.0]})
    sc = pd.DataFrame({"time_tag": [t] * 3, "x": [0.0, 0.0, 1.0], "y": [1.0, 0.0, 0.0], "z": [0.0, 1.0, 0.0]})
    g = to_gse(sc, sun)[["x_gse", "y_gse", "z_gse"]].to_numpy()
    np.testing.assert_allclose(g, [[0, 1, 0], [0, 0, 1], [1, 0, 0]], atol=1e-12)


# ---------------------------------------------------------------- NASA OMNI
def test_omni_values_and_fill_values():
    df = parse("2026-10-01T00:30:00.000Z,3.6,0.6,9.1,284,23,33,4\n"
               "2026-10-01T01:30:00.000Z,999.9,999.9,999.9,9999,999,99,99999\n")
    assert df.loc[0, "kp"] == pytest.approx(3.3)       # OMNI stores Kp x 10
    assert df.loc[0, "v"] == 284 and df.loc[0, "dst"] == 4
    assert df.iloc[1][["by", "bz", "n", "v", "kp", "dst"]].isna().all()


def test_omni_times_are_utc():
    df = parse("2026-10-01T00:30:00.000Z,3.6,0.6,9.1,284,23,33,4\n")
    assert str(df["time_tag"].dt.tz) == "UTC"


def test_omni_offline_falls_back_to_cache(tmp_path):
    cache = parse("2026-10-01T00:30:00.000Z,3.6,0.6,9.1,284,23,33,4\n")
    cache.to_csv(tmp_path / "omni_hourly.csv.gz", index=False, compression="gzip")
    got = load_omni(tmp_path, offline_dir=str(tmp_path / "no_feeds_here"))
    assert len(got) == 1 and got["v"].iloc[0] == 284


def test_omni_offline_without_cache_is_empty_not_an_error(tmp_path):
    assert load_omni(tmp_path, offline_dir=str(tmp_path)).empty


@pytest.mark.parametrize("which", ["wind", "mag", "both"])
def test_an_empty_feed_does_not_crash_the_merge(which):
    wind = [] if which in ("wind", "both") else [_w("2026-01-01T00:00:00")]
    mag = [] if which in ("mag", "both") else [_m("2026-01-01T00:00:00")]
    df = merge_wind_mag(wind, mag)
    assert len(df) == (0 if which == "both" else 1)
    assert {"time_tag", "source", "proton_speed", "bz_gsm", "active"} <= set(df.columns)
