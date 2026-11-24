"""The Kafka consumer's alert logic (no broker needed: the Detector is plain Python)."""
import json
import math

import pytest

from pipeline.features import lead_time_minutes
from stream.consumer import WINDOW_MIN, Detector
from stream.producer import records


def rec(minute, v=400.0, bz=1.0, by=0.0, src="SOLAR1", hour=0):
    return {"source": src, "time_tag": f"2026-01-01T{hour:02d}:{minute:02d}:00", "proton_speed": v,
            "bz_gsm": bz, "by_gsm": by}


def feed(d, recs):
    out = []
    for r in recs:
        out += d.evaluate(r)
    return out


def test_a_100_kms_rise_raises_one_shock_alert_with_earth_eta():
    a = feed(Detector(), [rec(i, 400 if i < 8 else 560) for i in range(12)])
    shocks = [x for x in a if x["type"] == "SHOCK"]
    assert len(shocks) == 1                                    # cooldown: one per hour
    assert shocks[0]["earth_eta_min"] == pytest.approx(lead_time_minutes(560), abs=0.1)


def test_a_90_kms_rise_is_not_a_shock():
    assert not any(x["type"] == "SHOCK" for x in feed(Detector(), [rec(i, 400 if i < 8 else 490) for i in range(12)]))


def test_shock_alert_can_fire_again_in_the_next_hour():
    d = Detector()
    first = feed(d, [rec(i, 400 if i < 8 else 560) for i in range(12)])
    second = feed(d, [rec(i, 400 if i < 8 else 560, hour=1) for i in range(12)])
    assert sum(x["type"] == "SHOCK" for x in first + second) == 2


def test_window_is_time_based_so_a_data_gap_cannot_stretch_it():
    d = Detector()
    d.evaluate(rec(0, 400))
    a = feed(d, [rec(40 + i, 520) for i in range(5)])          # 40 minutes later
    assert not any(x["type"] == "SHOCK" for x in a)
    assert WINDOW_MIN < 40


@pytest.mark.parametrize("v,bz,fires", [(500, -12, True), (440, -12, False), (500, -9, False)])
def test_southward_alert_needs_strong_south_field_and_fast_wind(v, bz, fires):
    a = feed(Detector(), [rec(0, v, bz)])
    assert any(x["type"] == "SOUTHWARD" for x in a) == fires


@pytest.mark.parametrize("v2,fires", [(480, True), (455, False)])   # |dv| / max(v): 16.7% vs 12.1%
def test_disagreement_alert_above_15_percent(v2, fires):
    a = feed(Detector(), [rec(0, 400, src="SOLAR1"), rec(0, v2, src="ACE")])
    assert any(x["type"] == "DISAGREE" for x in a) == fires


def test_detector_memory_stays_bounded():
    d = Detector()
    for h in range(10):
        for m in range(60):
            d.evaluate(rec(m, 400, hour=h))
    assert len(d.by_minute) <= 121
    assert len(d.buf["SOLAR1"]) <= WINDOW_MIN + 1


def test_missing_speed_never_raises():
    d = Detector()
    assert d.evaluate({"source": "ACE", "time_tag": "2026-01-01T00:00:00", "proton_speed": None,
                       "bz_gsm": -20.0, "by_gsm": None}) == []


def test_producer_records_are_json_ready():
    wind = [{"time_tag": "2026-01-01T00:00:03", "source": "ACE", "active": True, "proton_speed": 400.0,
             "proton_density": None}]
    mag = [{"time_tag": "2026-01-01T00:00:01", "source": "ACE", "active": True, "bz_gsm": -3.0}]
    (r,) = list(records(wind, mag))
    assert r["time_tag"] == "2026-01-01T00:00:00+00:00"
    assert r["proton_density"] is None and r["bz_gsm"] == -3.0
    assert not any(isinstance(v, float) and math.isnan(v) for v in r.values())


# ---------------------------------------------------------------- producer -> Kafka -> consumer
class FakeMsg:
    def __init__(self, v):
        self._v = v

    def value(self):
        return self._v

    def error(self):
        return None


class FakeBroker:
    """In-memory stand-in for Kafka: topics are lists of (key, value)."""
    def __init__(self):
        self.topics = {}

    def producer(self, conf):
        broker = self

        class P:
            def produce(self, topic, key=None, value=None):
                broker.topics.setdefault(topic, []).append((key, value))

            def poll(self, t):
                return 0

            def flush(self):
                return 0
        return P()

    def consumer(self, conf):
        broker = self

        class C:
            def subscribe(self, topics):
                self.queue = [FakeMsg(v) for t in topics for _, v in broker.topics.get(t, [])]

            def poll(self, t):
                if not self.queue:
                    raise KeyboardInterrupt        # end of the replay
                return self.queue.pop(0)

            def close(self):
                pass
        return C()


def test_replayed_sample_flows_through_kafka_and_raises_alerts(monkeypatch, sample_dir):
    import confluent_kafka
    import stream.consumer as consumer
    import stream.producer as producer
    broker = FakeBroker()
    monkeypatch.setattr(producer, "Producer", broker.producer)
    monkeypatch.setattr(producer.time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["producer", "--replay", str(sample_dir)])
    producer.main()
    sent = broker.topics["rtsw.minute"]
    assert len(sent) > 5000 and {k for k, _ in sent} == {"ACE", "IMAP", "SOLAR1"}   # keyed by spacecraft

    monkeypatch.setattr(confluent_kafka, "Consumer", broker.consumer)
    monkeypatch.setattr(confluent_kafka, "Producer", broker.producer)
    monkeypatch.setattr("sys.argv", ["consumer"])
    consumer.main()
    alerts = [json.loads(v) for _, v in broker.topics.get("rtsw.alerts", [])]
    kinds = {a["type"] for a in alerts}
    assert "SHOCK" in kinds and "SOUTHWARD" in kinds          # the sample's planted shock and storm
    assert all(a["earth_eta_min"] is None or 20 < a["earth_eta_min"] < 120 for a in alerts)
