"""Physics formulas: known values from the literature, limits, symmetries and scaling laws.
Property-based tests (hypothesis) check the laws hold for any physical input, not just one."""
import json
import math
import shutil
import subprocess

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, strategies as st

from conftest import ROOT
from pipeline.features import (HELIUM_RATIO, add_features, clock_angle_rad, dynamic_pressure_npa,
                               lead_time_minutes, newell_coupling)

speed = st.floats(200, 1200)
dens = st.floats(0.1, 80)
bcomp = st.floats(-60, 60)


# ---------------------------------------------------------------- dynamic pressure
def test_pressure_protons_only_matches_hand_calculation():
    # m_p n v^2 = 1.6726e-27 kg * 5e6 m^-3 * (4e5 m/s)^2 = 1.338e-9 Pa
    assert dynamic_pressure_npa(5, 400, alpha_ratio=0) == pytest.approx(1.338, abs=1e-3)


def test_pressure_with_typical_helium_is_16_percent_higher():
    assert dynamic_pressure_npa(5, 400) == pytest.approx(1.338 * (1 + 4 * HELIUM_RATIO), rel=1e-3)


def test_pressure_matches_omni_convention_at_5_percent_helium():
    # NASA OMNI uses Pdyn = 2.0e-6 n v^2 nPa, which corresponds to ~5% helium
    assert dynamic_pressure_npa(7, 450, alpha_ratio=0.05) == pytest.approx(2.0e-6 * 7 * 450 ** 2, rel=0.01)


@given(dens, speed, st.floats(1.1, 5))
def test_pressure_is_linear_in_density_and_quadratic_in_speed(n, v, k):
    p = dynamic_pressure_npa(n, v)
    assert dynamic_pressure_npa(k * n, v) == pytest.approx(k * p, rel=1e-9)
    assert dynamic_pressure_npa(n, k * v) == pytest.approx(k ** 2 * p, rel=1e-9)


# ---------------------------------------------------------------- clock angle & coupling
@pytest.mark.parametrize("by,bz,deg", [(0, 5, 0), (5, 0, 90), (0, -5, 180), (-5, 0, -90)])
def test_clock_angle_cardinal_directions(by, bz, deg):
    assert math.degrees(clock_angle_rad(by, bz)) == pytest.approx(deg)


@given(speed, st.floats(0, 60))
def test_coupling_vanishes_for_due_north_field(v, bz):
    assert newell_coupling(v, 0.0, bz) == pytest.approx(0.0, abs=1e-9)


@given(speed, bcomp, bcomp)
def test_coupling_is_non_negative_and_symmetric_in_by(v, by, bz):
    c = newell_coupling(v, by, bz)
    assert c >= 0
    assert newell_coupling(v, -by, bz) == pytest.approx(c, rel=1e-9, abs=1e-12)


@given(speed, st.floats(1, 40), st.floats(1.1, 3))
def test_coupling_scales_as_v_to_the_4_3(v, b, k):
    assert newell_coupling(k * v, 0.0, -b) == pytest.approx(k ** (4 / 3) * newell_coupling(v, 0.0, -b), rel=1e-9)


@given(speed, st.floats(1, 40), st.floats(1.1, 3))
def test_coupling_scales_as_field_to_the_2_3(v, b, k):
    assert newell_coupling(v, 0.0, -k * b) == pytest.approx(k ** (2 / 3) * newell_coupling(v, 0.0, -b), rel=1e-9)


def test_coupling_is_largest_for_due_south_field_at_fixed_strength():
    angles = np.linspace(0, 2 * np.pi, 361)
    c = newell_coupling(500, 8 * np.sin(angles), 8 * np.cos(angles))
    assert math.degrees(angles[int(np.argmax(c))]) == pytest.approx(180, abs=1)


def test_coupling_known_value_due_south():
    # 400^(4/3) * 5^(2/3) * 1 / 1e3
    assert newell_coupling(400, 0.0, -5.0) == pytest.approx(400 ** (4 / 3) * 5 ** (2 / 3) / 1e3, rel=1e-12)


# ---------------------------------------------------------------- lead time
def test_lead_time_at_400_kms():
    assert lead_time_minutes(400) == pytest.approx(1.5e6 / 400 / 60)   # 62.5 min


@given(speed, st.floats(1.1, 3))
def test_lead_time_is_inversely_proportional_to_speed(v, k):
    assert lead_time_minutes(k * v) == pytest.approx(lead_time_minutes(v) / k, rel=1e-9)


# ---------------------------------------------------------------- add_features
def _frame(n=200, alpha=None):
    t = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    d = pd.DataFrame({"proton_speed": 400.0, "proton_density": 5.0, "by_gsm": 0.0, "bz_gsm": -5.0}, index=t)
    if alpha is not None:
        d["alpha_density"] = alpha
    return d


def test_add_features_uses_typical_helium_when_not_measured():
    f = add_features(_frame())
    assert f["pdyn_npa"].iloc[0] == pytest.approx(dynamic_pressure_npa(5, 400))


def test_add_features_uses_measured_helium_when_available():
    f = add_features(_frame(alpha=0.5))           # 10% helium
    assert f["pdyn_npa"].iloc[0] == pytest.approx(dynamic_pressure_npa(5, 400, alpha_ratio=0.1))


def test_add_features_clips_impossible_helium_ratios():
    f = add_features(_frame(alpha=50.0))          # 1000% helium: an instrument glitch
    assert f["pdyn_npa"].iloc[0] == pytest.approx(dynamic_pressure_npa(5, 400, alpha_ratio=0.3))


def test_add_features_does_not_modify_its_input():
    d = _frame()
    before = d.copy()
    add_features(d)
    pd.testing.assert_frame_equal(d, before)


# ---------------------------------------------------------------- the 3D model's physics (JavaScript)
def _shue_py(bz, dp):
    r0 = (10.22 + 1.29 * math.tanh(0.184 * (bz + 8.14))) * dp ** (-1 / 6.6)
    alpha = (0.58 - 0.007 * bz) * (1 + 0.024 * math.log(dp))
    return r0, alpha


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_3d_model_uses_the_same_physics_as_the_pipeline():
    """Run the physics block of docs/magnetosphere.js in Node and compare it with
    Shue et al. (1998) and with the Python pressure formula."""
    js = (ROOT / "docs/magnetosphere.js").read_text(encoding="utf-8")
    block = js.split("/* ---------- physics ---------- */", 1)[1].split("/* ----------", 1)[0]
    cases = [(-10, 1.0), (0, 2.0), (5, 4.0), (-5, 8.0)]
    script = block + f"\nconsole.log(JSON.stringify({{shue: {json.dumps(cases)}.map(([b, p]) => shue(b, p)), " \
                     f"p: pdyn(5, 400)}}));"
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", check=True).stdout)
    for (bz, dp), got in zip(cases, out["shue"]):
        r0, a = _shue_py(bz, dp)
        assert got["r0"] == pytest.approx(r0, rel=1e-12) and got["alpha"] == pytest.approx(a, rel=1e-12)
    assert out["p"] == pytest.approx(dynamic_pressure_npa(5, 400), rel=1e-3)


def test_shue_standoff_is_about_ten_earth_radii_in_typical_wind():
    # Shue et al. (1998): ~10-11 Re for Bz ~ 0 and 2 nPa; compressed below 8 Re in a strong storm
    assert 10 < _shue_py(0, 2)[0] < 11
    assert _shue_py(-20, 15)[0] < 7
