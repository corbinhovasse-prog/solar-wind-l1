"""The whole pipeline run on bundled sample feeds (no network), and the contract between
its output (latest.json) and the dashboard that reads it."""
import json
import math
import re
from datetime import datetime

import pytest

from conftest import ROOT, run_pipeline


def _strict(text):
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=bad)


# ---------------------------------------------------------------- output is valid and complete
def test_output_is_strict_json_without_nan_or_infinity(pipeline_output):
    _, path = pipeline_output
    _strict(path.read_text(encoding="utf-8"))          # browsers' JSON.parse rejects NaN; Python would not


def test_output_has_every_section_the_dashboard_reads(pipeline_output):
    r, _ = pipeline_output
    js = (ROOT / "docs/app.js").read_text(encoding="utf-8") + (ROOT / "docs/magnetosphere.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\bD\.([a-z_0-9]+)", js))
    assert used and used <= set(r), f"dashboard reads missing keys: {used - set(r)}"


def test_all_timestamps_are_utc_iso8601(pipeline_output):
    r, _ = pipeline_output
    stamps = [r["generated_at"], r["latest"]["time"], *r["series"]["fine"]["active"]["t"][:5],
              *(e["start"] for e in r["anomalies"]), *(e["arrival"] for e in r["anomalies"])]
    for s in stamps:
        assert datetime.fromisoformat(s).utcoffset().total_seconds() == 0


def test_every_chart_series_lines_up_with_its_time_axis(pipeline_output):
    r, _ = pipeline_output
    for res in ("fine", "hourly"):
        blocks = [r["series"][res]["active"], *r["series"][res]["per_source"].values()]
        for b in blocks:
            assert all(len(v) == len(b["t"]) for k, v in b.items() if k != "t" and v)
    kp = r["series"]["kp"]
    assert len(kp["t"]) == len(kp["kp"]) > 0


def test_headline_values_are_physical(pipeline_output):
    L = pipeline_output[0]["latest"]
    assert 200 < L["speed"] < 1200 and 0 < L["density"] < 100 and -60 < L["bz"] < 60
    assert 20 < L["lead_time_min"] < 120 and 0 <= L["kp"] <= 9 and L["pdyn"] > 0


# ---------------------------------------------------------------- the planted answers come back
def test_all_three_spacecraft_are_ingested(pipeline_output):
    assert set(pipeline_output[0]["history"]["sources"]) == {"ACE", "IMAP", "SOLAR1"}


def test_the_planted_shock_is_found_with_what_earth_did_next(pipeline_output):
    ev = [e for e in pipeline_output[0]["anomalies"] if e["significant"]]
    shock = [e for e in ev if e["type"] == "Shock"]
    assert len(shock) == 1
    assert shock[0]["kp_before"] is not None and "kp_after_complete" in shock[0]
    assert all(e["why"] for e in ev)


def test_the_planted_timing_offset_is_recovered(pipeline_output):
    lags = {c["pair"]: c["bz_best_lag_min"] for c in pipeline_output[0]["cross_spacecraft"]}
    assert abs(lags["IMAP vs SOLAR1"]) == 4


def test_plasma_comes_from_whichever_spacecraft_has_it(pipeline_output):
    # SOLAR1 has no plasma for the last 3 hours of the sample, as often happens live
    L = pipeline_output[0]["latest"]
    assert L["speed"] is not None and L["plasma_source"] != "SOLAR1" and L["mag_source"] == "SOLAR1"


def test_spacecraft_agree_and_positions_are_at_l1(pipeline_output):
    r, _ = pipeline_output
    assert all(v["verdict"] == "in step" for v in r["agreement"]["spacecraft"].values())
    for p in r["positions"]["latest"].values():
        assert 1.2e6 < p["x_km"] < 1.8e6 and p["dist_km"]["SUN"] > 1.4e8


def test_long_term_context_is_built(pipeline_output):
    lt = pipeline_output[0]["longterm"]
    assert lt["available"] and lt["kp_drivers"]["best_lag_h"] >= 0
    assert len(lt["by_month"]["kp_mean"]) == 12


# ---------------------------------------------------------------- variations of the input
def test_a_calibration_offset_is_flagged_not_called_disagreement(tmp_path, fresh_sample):
    w = json.loads((fresh_sample / "wind.json").read_text(encoding="utf-8"))
    for rec in w:
        if rec["source"] == "ACE" and rec["proton_density"] is not None:
            rec["proton_density"] = round(rec["proton_density"] * 0.62, 2)
    (fresh_sample / "wind.json").write_text(json.dumps(w), encoding="utf-8")
    ace = run_pipeline(fresh_sample, tmp_path / "out", tmp_path / "hist")["agreement"]["spacecraft"]["ACE"]
    assert ace["verdict"] == "in step"
    assert any("density" in f for f in ace["calibration_flags"])


def test_switching_spacecraft_does_not_create_fake_density_jumps(tmp_path, fresh_sample):
    w = json.loads((fresh_sample / "wind.json").read_text(encoding="utf-8"))
    for rec in w:
        if rec["source"] == "IMAP" and rec["proton_density"] is not None:
            rec["proton_density"] = round(rec["proton_density"] * 2.1, 2)
    (fresh_sample / "wind.json").write_text(json.dumps(w), encoding="utf-8")
    r = run_pipeline(fresh_sample, tmp_path / "out", tmp_path / "hist")
    assert 0.43 < r["calibration"]["factors"]["IMAP"]["proton_density"] < 0.53
    assert [e["type"] for e in r["anomalies"] if e["significant"]] == ["Shock"]


def test_a_second_run_extends_the_archive_instead_of_replacing_it(tmp_path, sample_dir):
    r1 = run_pipeline(sample_dir, tmp_path / "out", tmp_path / "hist")
    r2 = run_pipeline(sample_dir, tmp_path / "out", tmp_path / "hist")
    assert r2["history"]["rows"] == r1["history"]["rows"]           # same data: nothing duplicated
    assert r2["anomalies"] == r1["anomalies"]


def test_the_pipeline_survives_without_positions(tmp_path, fresh_sample):
    (fresh_sample / "positions.json").unlink()
    r = run_pipeline(fresh_sample, tmp_path / "out", tmp_path / "hist")
    assert r["positions"]["available"] is False and r["latest"]["speed"] is not None
