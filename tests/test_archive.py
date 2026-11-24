"""The archive is the project's memory: the live feed covers only about a day, so every
run merges into it. These tests pin down the merge rules and the safety guards."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import ROOT, T0
from pipeline.run import KP_DAYS, MINUTE_DAYS, _read, update_archives

KP = pd.DataFrame({"time_tag": [T0], "kp": [2.0]})


def _rows(n, start=T0, src="SOLAR1", **vals):
    vals = vals or {"proton_speed": 400.0, "bz_gsm": -1.0, "by_gsm": 1.0}
    return pd.DataFrame({"time_tag": pd.date_range(start, periods=n, freq="1min"), "source": src, **vals})


def test_rerunning_with_the_same_data_changes_nothing(tmp_path):
    feed = _rows(300)
    m1, _, h1 = update_archives(feed, KP, tmp_path)
    m2, _, h2 = update_archives(feed, KP, tmp_path)
    pd.testing.assert_frame_equal(m1, m2)
    pd.testing.assert_frame_equal(h1.reset_index(drop=True), h2.reset_index(drop=True))


def test_overlapping_pulls_are_stitched_without_duplicates(tmp_path):
    update_archives(_rows(120), KP, tmp_path)
    m, _, _ = update_archives(_rows(120, start=T0 + pd.Timedelta(minutes=60)), KP, tmp_path)
    assert len(m) == 180
    assert not m.duplicated(["time_tag", "source"]).any()
    assert m["time_tag"].is_monotonic_increasing


def test_newest_value_wins_for_a_minute_in_both_pulls(tmp_path):
    update_archives(_rows(10, proton_speed=400.0, bz_gsm=-1.0), KP, tmp_path)
    m, _, _ = update_archives(_rows(10, proton_speed=500.0, bz_gsm=-1.0), KP, tmp_path)   # NOAA revised it
    assert (m["proton_speed"] == 500.0).all()


def test_plasma_from_one_pull_and_field_from_another_are_combined(tmp_path):
    update_archives(_rows(10, proton_speed=400.0), KP, tmp_path)
    m, _, _ = update_archives(_rows(10, bz_gsm=-3.0), KP, tmp_path)
    assert (m["proton_speed"] == 400.0).all() and (m["bz_gsm"] == -3.0).all()


def test_spacecraft_are_kept_separate(tmp_path):
    feed = pd.concat([_rows(10, src="SOLAR1"), _rows(10, src="ACE")])
    m, _, h = update_archives(feed, KP, tmp_path)
    assert len(m) == 20 and set(h["source"]) == {"SOLAR1", "ACE"}


def test_archive_with_off_minute_stamps_is_repaired_on_load(tmp_path):
    rows = []
    for k in range(30):
        t = T0 + pd.Timedelta(minutes=k)
        rows.append({"time_tag": t + pd.Timedelta(seconds=1), "source": "IMAP", "proton_speed": 400.0})
        rows.append({"time_tag": t + pd.Timedelta(seconds=3), "source": "IMAP", "bz_gsm": -1.0, "by_gsm": 1.0})
    pd.DataFrame(rows).to_csv(tmp_path / "rtsw_1m.csv.gz", index=False, compression="gzip")
    m, _, _ = update_archives(_rows(1, start=T0 + pd.Timedelta(minutes=30), src="IMAP"), KP, tmp_path)
    assert len(m) == 31
    assert (m["proton_speed"].notna() & m["bz_gsm"].notna()).all()


def test_minute_archive_keeps_only_the_last_30_days(tmp_path):
    update_archives(_rows(60), KP, tmp_path)
    later = T0 + pd.Timedelta(days=MINUTE_DAYS + 1)
    m, _, _ = update_archives(_rows(60, start=later), KP, tmp_path, allow_shrink=True)
    assert m["time_tag"].min() >= later - pd.Timedelta(days=MINUTE_DAYS)


def test_hourly_archive_is_kept_forever_and_is_the_hourly_mean(tmp_path):
    feed = _rows(120, proton_speed=np.r_[np.full(60, 400.0), np.full(60, 500.0)], bz_gsm=-1.0, by_gsm=1.0)
    update_archives(feed, KP, tmp_path)
    later = T0 + pd.Timedelta(days=MINUTE_DAYS + 5)
    _, _, h = update_archives(_rows(60, start=later), KP, tmp_path, allow_shrink=True)
    first = h[h["time_tag"] < T0 + pd.Timedelta(hours=2)]
    assert list(first["proton_speed"]) == [400.0, 500.0]       # still there after the 30-day trim
    assert {"by_gsm", "bz_gsm"} <= set(h.columns)              # enough to rebuild coupling later


def test_kp_history_is_deduplicated_and_trimmed(tmp_path):
    k1 = pd.DataFrame({"time_tag": [T0, T0 + pd.Timedelta(hours=3)], "kp": [2.0, 3.0]})
    k2 = pd.DataFrame({"time_tag": [T0 + pd.Timedelta(hours=3), T0 + pd.Timedelta(days=KP_DAYS + 10)],
                       "kp": [3.33, 1.0]})
    update_archives(_rows(10), k1, tmp_path)
    _, k, _ = update_archives(_rows(10), k2, tmp_path)
    assert not k["time_tag"].duplicated().any()
    assert T0 not in set(k["time_tag"])                        # older than 2 years: trimmed


def test_refuses_to_overwrite_a_large_archive_with_a_tiny_one(tmp_path):
    big = _rows(9000)
    big.to_csv(tmp_path / "rtsw_1m.csv.gz", index=False, compression="gzip")
    jumped = _rows(10, start=T0 + pd.Timedelta(days=40))    # e.g. a bad clock pushes the 30-day trim
    with pytest.raises(RuntimeError):
        update_archives(jumped, KP, tmp_path)
    assert len(_read(tmp_path / "rtsw_1m.csv.gz")) == 9000    # untouched


def test_shrink_guard_can_be_overridden_deliberately(tmp_path):
    _rows(9000).to_csv(tmp_path / "rtsw_1m.csv.gz", index=False, compression="gzip")
    m, _, _ = update_archives(_rows(10, start=T0 + pd.Timedelta(days=40)), KP, tmp_path, allow_shrink=True)
    assert len(m) == 10


def test_small_archives_can_grow_from_nothing(tmp_path):
    m, _, _ = update_archives(_rows(5), KP, tmp_path)
    assert len(m) == 5 and (tmp_path / "rtsw_1m.csv.gz").exists()


def test_workflow_fails_instead_of_starting_over_when_the_restore_fails():
    wf = (ROOT / ".github/workflows/update.yml").read_text(encoding="utf-8")
    assert "git ls-remote --exit-code --heads origin data" in wf
    restore = wf.split("Restore archives", 1)[1].split("- name:", 1)[0]
    assert restore.count("exit 1") >= 2           # failed restore AND unreachable repo both stop the run
    assert "first run" in restore
