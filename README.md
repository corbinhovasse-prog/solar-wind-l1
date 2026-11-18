# Three spacecraft, one solar wind

**Live dashboard:** https://corbinhovasse-prog.github.io/solar-wind-l1/

A real-time data pipeline for the solar wind measured at the Sun–Earth L1 point, about 1.5 million km sunward of Earth.

Three spacecraft now report from L1 at the same time: NOAA's **SOLAR-1** (formerly SWFO-L1, operational since June 2026), NASA's **IMAP**, and NASA's **ACE**, which has been there since 1997. NOAA's real-time feed carries all three side by side, one row per spacecraft per minute. This project ingests that feed and turns it into answers:

- **What's coming, and how long until it arrives.** It computes physics quantities (ram pressure, Newell coupling) and each spacecraft's real warning time from its position.
- **Where every reading comes from.** It pulls spacecraft orbits from NASA JPL Horizons and maps them against Earth, the Moon and L1, to scale.
- **Why a minute was flagged.** Isolation Forest anomaly detection, with each event explained by the measurements that were most unusual and checked against what Kp did afterwards.
- **Whether the spacecraft agree.** Each one is scored against the consensus of the group, with an hour-by-hour agreement record.
- **How much of Earth's response the wind explains.** Readings are time-shifted to their arrival at Earth. Twenty-five years of NASA OMNI data measure the response lag and split Kp's variation into solar wind, memory and season.
- **How different Earth's wind may be from what L1 measures.** Spacecraft pairs at different separations show how fast the wind changes across space.

An interactive dashboard on GitHub Pages shows it all, from 24 hours back to 2000, including a 3D model of how a southward field lets energy into Earth's magnetic shield.

```
NOAA SWPC (live) ─┐
JPL Horizons ─────┼─► pipeline (GitHub Actions, every 30 min) ─► docs/data/latest.json ─► dashboard
NASA OMNI ────────┘        └─ archives on the `data` branch: 1-minute (30 days), hourly (forever), Kp, OMNI cache
NOAA SWPC ─► stream.producer ─► Kafka [rtsw.minute] ─► stream.consumer ─► Kafka [rtsw.alerts]
```

## Why three spacecraft matter

One instrument can't tell a real event apart from an instrument glitch. Three instruments looking at the same wind can. Disagreement between them carries information in two ways:

- **Calibration.** A steady bias between two spacecraft points to a calibration difference.
- **Timing.** A short burst of disagreement usually means a shock is sweeping across the spacecraft one after another. The pipeline estimates the minutes between them.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m pipeline.run          # pull live data, update archive + dashboard JSON
python -m pytest -q             # tests (offline, synthetic data)

# preview the dashboard
python -m http.server -d docs 8000    # open http://localhost:8000
```

No network? Generate realistic synthetic data and run on it offline:

```bash
python tools/make_sample_data.py sample_data
python -m pipeline.run --offline sample_data --out /tmp/out --history /tmp/hist
```

## Streaming with Kafka

```bash
docker compose up -d                                 # single-node Kafka (KRaft)
python -m stream.consumer                            # terminal 1: detector
python -m stream.producer                            # terminal 2: poll NOAA every 60 s
python -m stream.producer --replay sample_data       # or replay a shock in seconds
```

The consumer raises three kinds of alert. Each alert includes the estimated minutes until that wind reaches Earth.

- `SHOCK`: speed rises more than 100 km/s within 15 minutes.
- `SOUTHWARD`: Bz is below −10 nT while speed is above 450 km/s.
- `DISAGREE`: two spacecraft differ in speed by more than 15% in the same minute.

## Physics used

| Quantity | Formula | Why it matters |
|---|---|---|
| Dynamic pressure | P = m_p · n · v² (= 1.6726×10⁻⁶ · n · v² nPa) | Compresses the magnetosphere |
| Newell coupling | v^(4/3) · B_T^(2/3) · sin^(8/3)(θ/2) | Rate energy enters the magnetosphere; peaks for fast wind with southward field |
| Warning time | 1.5×10⁶ km / v | How long until the wind at L1 reaches Earth |

## Limits

- **Warning time:** the estimate assumes straight-line travel at the measured speed. Real structures tilt and evolve on the way to Earth.
- **Point measurements:** each spacecraft samples one point, and the wind that reaches Earth can differ.
- **Kp:** the index is global and 3-hourly, so the Kp correlation describes a trend and isn't a forecast.
- **Provisional data:** NOAA reprocesses real-time data later, and a flagged anomaly can be an instrument artifact.

## Data

Long-term context: [NASA OMNI](https://omniweb.gsfc.nasa.gov/) hourly data (dataset `OMNI2_H0_MRG1HR`) via the CDAWeb HAPI service, refreshed weekly.


Spacecraft positions: [NASA JPL Horizons](https://ssd.jpl.nasa.gov/horizons/) (SOLAR-1 is listed as SWFO-L1, ID -231; IMAP -43; ACE -92), converted to GSE coordinates.


[NOAA Space Weather Prediction Center](https://www.swpc.noaa.gov/) real-time solar wind (`/json/rtsw/rtsw_wind_1m.json`, `/json/rtsw/rtsw_mag_1m.json`) and the planetary K-index (`/products/noaa-planetary-k-index.json`). This is public U.S. government data.

## Credits

Built by Corbin Hovasse with an AI assistant.
