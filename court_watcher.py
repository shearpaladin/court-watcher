"""
court_watcher.py - poll a PerfectMind booking page and ping you when a full slot opens up.

Setup:
    pip install playwright requests
    playwright install chromium

Run:
    python court_watcher.py --list     # one pass, print what it parsed (use this first to check filters)
    python court_watcher.py            # loop forever, notify on openings

Config is via environment variables (or edit the defaults below):
    WATCH_URL        the exact page URL from your browser (with your location/filters applied)
    DISCORD_WEBHOOK  Discord channel webhook URL (Server Settings > Integrations > Webhooks)
    NTFY_TOPIC       optional, push to phone via the free ntfy app (https://ntfy.sh/<topic>)
    TITLE_REGEX      which events to watch, default "BADMINTON COURT"
    DATE_REGEX       which dates, e.g. "Nov 21st|Nov 28th" (blank = all)
    TIME_REGEX       which times, e.g. "5:30 pm|6:30 pm" (blank = all)
    INTERVAL_SEC     seconds between checks, default 180 (please keep it polite, 2+ minutes)
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

WATCH_URL = os.getenv("WATCH_URL", "PASTE_THE_BOOKING_PAGE_URL_HERE")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
TITLE_REGEX = os.getenv("TITLE_REGEX") or r"^#\d+\s+BADMINTON COURT$"  # excludes FAMILY AND YOUTH
DATE_REGEX = os.getenv("DATE_REGEX") or r"^(Sat|Sun)"  # weekends only
TIME_REGEX = os.getenv("TIME_REGEX", "")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "180"))
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "0"))  # 0 = run forever; GitHub Actions sets this
STATE_FILE = Path(os.getenv("STATE_FILE", "court_watcher_state.json"))

DATE_LINE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+\w+\s+\d+", re.I)
EVENT_ID = re.compile(r"#(\d{5,8})\b")
TIME_RANGE = re.compile(r"\d{1,2}:\d{2}\s*[ap]m\s*-\s*\d{1,2}:\d{2}\s*[ap]m", re.I)
NOT_OPEN = re.compile(r"\bfull\b|closed|cancel|not available|unavailable", re.I)


def parse_events(page_text: str) -> list[dict]:
    """Turn the page's visible text into a list of events.

    Text-based on purpose: PerfectMind's CSS class names change, the visible
    layout (date header, title, #id, time, location, price, status, button) doesn't.
    """
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]
    events, current, date = [], None, ""
    for i, line in enumerate(lines):
        if DATE_LINE.match(line):
            date = line
            continue
        m = EVENT_ID.search(line)
        if m:
            if current:
                events.append(current)
            title = line[: m.start()].strip()
            if not title and i > 0:
                title = lines[i - 1]
            current = {"id": m.group(1), "title": title, "date": date, "lines": []}
            continue
        if current:
            current["lines"].append(line)
    if current:
        events.append(current)

    for e in events:
        block = " | ".join(e["lines"])
        t = TIME_RANGE.search(block)
        e["time"] = t.group(0) if t else ""
        e["open"] = not NOT_OPEN.search(block)
        e["status"] = block
    return events


def matches(e: dict) -> bool:
    return (
        re.search(TITLE_REGEX, e["title"], re.I)
        and (not DATE_REGEX or re.search(DATE_REGEX, e["date"], re.I))
        and (not TIME_REGEX or re.search(TIME_REGEX, e["time"], re.I))
    )


def fetch_text(browser) -> str:
    page = browser.new_page()
    try:
        page.goto(WATCH_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2_000)  # give the listing a moment to finish rendering
        return page.inner_text("body")
    finally:
        page.close()


def notify(msg: str) -> None:
    print(msg)
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
        except Exception as ex:
            print(f"Discord notify failed: {ex}")
    if NTFY_TOPIC:
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode(),
                          headers={"Title": "Badminton court open", "Priority": "high"}, timeout=10)
        except Exception as ex:
            print(f"ntfy notify failed: {ex}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="one pass, print parsed events and exit")
    args = ap.parse_args()

    if "PASTE_" in WATCH_URL:
        sys.exit("Set WATCH_URL first (copy it from your browser's address bar).")

    state = load_state()  # id -> last known open/closed, so you only get pinged on a change
    deadline = time.time() + RUN_MINUTES * 60 if RUN_MINUTES else None
    bad_streak, warned = 0, False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        while deadline is None or time.time() < deadline:
            try:
                events = [e for e in parse_events(fetch_text(browser)) if matches(e)]
                if args.list:
                    for e in events:
                        print(f"{'OPEN ' if e['open'] else 'full '} #{e['id']} {e['date']} "
                              f"{e['time']} {e['title']}\n        {e['status']}")
                    print(f"\n{len(events)} matching events")
                    return

                # Each slot alerts once, ever. state[id] == True means "already told you".
                fresh = [e for e in events if e["open"] and not state.get(e["id"])]
                if fresh:
                    lines = [f"• {e['date'].split(',')[0]} {e['date'].split(',')[1].strip()}  "
                             f"{e['time']}  {e['title']}" for e in fresh]
                    notify(f"🏸 {len(fresh)} weekend court{'s' if len(fresh) > 1 else ''} open:\n"
                           + "\n".join(lines) + f"\nBook: <{WATCH_URL}>")
                    for e in fresh:
                        state[e["id"]] = True
                STATE_FILE.write_text(json.dumps(state))
                print(time.strftime("%H:%M:%S"), f"checked {len(events)} events,",
                      sum(e["open"] for e in events), "open", flush=True)
                bad_streak = 0 if events else bad_streak + 1
            except Exception as ex:
                print(f"check failed: {ex}", flush=True)
                bad_streak += 1
            # Warn once if the page keeps failing or showing nothing (blocked, layout changed, bad URL)
            if bad_streak >= 5 and not warned:
                notify("⚠️ Court watcher: 5 checks in a row failed or found no matching courts. "
                       "Check the GitHub Actions log.")
                warned = True
            time.sleep(INTERVAL_SEC + random.randint(0, 30))
        print("Run window finished, exiting so the next scheduled run can take over.")


if __name__ == "__main__":
    main()
