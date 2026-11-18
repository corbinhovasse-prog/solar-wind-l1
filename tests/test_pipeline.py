import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from pipeline.features import dynamic_pressure_npa, lead_time_minutes, newell_coupling
from pipeline.fetch import to_frame
from stream.consumer import Detector

ROOT = Path(__file__).resolve().parents[1]


def test_dynamic_pressure_typical_wind():
    # 5 protons/cc at 400 km/s is ~1.3 nPa, the textbook quiet-wind value
    assert abs(dynamic_pressure_npa(5, 400) - 1.338) < 0.01


def test_coupling_zero_for_northward_field():
    assert newell_coupling(400, 0.0, 5.0) < 1e-9
    assert newell_coupling(400, 0.0, -5.0) > newell_coupling(400, 3.0, 4.0)


def test_lead_time_at_400_kms_is_about_an_hour():
    assert 60 < lead_time_minutes(400) < 65


def test_both_swpc_layouts_parse():
    a = to_frame([{"time_tag": "2026-01-01T00:00:00", "Kp": 2.0}])
    b = to_frame([["time_tag", "Kp"], ["2026-01-01T00:00:00", "2.0"]])
    assert list(a.columns) == list(b.columns) == ["time_tag", "Kp"]


def test_detector_flags_shock():
    d = Detector()
    alerts = []
    for i in range(12):
        v = 400 if i < 8 else 560
        alerts += d.evaluate({"source": "SOLAR1", "time_tag": f"2026-01-01T00:{i:02d}:00",
                              "proton_speed": v, "bz_gsm": 1.0, "by_gsm": 0.0})
    assert any(a["type"] == "SHOCK" for a in alerts)
    assert sum(a["type"] == "SHOCK" for a in alerts) == 1  # cooldown


def test_end_to_end_offline(tmp_path):
    sample = tmp_path / "sample"
    subprocess.run([sys.executable, str(ROOT / "tools/make_sample_data.py"), str(sample)], check=True)
    subprocess.run([sys.executable, "-m", "pipeline.run", "--offline", str(sample),
                    "--out", str(tmp_path / "out"), "--history", str(tmp_path / "h.csv.gz")],
                   check=True, cwd=ROOT)
    r = json.loads((tmp_path / "out/latest.json").read_text())
    assert set(r["history"]["sources"]) == {"ACE", "IMAP", "SOLAR1"}
    assert len(r["anomalies"]) >= 1
    lags = {c["pair"]: c["bz_best_lag_min"] for c in r["cross_spacecraft"]}
    assert abs(lags["IMAP vs SOLAR1"]) == 4  # recovers the lag planted in the sample data


def test_latest_uses_spacecraft_that_has_plasma(tmp_path):
    # sample data leaves the active spacecraft (SOLAR1) without plasma for the last 3 hours,
    # just like the live feed often does; speed must still come through from another one
    sample = tmp_path / "sample"
    subprocess.run([sys.executable, str(ROOT / "tools/make_sample_data.py"), str(sample)], check=True)
    subprocess.run([sys.executable, "-m", "pipeline.run", "--offline", str(sample),
                    "--out", str(tmp_path / "out"), "--history", str(tmp_path / "h.csv.gz")],
                   check=True, cwd=ROOT)
    L = json.loads((tmp_path / "out/latest.json").read_text())["latest"]
    assert L["speed"] is not None and L["lead_time_min"] is not None
    assert L["plasma_source"] != "SOLAR1" and L["mag_source"] == "SOLAR1"


def test_gse_conversion_on_real_horizons_numbers():
    # SOLAR-1 and the Sun relative to Earth, 2026-11-17 00:00 TDB, from JPL Horizons
    from pipeline.ephemeris import parse_vectors, to_gse
    row = "2461361.500000000, A.D. 2026-Nov-17 00:00:00.0000, {}, {}, {},\n"
    sun = parse_vectors("$$SOE\n" + row.format(-8.638147671941963e07, -1.200917024777759e08, 7.915319478347898e03) + "$$EOE")
    sc = parse_vectors("$$SOE\n" + row.format(-1.092321430902997e06, -1.003392401594134e06, -1.014824458381130e05) + "$$EOE")
    g = to_gse(sc, sun).iloc[0]
    assert 1.4e6 < g.x_gse < 1.6e6          # about 1.5 million km sunward: at L1
    assert abs(g.y_gse) < 5e5 and abs(g.z_gse) < 5e5  # within a halo orbit of the Sun-Earth line


def test_omni_fill_values_become_missing():
    from pipeline.omni import parse
    df = parse("2026-10-01T00:30:00.000Z,3.6,0.6,9.1,284,23,33,4\n2026-10-01T01:30:00.000Z,999.9,999.9,999.9,9999,999,99,99999\n")
    assert df.loc[0, "kp"] == 3.3 and df.loc[0, "v"] == 284
    assert df.iloc[1][["by", "bz", "n", "v", "kp", "dst"]].isna().all()


def test_dashboard_sections_present(tmp_path):
    sample = tmp_path / "sample"
    subprocess.run([sys.executable, str(ROOT / "tools/make_sample_data.py"), str(sample)], check=True)
    subprocess.run([sys.executable, "-m", "pipeline.run", "--offline", str(sample),
                    "--out", str(tmp_path / "out"), "--history", str(tmp_path / "hist")], check=True, cwd=ROOT)
    r = json.loads((tmp_path / "out/latest.json").read_text())
    assert r["longterm"]["available"] and r["longterm"]["kp_drivers"]["best_lag_h"] >= 0
    assert r["agreement"]["spacecraft"]["SOLAR1"]["verdict"] == "in step"
    assert all("why" in e and e["why"] for e in r["anomalies"])
    assert r["positions"]["latest"]["SOLAR1"]["dist_km"]["SUN"] > 1e8


def test_calibration_offset_is_flagged_not_called_disagreement(tmp_path):
    # ACE reads density 38% low but follows every change: that's calibration, not disagreement
    sample = tmp_path / "sample"
    subprocess.run([sys.executable, str(ROOT / "tools/make_sample_data.py"), str(sample)], check=True)
    w = json.loads((sample / "wind.json").read_text())
    for r in w:
        if r["source"] == "ACE" and r["proton_density"] is not None:
            r["proton_density"] = round(r["proton_density"] * 0.62, 2)
    (sample / "wind.json").write_text(json.dumps(w))
    subprocess.run([sys.executable, "-m", "pipeline.run", "--offline", str(sample),
                    "--out", str(tmp_path / "out"), "--history", str(tmp_path / "hist")], check=True, cwd=ROOT)
    ace = json.loads((tmp_path / "out/latest.json").read_text())["agreement"]["spacecraft"]["ACE"]
    assert ace["verdict"] == "in step"
    assert any("density" in f for f in ace["calibration_flags"])


def test_switching_spacecraft_does_not_create_fake_jumps(tmp_path):
    # IMAP reads density 2.1x higher (like the live feed); SOLAR-1 plasma is intermittent,
    # so the merged series switches between them. Calibration must remove the offset.
    sample = tmp_path / "sample"
    subprocess.run([sys.executable, str(ROOT / "tools/make_sample_data.py"), str(sample)], check=True)
    w = json.loads((sample / "wind.json").read_text())
    for r in w:
        if r["source"] == "IMAP" and r["proton_density"] is not None:
            r["proton_density"] = round(r["proton_density"] * 2.1, 2)
    (sample / "wind.json").write_text(json.dumps(w))
    subprocess.run([sys.executable, "-m", "pipeline.run", "--offline", str(sample),
                    "--out", str(tmp_path / "out"), "--history", str(tmp_path / "hist")], check=True, cwd=ROOT)
    r = json.loads((tmp_path / "out/latest.json").read_text())
    f = r["calibration"]["factors"]["IMAP"]["proton_density"]
    assert 0.43 < f < 0.53
    assert not any("density" in " ".join(e["why"]) and not e["significant"] and e["minutes"] < 5 and e["type"] == "Density jump"
                   for e in r["anomalies"])
