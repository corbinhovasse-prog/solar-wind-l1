"""Generate synthetic RTSW-format JSON for offline testing (no network needed).

Three spacecraft see the same wind with small calibration offsets and timing
lags; a shock arrives partway through. Output mirrors SWPC's field names.
    python tools/make_sample_data.py sample_data
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data")
out.mkdir(parents=True, exist_ok=True)

end = pd.Timestamp.now(tz="UTC").floor("min")
t = pd.date_range(end - pd.Timedelta(days=2), end, freq="1min")
n = len(t)
shock = n * 2 // 3
speed = 380 + np.cumsum(rng.normal(0, 1.5, n)).clip(-60, 60)
speed[shock:] += 220
dens = np.abs(5 + np.cumsum(rng.normal(0, 0.05, n)).clip(-3, 3))
dens[shock:shock + 240] *= 3.5
bz = np.convolve(rng.normal(0, 2.5, n), np.ones(15) / 15, "same") * 2
bz[shock + 30:shock + 400] -= 12
by = np.convolve(rng.normal(0, 2.5, n), np.ones(15) / 15, "same") * 2
bx = rng.normal(-3, 1, n)
# a shock compresses the field along with the plasma
bx[shock:shock + 240] *= 2
by[shock:shock + 240] *= 2
bt = np.sqrt(bx**2 + by**2 + bz**2)

specs = {"SOLAR1": (0, 1.00, 0.0), "IMAP": (4, 1.02, 0.2), "ACE": (-3, 0.97, -0.3)}
wind, mag = [], []
for src, (lag, gain, off) in specs.items():
    for i in range(n):
        j = min(max(i - lag, 0), n - 1)
        if rng.random() < 0.03:  # data gaps
            continue
        tag = t[i].strftime("%Y-%m-%dT%H:%M:%S")
        active = src == "SOLAR1"
        # mimic the live feed: the active spacecraft's plasma values are often null
        no_plasma = src == "SOLAR1" and (i >= n - 180 or i % 5)
        wind.append({"time_tag": tag, "active": active, "source": src,
                     "proton_speed": None if no_plasma else round(float(speed[j] * gain), 2),
                     "proton_density": None if no_plasma else round(float(dens[j] * gain), 2),
                     "proton_temperature": None if no_plasma else 60000, "overall_quality": 0})
        mag.append({"time_tag": tag, "active": active, "source": src,
                    "bt": round(float(bt[j]), 2), "bx_gsm": round(float(bx[j]), 2),
                    "by_gsm": round(float(by[j] + off), 2), "bz_gsm": round(float(bz[j] + off), 2),
                    "overall_quality": 0})
wind.reverse(); mag.reverse()  # SWPC lists newest first

kp_t = pd.date_range(t[0].floor("3h"), end, freq="3h")
kp = [{"time_tag": x.strftime("%Y-%m-%dT%H:%M:%S"),
       "Kp": float(6.0 if x >= t[shock] - pd.Timedelta(hours=1) and x < t[shock] + pd.Timedelta(hours=8) else rng.choice([1, 1.33, 2, 2.67])),
       "a_running": 7, "station_count": 8} for x in kp_t]

# 6-hourly GSE positions (km) over ~6 months: one full halo loop each.
# X offsets match the planted timing lags at ~400 km/s (4 min = 96,000 km).
pos = []
hours = pd.date_range(end.floor("6h") - pd.Timedelta(days=180), end.floor("6h"), freq="6h")
for src, (x0, ay, az, ph) in {"SOLAR1": (1.452e6, 3.0e5, 1.0e5, 3.4), "IMAP": (1.356e6, 2.4e5, 0.6e5, 3.6),
                               "ACE": (1.524e6, 2.7e5, 1.5e5, 0.4)}.items():
    for k, h in enumerate(hours):
        w = 2 * np.pi * k / (178 * 4) + ph  # ~178-day halo period
        pos.append({"time_tag": h.strftime("%Y-%m-%dT%H:%M:%S"), "source": src,
                    "x_gse": x0 + 4e4 * np.cos(2 * w), "y_gse": ay * np.cos(w), "z_gse": az * np.sin(w)})
bodies = {"SUN": {"x_gse": 1.478e8, "y_gse": 0, "z_gse": 0},
          "MOON": {"x_gse": -2.1e5, "y_gse": 3.2e5, "z_gse": 1.5e4},
          "VENUS": {"x_gse": 1.05e8, "y_gse": 6.0e7, "z_gse": 2.0e6},
          "MARS": {"x_gse": -1.8e8, "y_gse": 1.9e8, "z_gse": 3.0e6}}
positions = {"tracks": pos, "bodies": bodies}

# synthetic OMNI: hourly since 2023 with a solar-cycle-ish drift, equinox bumps and storms
oh = pd.date_range(pd.Timestamp("2023-01-01", tz="UTC"), end.floor("h") - pd.Timedelta(days=30), freq="1h")
m = len(oh)
doy = oh.day_of_year.to_numpy()
eq = np.cos(4 * np.pi * (doy - 80) / 365.25)
ov = 420 + 60 * np.sin(np.arange(m) / (27 * 24) * 2 * np.pi) + np.convolve(rng.normal(0, 40, m), np.ones(12) / 12, "same")
oby = np.convolve(rng.normal(0, 3, m), np.ones(4) / 4, "same")
obz = np.convolve(rng.normal(0, 3, m), np.ones(4) / 4, "same") - 0.4 * eq
on = np.abs(6 + np.convolve(rng.normal(0, 2, m), np.ones(6) / 6, "same"))
th = np.arctan2(oby, obz)
coup = ov ** (4 / 3) * np.sqrt(oby ** 2 + obz ** 2) ** (2 / 3) * np.abs(np.sin(th / 2)) ** (8 / 3) / 1e3
c3 = pd.Series(coup).rolling(3, min_periods=1).mean().shift(1).bfill().to_numpy()
okp = np.clip(0.6 + 0.9 * np.log1p(c3) + 0.3 * eq + rng.normal(0, 0.5, m), 0, 9)
okp = pd.Series(okp).groupby(np.arange(m) // 3).transform("mean").round(1).to_numpy()
lines = [f"{t:%Y-%m-%dT%H:%M:%S.000Z},{oby[i]:.1f},{obz[i]:.1f},{on[i]:.1f},{ov[i]:.0f},{80 + 40 * np.sin(i / m * np.pi):.0f},{okp[i] * 10:.0f},{-10 - 5 * okp[i]:.0f}"
         for i, t in enumerate(oh)]
(out / "omni.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

for name, data in {"wind": wind, "mag": mag, "kp": kp, "positions": positions}.items():
    (out / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
print(f"wrote {len(wind)} wind, {len(mag)} mag, {len(kp)} kp rows to {out}/")
