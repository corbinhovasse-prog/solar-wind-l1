"""The code paths that talk to NOAA, JPL and NASA, with the network replaced by fakes:
requests are well-formed, caches are respected, and outages degrade gracefully."""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from conftest import ROOT
from pipeline import ephemeris, fetch, omni

OMNI_ROW = "{t},3.6,-2.0,6.0,420,50,23,-10\n"


class FakeResponse:
    def __init__(self, text="", payload=None, status=200):
        self.text, self._payload, self.status_code = text, payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


# ---------------------------------------------------------------- NOAA SWPC
def test_live_fetch_requests_every_feed_with_a_timeout_and_user_agent(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return FakeResponse(payload=[{"time_tag": "2026-01-01T00:00:00"}])
    monkeypatch.setattr(fetch.requests, "get", fake_get)
    out = fetch.fetch_all()
    assert set(out) == set(fetch.URLS)
    assert all(u.startswith("https://services.swpc.noaa.gov/") for u, _, _ in calls)
    assert all(t and h and "User-Agent" in h for _, h, t in calls)


def test_a_noaa_error_stops_the_run_instead_of_publishing_empty_data(monkeypatch):
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: FakeResponse(status=503))
    with pytest.raises(requests.HTTPError):
        fetch.fetch_all()


# ---------------------------------------------------------------- JPL Horizons
def _horizons_text(x, y, z, start):
    rows = []
    for i in range(3):
        t = start + timedelta(hours=6 * i)
        rows.append(f"2461361.5, A.D. {t:%Y-%b-%d %H:%M:%S}.0000, {x}, {y}, {z},")
    return "$$SOE\n" + "\n".join(rows) + "\n$$EOE"


def test_positions_are_built_from_horizons_answers(monkeypatch):
    seen = []

    def fake_query(target, start, stop, step="1h"):
        seen.append(target)
        if target == ephemeris.SUN:
            v = (1.5e8, 0.0, 0.0)
        elif target in ephemeris.SPACECRAFT.values():
            v = (1.5e6, 2e5, -1e5)
        else:
            v = (3.8e5, 1e4, 0.0)
        return ephemeris.parse_vectors(_horizons_text(*v, start))
    monkeypatch.setattr(ephemeris, "query", fake_query)
    tracks, bodies = ephemeris.fetch_positions()
    assert set(tracks["source"]) == set(ephemeris.SPACECRAFT)
    assert tracks["x_gse"].iloc[0] == pytest.approx(1.5e6)
    assert set(bodies) == {"SUN", *ephemeris.BODIES}
    assert set(ephemeris.SPACECRAFT.values()) <= set(seen)


def test_a_horizons_outage_skips_positions_without_failing(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(ephemeris, "query", boom)
    tracks, bodies = ephemeris.fetch_positions()
    assert tracks.empty and bodies == {}


def test_horizons_query_asks_for_earth_centred_ecliptic_vectors(monkeypatch):
    got = {}

    def fake_get(url, params=None, timeout=None):
        got.update(params=params, timeout=timeout)
        return FakeResponse(text=_horizons_text(1, 2, 3, datetime(2026, 11, 17, tzinfo=timezone.utc)))
    monkeypatch.setattr(ephemeris.requests, "get", fake_get)
    ephemeris.query("-231", datetime(2026, 11, 17), datetime(2026, 11, 18), "6h")
    p = got["params"]
    assert p["CENTER"] == "'500@399'" and p["REF_PLANE"] == "'ECLIPTIC'" and p["EPHEM_TYPE"] == "VECTORS"
    assert got["timeout"]


# ---------------------------------------------------------------- NASA OMNI cache
def _fake_download(rows):
    def dl(start, stop):
        t = pd.date_range(start, stop, freq="1h", tz="UTC")[:rows]
        return omni.parse("".join(OMNI_ROW.format(t=x.strftime("%Y-%m-%dT%H:%M:%S.000Z")) for x in t))
    return dl


def test_omni_is_downloaded_once_then_served_from_cache(tmp_path, monkeypatch):
    calls = []
    dl = _fake_download(48)
    monkeypatch.setattr(omni, "_download", lambda a, b: calls.append(a) or dl(a, b))
    first = omni.load_omni(tmp_path)
    second = omni.load_omni(tmp_path)
    assert len(calls) == 1 and len(first) == len(second) == 48
    assert json.loads((tmp_path / "omni_meta.json").read_text(encoding="utf-8"))["fetched_at"]


def test_stale_omni_cache_refreshes_only_the_last_90_days(tmp_path, monkeypatch):
    monkeypatch.setattr(omni, "_download", _fake_download(24 * 200))
    cached_end = omni.load_omni(tmp_path)["time_tag"].max()
    old = datetime.now(timezone.utc) - timedelta(days=8)
    (tmp_path / "omni_meta.json").write_text(json.dumps({"fetched_at": old.isoformat()}), encoding="utf-8")
    starts = []
    dl = _fake_download(24)
    monkeypatch.setattr(omni, "_download", lambda a, b: starts.append(a) or dl(a, b))
    df = omni.load_omni(tmp_path)
    assert len(starts) == 1 and not df["time_tag"].duplicated().any()
    assert cached_end - pd.Timestamp(starts[0]) == pd.Timedelta(days=90)    # OMNI revises recent data
    assert df["time_tag"].min() == pd.Timestamp("2000-01-01", tz="UTC")       # older data kept


def test_omni_outage_falls_back_to_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(omni, "_download", _fake_download(24))
    omni.load_omni(tmp_path)
    (tmp_path / "omni_meta.json").write_text(json.dumps({"fetched_at": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")

    def boom(*a):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(omni, "_download", boom)
    assert len(omni.load_omni(tmp_path)) == 24


def test_omni_download_asks_cdaweb_one_year_at_a_time(monkeypatch):
    asked = []

    def fake_get(url, params=None, timeout=None):
        asked.append(params)
        return FakeResponse(text=OMNI_ROW.format(t=params["time.min"].replace("Z", ".000Z")))
    monkeypatch.setattr(omni.requests, "get", fake_get)
    df = omni._download(datetime(2024, 6, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert len(asked) == 3 and all(a["id"] == "OMNI2_H0_MRG1HR" for a in asked)
    assert len(df) == 3


# ---------------------------------------------------------------- command line
def test_command_line_entry_point_runs_offline(tmp_path, sample_dir):
    r = subprocess.run([sys.executable, "-m", "pipeline.run", "--offline", str(sample_dir),
                        "--out", str(tmp_path / "out"), "--history", str(tmp_path / "h.csv.gz")],
                       cwd=ROOT, capture_output=True, encoding="utf-8", check=True)
    assert "plasma from" in r.stdout and (tmp_path / "out/latest.json").exists()
    assert (tmp_path / "rtsw_1m.csv.gz").exists()          # a .gz path means "use its folder"
