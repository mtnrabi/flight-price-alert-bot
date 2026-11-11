#!/usr/bin/env python3
"""flight-price-alert-bot: watch flight routes and get a Telegram (or Discord) message when a fare drops.

For each watch in watches.json it checks the live Google Flights fare (one request per watch per check)
and sends an alert when the cheapest fare is:
  - at or under your target price, or
  - under Google's own usual range for that route (price_insights_low), or
  - marked "low" by Google (price_range_in_relation_to_other_periods),
and it is cheaper than the last fare it alerted you about (so no repeat pings for the same price).

Standard library only. Python 3.9+.

    python alert_bot.py --once          # check every watch once, then exit (cron / GitHub Actions)
    python alert_bot.py                 # loop forever, checking every CHECK_EVERY_HOURS
    python alert_bot.py --once --dry-run   # print the messages instead of sending them
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

API_HOST = "google-flights-live-api.p.rapidapi.com"
BASE = f"https://{API_HOST}/api/google_flights"
SIGNUP_URL = "https://rapidapi.com/mtnrabi/api/google-flights-live-api"
USER_AGENT = "flight-price-alert-bot/1.0 (+https://github.com/mtnrabi/flight-price-alert-bot)"

# Optional: lets FlightPowers count requests that come from this example.
# The API drops these two fields before it reads the search. Delete them if you prefer.
ATTRIBUTION = {"_fp_source": "gh-example", "_fp_tool": "flight-price-alert-bot"}


class SearchFailed(Exception):
    """The search did not complete. Says nothing about availability; try again on the next check."""


# ---------------------------------------------------------------- loading config

def load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=value lines). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_watches(path: Path) -> list[dict]:
    watches = json.loads(path.read_text())
    if not isinstance(watches, list) or not watches:
        raise SystemExit(f"{path} must be a JSON list with at least one watch")
    for i, w in enumerate(watches):
        for k in ("from", "to", "departure_date"):
            if not w.get(k):
                raise SystemExit(f"watch #{i + 1} in {path} is missing '{k}'")
        w.setdefault("name", f"{w['from']} -> {w['to']} {w['departure_date']}"
                     + (f" / {w['return_date']}" if w.get("return_date") else ""))
    return watches


# ---------------------------------------------------------------- the API

def build_request(w: dict) -> tuple[str, dict]:
    """(endpoint path, JSON body) for one watch. Round trip when return_date is set."""
    body = {
        "from_airport": w["from"].upper(),
        "to_airport": w["to"].upper(),
        "departure_date": w["departure_date"],
        "currency": w.get("currency", "usd"),
        "limit": 5,
        "sort_type": "Price",
    }
    if w.get("return_date"):
        body["return_date"] = w["return_date"]
        if w.get("max_stops") is not None:
            body["max_departure_stops"] = body["max_return_stops"] = int(w["max_stops"])
        path = "roundtrip/v1"
    else:
        if w.get("max_stops") is not None:
            body["max_stops"] = int(w["max_stops"])
        path = "oneway/v1"
    if w.get("passengers"):
        body["passengers"] = w["passengers"]
    body.update(ATTRIBUTION)
    return path, body


def http_search(path: str, body: dict, api_key: str, timeout: float = 90) -> list:
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-rapidapi-key": api_key,
                 "x-rapidapi-host": API_HOST, "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code >= 500:
            raise SearchFailed(f"HTTP {e.code}") from None
        if e.code in (401, 403):
            raise SystemExit(f"The API refused the key (HTTP {e.code}). Subscribe it to {SIGNUP_URL}")
        if e.code == 429:
            raise SearchFailed("HTTP 429: rate limit or monthly quota reached") from None
        raise SearchFailed(f"HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise SearchFailed(str(e)) from None


def replay_search(folder: Path) -> Callable[[str, dict], list]:
    """Answer from a recorded response file named after the watch (no key, no network)."""
    def search(path: str, body: dict) -> list:
        name = f"{body['from_airport']}-{body['to_airport']}_{body['departure_date']}"
        if body.get("return_date"):
            name += f"_{body['return_date']}"
        f = folder / f"{name}.json"
        if not f.exists():
            raise SearchFailed(f"no recorded response {f.name}")
        return json.loads(f.read_text())
    return search


# ---------------------------------------------------------------- deciding

def price_of(row: dict) -> Optional[float]:
    v = row.get("total_price_as_number", row.get("price_as_number"))
    return v if isinstance(v, (int, float)) else None


def cheapest(rows: list) -> Optional[dict]:
    priced = [r for r in rows if price_of(r) is not None]
    return min(priced, key=price_of) if priced else None


def reasons_to_alert(w: dict, row: dict) -> list[str]:
    """Why this fare is worth a message (empty list = not worth it)."""
    p = price_of(row)
    out = []
    target = w.get("target_price")
    if target is not None and p <= float(target):
        out.append(f"at or under your target of {target:g}")
    low = row.get("price_insights_low")
    if w.get("alert_below_google_range", True) and isinstance(low, (int, float)) and p < low:
        out.append(f"under Google's usual range ({low:g}-{row.get('price_insights_high', '?')})")
    if w.get("alert_on_low_verdict", True) and row.get("price_range_in_relation_to_other_periods") == "low":
        out.append("Google rates it LOW for these dates")
    return out


def format_message(w: dict, row: dict, reasons: list[str], fetched_at: str) -> str:
    cur = w.get("currency", "usd").upper()
    p = price_of(row)
    rt = "return_date" in row
    airline = row.get("departure_flight_airline") if rt else row.get("airline")
    stops = row.get("total_stops") if rt else row.get("stops")
    when = row.get("departure_flight_departure_description") if rt else row.get("departure_description")
    lines = [
        f"✈️ Fare alert: {w['name']}",
        f"{p:g} {cur} {'round trip' if rt else 'one way'} · {airline} · {stops} stop{'s' if stops != 1 else ''}",
        f"Departs {when}" + (f", back {row.get('return_flight_departure_description')}" if rt else ""),
        "Why: " + "; ".join(reasons),
    ]
    if row.get("price_insights_low") is not None:
        lines.append(f"Google: {row.get('price_range_in_relation_to_other_periods', '?')} "
                     f"(usual {row['price_insights_low']:g}-{row.get('price_insights_high', '?')} {cur})")
    lines += [f"Book: {row.get('buy_link', '')}", f"Fetched {fetched_at}. Fares change fast."]
    return "\n".join(lines)


# ---------------------------------------------------------------- notifying

def send_telegram(text: str, token: str, chat_id: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        if not json.loads(resp.read().decode()).get("ok"):
            raise RuntimeError("Telegram did not accept the message")


def send_discord(text: str, webhook_url: str) -> None:
    req = urllib.request.Request(webhook_url, data=json.dumps({"content": text[:1900]}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    urllib.request.urlopen(req, timeout=30).close()


def notifiers_from_env(dry_run: bool) -> list[Callable[[str], None]]:
    if dry_run:
        return [lambda text: print("---- would send ----\n" + text + "\n")]
    out = []
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        out.append(lambda text: send_telegram(text, tok, chat))
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        out.append(lambda text: send_discord(text, hook))
    if not out:
        raise SystemExit("No notifier set: add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL "
                         "to .env (or run with --dry-run).")
    return out


# ---------------------------------------------------------------- one check

def watch_key(w: dict) -> str:
    return f"{w['from'].upper()}-{w['to'].upper()}_{w['departure_date']}_{w.get('return_date', '')}"


def check_all(watches: list[dict], search: Callable[[str, dict], list], notify: list[Callable[[str], None]],
              state: dict, now: Optional[str] = None, log=print) -> dict:
    """Check every watch once. Returns the new state ({watch_key: last alerted price})."""
    fetched_at = now or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = dict(state)
    for w in watches:
        path, body = build_request(w)
        try:
            rows = search(path, body)
        except SearchFailed as e:
            log(f"[{w['name']}] search did not complete ({e}); will try again next check")
            continue
        best = cheapest(rows)
        if best is None:
            log(f"[{w['name']}] no flights found")
            continue
        p = price_of(best)
        reasons = reasons_to_alert(w, best)
        last = state.get(watch_key(w))
        log(f"[{w['name']}] cheapest {p:g} {w.get('currency', 'usd').upper()}, "
            f"Google: {best.get('price_range_in_relation_to_other_periods', '?')}"
            + (f"; last alert at {last:g}" if last is not None else ""))
        if reasons and (last is None or p < last):
            msg = format_message(w, best, reasons, fetched_at)
            for send in notify:
                send(msg)
            state[watch_key(w)] = p
        elif not reasons and last is not None and p > last:
            # the deal is gone: forget it, so the next drop alerts again
            state.pop(watch_key(w), None)
    return state


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Telegram / Discord alerts when a flight fare drops.")
    ap.add_argument("--watches", default="watches.json")
    ap.add_argument("--state", default="state.json", help="remembers the last alerted price per watch")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--replay", default=None, help="answer from recorded responses in this folder (no key)")
    a = ap.parse_args(argv)

    load_env_file(Path(".env"))
    watches = load_watches(Path(a.watches))
    notify = notifiers_from_env(a.dry_run)
    now = None
    if a.replay:
        search = replay_search(Path(a.replay))
        stamp = Path(a.replay) / "RECORDED_AT.txt"
        now = stamp.read_text().strip() if stamp.exists() else None
    else:
        key = os.environ.get("RAPIDAPI_KEY", "").strip()
        if not key:
            print(f"Set RAPIDAPI_KEY first. Get a free key at {SIGNUP_URL}", file=sys.stderr)
            return 2
        search = lambda path, body: http_search(path, body, key)

    state_path = Path(a.state)
    every_h = float(os.environ.get("CHECK_EVERY_HOURS", "6"))
    print(f"Watching {len(watches)} route(s); {len(watches)} request(s) per check"
          + ("" if a.once else f", every {every_h:g} h"))
    while True:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        state = check_all(watches, search, notify, state, now=now)
        state_path.write_text(json.dumps(state, indent=2))
        if a.once:
            return 0
        time.sleep(every_h * 3600)


if __name__ == "__main__":
    sys.exit(main())
