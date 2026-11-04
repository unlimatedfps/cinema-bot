#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elCinema (VOX Almaza) -> Telegram notifier."""

import datetime as dt
import json
import os
import pathlib
import random
import re
import time

import requests

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO = dt.timezone(dt.timedelta(hours=2))

THEATERS    = [t.strip() for t in os.getenv("THEATERS", "3101353").split(",") if t.strip()]
STATE_FILE  = pathlib.Path(os.getenv("STATE_FILE", "state.json"))
TOKEN       = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL    = int(os.getenv("INTERVAL_SECONDS", "120"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "0"))
MAX_NOTIFY  = int(os.getenv("MAX_NOTIFY", "40"))

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
          "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
          "december": 12}

MOVIE_RE = re.compile(r'href="[^"]*?/work/(\d+)[^"]*"[^>]*>([^<]{0,90})</a>', re.I)
DAY_RE = re.compile(r'(?:Saturday|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday)\s+'
                    r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|'
                    r'September|October|November|December)', re.I)
EXP_RE = re.compile(r'>\s*(Standard|Gold|Kids|4DX|IMAX|MAX VIP|MAX|VIP|Premium|'
                    r'Platinum|Screen ?X|Dolby[^<]{0,10})\s*<', re.I)
TIME_RE = re.compile(r'>\s*(\d{1,2}:\d{2})\s*(am|pm|midnight|noon)?\s*<', re.I)
TITLE_RE = re.compile(r'<title>([^<]{3,150})</title>', re.I)


def log(*a):
    print(dt.datetime.now(CAIRO).strftime("[%H:%M:%S]"), *a, flush=True)


def make_date(day, month_name):
    today = dt.datetime.now(CAIRO).date()
    mon = MONTHS[month_name.lower()]
    year = today.year
    if mon < today.month - 6:
        year += 1
    try:
        return dt.date(year, mon, int(day)).isoformat()
    except ValueError:
        return today.isoformat()


def to_24h(hhmm, suffix):
    h, mi = hhmm.split(":")
    h = int(h)
    s = (suffix or "").lower()
    if s == "pm" and h != 12:
        h += 12
    elif s == "am" and h == 12:
        h = 0
    elif s == "midnight":
        h = 0
    elif s == "noon":
        h = 12
    return "%02d:%s" % (h % 24, mi)


def fetch(url):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            log("HTTP", r.status_code, url)
            if r.status_code == 404:
                return None
        except Exception as e:
            log("fetch error:", type(e).__name__)
        time.sleep(2 + attempt * 3 + random.random())
    return None


def parse(html, theater):
    events = []
    for m in MOVIE_RE.finditer(html):
        events.append((m.start(), 0, "movie", m.group(1), m.group(2).strip()))
    for m in DAY_RE.finditer(html):
        events.append((m.start(), 1, "day", m.group(1), m.group(2)))
    for m in EXP_RE.finditer(html):
        events.append((m.start(), 2, "exp", m.group(1).strip(), ""))
    for m in TIME_RE.finditer(html):
        events.append((m.start(), 3, "time", m.group(1), m.group(2) or ""))
    events.sort(key=lambda x: (x[0], x[1]))

    titles = {}
    rows = []
    cur_movie = None
    cur_date = None
    cur_exp = ""
    for item in events:
        kind, a, b = item[2], item[3], item[4]
        if kind == "movie":
            cur_movie = a
            if b and not b.isdigit():
                titles.setdefault(a, b)
        elif kind == "day":
            cur_date = make_date(a, b)
        elif kind == "exp":
            cur_exp = a
        elif kind == "time" and cur_movie and cur_date:
            rows.append({"date": cur_date, "time": to_24h(a, b),
                         "movie": titles.get(cur_movie, "Movie #" + cur_movie),
                         "cinema": theater, "experience": cur_exp})
    return rows


def collect():
    seen = set()
    out = []
    for tid in THEATERS:
        html = fetch("https://elcinema.com/en/theater/" + tid + "/")
        if not html:
            log("theater", tid, "-> no page")
            continue
        t = TITLE_RE.search(html)
        name = t.group(1).split(" - ")[0].strip() if t else ("Theater " + tid)
        rows = parse(html, name)
        log("theater", tid, name, "->", len(rows), "showtimes")
        for r in rows:
            key = "|".join([r["date"], r["cinema"], r["movie"], r["time"], r["experience"]])
            if key in seen:
                continue
            seen.add(key)
            r["key"] = key
            out.append(r)
        time.sleep(1.5)
    return out


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"seen": {}, "bootstrapped": False}


def save_state(state):
    cutoff = (dt.datetime.now(CAIRO).date() - dt.timedelta(days=1)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if k.split("|")[0] >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), "utf-8")


def tg(text):
    if not TOKEN or not CHAT_ID:
        log("!! telegram secrets missing")
        return
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    for i in range(0, len(text), 3800):
        data = {"chat_id": CHAT_ID, "text": text[i:i + 3800],
                "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                r = S.post(url, data=data, timeout=20)
                if r.ok:
                    break
                log("telegram error:", r.status_code, r.text[:200])
            except Exception as e:
                log("telegram exception:", type(e).__name__)
            time.sleep(3 * (attempt + 1))


def format_message(rows):
    by_movie = {}
    for r in rows:
        by_movie.setdefault(r["movie"], []).append(r)
    parts = ["\U0001F4FA <b>[elCinema] نزلت " + str(len(rows)) + " حفلة جديدة</b>"]
    for movie, items in by_movie.items():
        parts.append("\n<b>" + movie + "</b>")
        for r in sorted(items, key=lambda x: (x["date"], x["time"]))[:12]:
            exp = " · " + r["experience"] if r["experience"] else ""
            parts.append("• " + r["date"] + " — " + r["time"] + exp)
        if len(items) > 12:
            parts.append("• … و" + str(len(items) - 12) + " حفلة كمان")
    return "\n".join(parts)


def one_pass():
    state = load_state()
    rows = collect()
    if not rows:
        log("nothing parsed")
        if not state.get("warned"):
            state["warned"] = True
            save_state(state)
            tg("\u26A0\uFE0F [elCinema] البوت مقدرش يقرا أي مواعيد.")
        return
    state["warned"] = False
    new = [r for r in rows if r["key"] not in state["seen"]]
    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")
    for r in rows:
        state["seen"].setdefault(r["key"], now)
    if not state.get("bootstrapped"):
        state["bootstrapped"] = True
        save_state(state)
        tg("\u2705 [elCinema] البوت التاني اشتغل على GitHub. اتسجّل "
           + str(len(rows)) + " موعد كخط أساس.")
        return
    save_state(state)
    if new:
        log(str(len(new)) + " new")
        tg(format_message(new[:MAX_NOTIFY]))
    else:
        log("no change (" + str(len(rows)) + " tracked)")


def main():
    if RUN_SECONDS == 0:
        one_pass()
        return
    deadline = None if RUN_SECONDS < 0 else time.time() + RUN_SECONDS
    while True:
        try:
            one_pass()
        except Exception as e:
            log("pass failed:", repr(e))
        if deadline is not None and time.time() + INTERVAL > deadline:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
