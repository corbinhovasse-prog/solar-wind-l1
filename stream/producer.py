"""Publish each new spacecraft-minute to Kafka as it appears in NOAA's feed.

    python -m stream.producer                          # poll NOAA every 60 s
    python -m stream.producer --replay sample_data     # replay saved JSON fast (demo mode)

Topic `rtsw.minute`, keyed by spacecraft so each one's minutes stay ordered
within a partition.
"""
from __future__ import annotations

import argparse
import json
import time

from confluent_kafka import Producer

from pipeline.fetch import URLS, get_json, merge_wind_mag

TOPIC = "rtsw.minute"


def records(wind_raw, mag_raw):
    df = merge_wind_mag(wind_raw, mag_raw)
    for row in df.to_dict(orient="records"):
        row["time_tag"] = row["time_tag"].isoformat()
        yield {k: (None if v != v else v) for k, v in row.items()}  # NaN -> None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bootstrap", default="localhost:9092")
    p.add_argument("--interval", type=int, default=60, help="seconds between NOAA polls")
    p.add_argument("--replay", help="folder with wind.json and mag.json to replay")
    p.add_argument("--rate", type=float, default=50, help="replay messages per second")
    a = p.parse_args()

    prod = Producer({"bootstrap.servers": a.bootstrap, "linger.ms": 50})
    send = lambda r: prod.produce(TOPIC, key=r["source"], value=json.dumps(r))

    if a.replay:
        wind = json.load(open(f"{a.replay}/wind.json"))
        mag = json.load(open(f"{a.replay}/mag.json"))
        for i, r in enumerate(records(wind, mag)):
            send(r)
            prod.poll(0)
            time.sleep(1 / a.rate)
            if i % 500 == 0:
                print(f"replayed {i} records, now at {r['time_tag']}")
        prod.flush()
        return

    seen: set[tuple[str, str]] = set()
    while True:
        try:
            new = 0
            for r in records(get_json(URLS["wind"]), get_json(URLS["mag"])):
                key = (r["time_tag"], r["source"])
                if key not in seen:
                    seen.add(key)
                    send(r)
                    new += 1
            prod.flush()
            print(f"published {new} new spacecraft-minutes")
            if len(seen) > 20000:  # feed only spans ~1 day; forget old keys
                seen = set(sorted(seen)[-10000:])
        except Exception as e:  # keep the stream alive through a bad poll
            print(f"poll failed: {e}")
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
