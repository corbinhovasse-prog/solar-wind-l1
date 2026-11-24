"""Turn the stream of spacecraft-minutes into decisions.

Reads `rtsw.minute`, keeps a short rolling window per spacecraft, and
publishes to `rtsw.alerts` when:
  * SHOCK      speed jumps > 100 km/s within 10 minutes (shock / CME arrival)
  * SOUTHWARD  Bz < -10 nT while speed > 450 km/s (strong geomagnetic driving)
  * DISAGREE   two spacecraft differ in speed by > 15% at the same minute
               (instrument problem, or real structure between them)
Each alert carries the estimated minutes until that wind reaches Earth.

    python -m stream.consumer
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from datetime import datetime, timedelta

from pipeline.features import lead_time_minutes, newell_coupling

WINDOW_MIN = 15  # minutes of history kept per spacecraft (by timestamp, not by record count)


class Detector:
    def __init__(self):
        self.buf = defaultdict(deque)
        self.by_minute = defaultdict(dict)  # time_tag -> {source: speed}
        self.cooldown = {}

    def _fire(self, kind, rec, msg):
        key = (kind, rec["source"])
        t = rec["time_tag"]
        if self.cooldown.get(key, "") > t:
            return None
        self.cooldown[key] = t[:13] + "~"  # at most one alert per kind/source per clock hour
        v = rec.get("proton_speed")
        return {"type": kind, "source": rec["source"], "time_tag": t, "message": msg,
                "earth_eta_min": round(lead_time_minutes(v), 1) if v else None}

    def evaluate(self, rec: dict) -> list[dict]:
        alerts = []
        src, v, bz, by = rec["source"], rec.get("proton_speed"), rec.get("bz_gsm"), rec.get("by_gsm")
        buf = self.buf[src]
        buf.append(rec)
        # drop records older than WINDOW_MIN minutes, so a data gap can't stretch the window
        now = datetime.fromisoformat(rec["time_tag"])
        while buf and now - datetime.fromisoformat(buf[0]["time_tag"]) > timedelta(minutes=WINDOW_MIN):
            buf.popleft()

        speeds = [r["proton_speed"] for r in buf if r.get("proton_speed") is not None]
        if v is not None and len(speeds) >= 5 and v - min(speeds) > 100:
            alerts.append(self._fire("SHOCK", rec, f"speed rose {v - min(speeds):.0f} km/s in <= {WINDOW_MIN} min to {v:.0f} km/s"))

        if v is not None and bz is not None and bz < -10 and v > 450:
            c = newell_coupling(v, by or 0.0, bz)
            alerts.append(self._fire("SOUTHWARD", rec, f"Bz {bz:.1f} nT at {v:.0f} km/s, coupling {c:.1f}"))

        if v is not None:
            same = self.by_minute[rec["time_tag"]]
            for other, ov in same.items():
                if other != src and abs(v - ov) / max(v, ov) > 0.15:
                    alerts.append(self._fire("DISAGREE", rec, f"{src} {v:.0f} vs {other} {ov:.0f} km/s"))
            same[src] = v
            if len(self.by_minute) > 120:
                for k in sorted(self.by_minute)[:-60]:
                    del self.by_minute[k]
        return [a for a in alerts if a]


def main():
    from confluent_kafka import Consumer, Producer

    p = argparse.ArgumentParser()
    p.add_argument("--bootstrap", default="localhost:9092")
    p.add_argument("--group", default="l1-detector")
    a = p.parse_args()

    cons = Consumer({"bootstrap.servers": a.bootstrap, "group.id": a.group, "auto.offset.reset": "earliest"})
    prod = Producer({"bootstrap.servers": a.bootstrap})
    cons.subscribe(["rtsw.minute"])
    det, n = Detector(), 0
    print("listening on rtsw.minute ... (Ctrl+C to stop)")
    try:
        while True:
            msg = cons.poll(1.0)
            if msg is None or msg.error():
                continue
            n += 1
            for alert in det.evaluate(json.loads(msg.value())):
                prod.produce("rtsw.alerts", key=alert["type"], value=json.dumps(alert))
                print(f"ALERT {alert['type']:<9} {alert['time_tag']} {alert['source']:<6} "
                      f"{alert['message']}  (reaches Earth in ~{alert['earth_eta_min']} min)")
            prod.poll(0)
            if n % 1000 == 0:
                print(f"... {n} records processed")
    except KeyboardInterrupt:
        pass
    finally:
        cons.close()
        prod.flush()


if __name__ == "__main__":
    main()
