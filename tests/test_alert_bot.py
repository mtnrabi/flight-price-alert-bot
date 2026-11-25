import json
from pathlib import Path

import pytest

import alert_bot as ab

ROOT = Path(__file__).resolve().parent.parent
REC = ROOT / "recorded"


def watches():
    return ab.load_watches(ROOT / "watches.example.json")


def run(state, ws=None):
    sent = []
    new = ab.check_all(ws or watches(), ab.replay_search(REC), [sent.append], state, now="T", log=lambda *_: None)
    return new, sent


def test_oneway_and_roundtrip_bodies_use_documented_fields():
    one, rt = watches()
    path, body = ab.build_request(one)
    assert path == "oneway/v1" and "return_date" not in body
    assert {"from_airport", "to_airport", "departure_date", "currency", "limit"} <= body.keys()
    path, body = ab.build_request(dict(rt, max_stops=0))
    assert path == "roundtrip/v1" and body["return_date"] == "2027-02-08"
    assert body["max_departure_stops"] == 0 and body["max_return_stops"] == 0
    assert all(k == k.lower() for k in body)


def test_no_price_sort_so_google_sends_its_range_and_verdict():
    for w in watches():
        _, body = ab.build_request(w)
        assert "sort_type" not in body and "max_price" not in body  # either one drops Google's range and verdict


def test_cheapest_row_wins_when_it_is_not_first():
    # default order: fewest stops first, so the cheapest row can come later
    band = {"price_insights_low": 100, "price_insights_high": 200, "price_range_in_relation_to_other_periods": "low"}
    rows = [dict(band, price_as_number=140, stops=0), dict(band, price_as_number=90, stops=1)]
    w = {"name": "x", "from": "LHR", "to": "BCN", "departure_date": "2027-03-12"}
    sent = []
    state = ab.check_all([w], lambda path, body: rows, [sent.append], {}, now="T", log=lambda *_: None)
    assert state == {"LHR-BCN_2027-03-12_": 90}
    assert "under Google's usual range (100-200)" in sent[0] and "LOW" in sent[0]


def test_alerts_only_when_worth_it_on_recorded_fares():
    state, sent = run({})
    # LHR-BCN cheapest is 121 <= target 130 -> alert; JFK-LIS 628 > target 600, Google 'typical' -> no alert
    assert len(sent) == 1
    assert "London to Barcelona" in sent[0] and "121 USD one way" in sent[0]
    assert "https://www.google.com/travel/flights" in sent[0]
    assert state == {"LHR-BCN_2027-03-12_": 121}


def test_no_repeat_alert_for_same_price():
    state, _ = run({})
    state2, sent = run(state)
    assert sent == [] and state2 == state


def test_alerts_again_when_price_drops_further():
    _, sent = run({"LHR-BCN_2027-03-12_": 125})
    assert len(sent) == 1


def test_below_google_range_and_low_verdict_reasons():
    w = {"name": "x", "from": "A", "to": "B", "departure_date": "2027-01-01"}
    row = {"price_as_number": 40, "price_insights_low": 50, "price_insights_high": 135,
           "price_range_in_relation_to_other_periods": "low"}
    r = ab.reasons_to_alert(w, row)
    assert any("under Google's usual range" in x for x in r) and any("LOW" in x for x in r)
    assert ab.reasons_to_alert(dict(w, alert_below_google_range=False, alert_on_low_verdict=False), row) == []


def test_failed_search_is_skipped_not_alerted():
    def boom(path, body):
        raise ab.SearchFailed("HTTP 503")
    sent = []
    state = ab.check_all(watches(), boom, [sent.append], {}, log=lambda *_: None)
    assert sent == [] and state == {}


def test_roundtrip_message_mentions_both_legs():
    rows = json.loads((REC / "JFK-LIS_2027-02-01_2027-02-08.json").read_text())
    best = ab.cheapest(rows)
    msg = ab.format_message(watches()[1], best, ["test"], "T")
    assert "628 USD round trip" in msg and "back 4:35 PM on Mon, Feb 8" in msg


def test_missing_notifier_is_a_clear_error(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        ab.notifiers_from_env(dry_run=False)


class _Resp:
    def __init__(self, body=b'{"ok": true}'):
        self.body = body

    def read(self):
        return self.body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_telegram_and_discord_requests(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _Resp()

    monkeypatch.setattr(ab.urllib.request, "urlopen", fake_urlopen)
    ab.send_telegram("hello", "123:ABC", "42")
    ab.send_discord("hello", "https://discord.com/api/webhooks/1/x")
    tg, dc = seen
    assert tg.full_url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert b"chat_id=42" in tg.data and b"text=hello" in tg.data
    assert json.loads(dc.data) == {"content": "hello"}
