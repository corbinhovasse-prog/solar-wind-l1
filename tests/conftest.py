"""Shared fixtures and synthetic-data builders.

Synthetic data is used where the right answer must be known exactly (a planted lag, a
planted calibration factor, a planted event). The end-to-end fixture runs the real
pipeline once on the bundled sample feeds and is shared by every output test."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
T0 = pd.Timestamp("2026-11-10T00:00:00", tz="UTC")


def quiet_week(seed: int = 0, days: int = 7, storm_day: int | None = None) -> pd.DataFrame:
    """A realistic quiet stretch of 1-minute solar wind: ~380 km/s, ~5 /cc, Bz wandering
    within a few nT. Optionally a large storm on `storm_day` to raise the week's baseline."""
    rng = np.random.default_rng(seed)
    t = pd.date_range(T0, periods=days * 1440, freq="1min", tz="UTC", name="time_tag")
    n = len(t)
    v = 380 + np.cumsum(rng.normal(0, 0.8, n)).clip(-40, 40)
    bz = np.convolve(rng.normal(0, 2, n), np.ones(20) / 20, "same") * 2
    by = np.convolve(rng.normal(0, 2, n), np.ones(20) / 20, "same") * 2
    dens = np.abs(5 + np.convolve(rng.normal(0, 0.3, n), np.ones(30) / 30, "same"))
    if storm_day is not None:
        i = storm_day * 1440
        v[i:i + 600] += 300; bz[i:i + 600] -= 20; dens[i:i + 120] *= 4
    return pd.DataFrame({"proton_speed": v, "proton_density": dens, "bz_gsm": bz, "by_gsm": by,
                         "bt": np.sqrt(bz ** 2 + by ** 2 + 9)}, index=t)


def three_spacecraft(seed: int = 0, hours: int = 96, noise: float = 0.5,
                     lags: dict | None = None, scale: dict | None = None) -> pd.DataFrame:
    """Three spacecraft measuring the same wind with independent noise.
    lags: minutes by which each spacecraft sees the wind EARLIER than SOLAR1.
    scale: multiplicative calibration factor on density per spacecraft."""
    lags, scale = lags or {}, scale or {}
    rng = np.random.default_rng(seed)
    pad = 60
    t = pd.date_range(T0, periods=hours * 60, freq="1min", tz="UTC")
    N = len(t) + 2 * pad
    base_v = 400 + np.cumsum(rng.normal(0, 1.0, N))
    base_n = np.abs(5 + np.cumsum(rng.normal(0, 0.03, N)))
    base_bz = np.convolve(rng.normal(0, 3, N), np.ones(30) / 30, "same") * 3
    frames = []
    for src in ["SOLAR1", "IMAP", "ACE"]:
        k = pad + lags.get(src, 0)                  # earlier sighting = later sample of the base
        sl = slice(k, k + len(t))
        frames.append(pd.DataFrame({
            "time_tag": t, "source": src, "active": src == "SOLAR1",
            "proton_speed": base_v[sl] + rng.normal(0, noise, len(t)),
            "proton_density": base_n[sl] * (1 + rng.normal(0, 0.02, len(t))) * scale.get(src, 1.0),
            "bz_gsm": base_bz[sl] + rng.normal(0, noise, len(t)),
            "by_gsm": rng.normal(0, 2, len(t)), "bt": 6 + rng.normal(0, 0.2, len(t)),
        }))
    return pd.concat(frames, ignore_index=True)


def run_pipeline(sample: Path, out: Path, hist: Path) -> dict:
    """Run the pipeline in-process (so coverage sees it) and return latest.json as the
    dashboard would read it."""
    from pipeline.run import run
    run(str(sample), out, hist)
    return json.loads((out / "latest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("sample")
    subprocess.run([sys.executable, str(ROOT / "tools/make_sample_data.py"), str(d)], check=True,
                   capture_output=True)
    return d


@pytest.fixture
def fresh_sample(tmp_path, sample_dir) -> Path:
    """A private copy of the sample feeds that a test may modify."""
    d = tmp_path / "sample"
    d.mkdir()
    for f in sample_dir.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    return d


@pytest.fixture(scope="session")
def pipeline_output(tmp_path_factory, sample_dir) -> tuple[dict, Path]:
    """The pipeline run once, end to end, on the sample feeds: (latest.json, output text path)."""
    base = tmp_path_factory.mktemp("run")
    r = run_pipeline(sample_dir, base / "out", base / "hist")
    return r, base / "out" / "latest.json"
