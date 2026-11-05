#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOX Egypt (City Centre Almaza) -> Telegram.

Runs in two modes from the same file:
  RUN_SECONDS=-1  (default)  infinite loop, sends a startup ping  -> Termux
  RUN_SECONDS=250            bounded loop, no startup ping        -> GitHub Actions
  RUN_SECONDS=0              one pass and exit                    -> manual test
"""
import datetime as dt
import json
import os
import pathlib
import random
import re
import time

import requests

import fmt
from fmt import CAIRO, log, word_date, any_date, tg

BASE = "https://egy.voxcinemas.com"
CINEMA = os.getenv("CINEMA", "city-centre-almaza")
CINEMA_LABEL = os.getenv("CINEMA_LABEL", "VOX الماظة")
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "120"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "-1"))
STATE_FILE = pathlib.Path(os.getenv("STATE_FILE", "vox_state.json"))
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_NOTIFY = int(os.getenv("MAX_NOTIFY", "0"))  # 0 = no cap
TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "12"))
MAX_HORIZON = int(os.getenv("MAX_HORIZON_DAYS", "365"))

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

TIME_TXT = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(AM|PM|am|pm)?")
ISO_DT = re.compile(r"(\d{4}-\d{2}-\d{2})[T ]([0-2]\d:[0-5]\d)")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
COMPACT_DATE = re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b")

MOVIE_KEYS = {"moviename", "movietitle", "filmtitle", "filmname", "eventname"}
EXP_KEYS = {"experience", "format", "screentype", "sessionattribute", "attribute"}
TIME_KEYS = {"time", "starttime", "showtime", "sessiontime", "start", "showdatetime",
             "datetime", "showtimedate", "sessiondatetime", "startsat"}
DATE_KEYS = {"date", "showdate", "sessiondate", "day", "displaydate", "datelabel",
             "dateheading", "daylabel", "businessdate"}

# Words the site prints instead of a real date.
WORD_DAYS = {
    "today": 0, "tonight": 0, "اليوم": 0, "النهاردة": 0, "النهارده": 0,
    "tomorrow": 1, "بكرة": 1, "بكره": 1, "غدا": 1, "غداً": 1,
}

TOKEN_RE = re.compile(r"/movies/([a-z0-9\-]{3,})"
                      r"|>\s*((?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM|am|pm)?)\s*<")

WORD_IN_HTML = re.compile(
    r">\s*([^<>]{0,30}?(?:Today|Tonight|Tomorrow|اليوم|النهاردة|بكرة)[^<>]{0,30}?)\s*<",
    re.I)


def nkey(k):
    return k.lower().replace("_", "").replace("-", "")


def to_hhmm(raw):
    """Return (date_or_None, 'HH:MM'). None date means the string carried only
    a time, and the caller decides which date it belongs to."""
    m = ISO_DT.search(raw)
    if m:
        return m.group(1), m.group(2)
    wd = any_date(raw)
    m = TIME_TXT.search(raw)
    if not m:
        return None, None
    h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return wd, "%02d:%s" % (h, mi)


def json_blobs(html):
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        txt = m.group(1).strip()
        if len(txt) < 20:
            continue
        if txt.startswith("{") or txt.startswith("["):
            try:
                yield json.loads(txt)
                continue
            except Exception:
                pass
        for m2 in re.finditer(r"=\s*(\{.{200,}?\})\s*[;\n]", txt, re.S):
            try:
                yield json.loads(m2.group(1))
            except Exception:
                pass


def walk(node, ctx, out, page_date):
    if isinstance(node, dict):
        local = dict(ctx)
        hint = local.pop("_hint", None)
        for k, v in node.items():
            if not isinstance(v, (str, int, float)):
                continue
            lk = nkey(k)
            sv = re.sub(r"\s+", " ", str(v)).strip()
            if not sv or sv.lower() in ("none", "null"):
                continue
            if lk in MOVIE_KEYS:
                local["movie"] = sv
            elif lk in EXP_KEYS and len(sv) < 40:
                local["experience"] = sv
            elif lk in DATE_KEYS:
                d = any_date(sv)
                if d:
                    local["date"] = d
            elif lk in ("title", "name", "slug") and hint == "movie":
                local.setdefault("movie", sv)
        raw_time = None
        for k, v in node.items():
            if isinstance(v, str) and nkey(k) in TIME_KEYS:
                if ISO_DT.search(v) or TIME_TXT.search(v):
                    raw_time = v
                    break
        if raw_time:
            d, hhmm = to_hhmm(raw_time)
            if hhmm:
                out.append({"date": d or local.get("date") or page_date,
                            "time": hhmm,
                            "movie": local.get("movie", "?"),
                            "experience": local.get("experience", "")})
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                child = dict(local)
                lk = nkey(k)
                if lk.startswith("movie") or lk.startswith("film"):
                    child["_hint"] = "movie"
                walk(v, child, out, page_date)
    elif isinstance(node, list):
        for it in node:
            walk(it, dict(ctx), out, page_date)


def html_fallback(html, page_date):
    """Plain-HTML pass used when no JSON blob yielded anything.

    Tracks word-dates ('Today') as section headings so the times printed
    under one are stamped with the right day.
    """
    events = []
    for m in TOKEN_RE.finditer(html):
        if m.group(1):
            events.append((m.start(), "movie", m.group(1)))
        elif m.group(2):
            events.append((m.start(), "time", m.group(2)))
    for m in WORD_IN_HTML.finditer(html):
        d = word_date(m.group(1))
        if d:
            events.append((m.start(), "date", d))
    for m in ISO_DATE.finditer(html):
        events.append((m.start(), "date", m.group(0)))
    events.sort(key=lambda x: x[0])

    out, movie, cur_date = [], "?", None
    for _, kind, val in events:
        if kind == "movie":
            movie = val.replace("-", " ").title()
        elif kind == "date":
            cur_date = val
        elif kind == "time" and movie != "?":
            d, hhmm = to_hhmm(val)
            if hhmm:
                out.append({"date": d or cur_date or page_date, "time": hhmm,
                            "movie": movie, "experience": ""})
    return out


def fetch(url):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            log("HTTP", r.status_code, url)
        except Exception as e:
            log("fetch error:", type(e).__name__)
        time.sleep(2 + attempt * 3 + random.random())
    return None


DAY_LINK = re.compile(r'[?&]d=(\d{8})')


def discover_days():
    """Return {iso_date: url} for exactly the days the cinema page lists,
    or None if the page could not be read.

    No invented days: if the landing page is unreachable the caller reports
    a failed scan rather than guessing a window that may not exist.
    """
    today = dt.datetime.now(CAIRO).date()
    base_url = BASE + "/showtimes?c=" + CINEMA

    html = fetch(base_url)
    if not html:
        log("landing page unreadable - cannot list the days")
        return None

    # The landing page itself is the cinema page for today.
    days = {today.isoformat(): base_url}
    skipped = 0
    for m in DAY_LINK.finditer(html):
        raw = m.group(1)
        try:
            d = dt.datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            continue
        if d < today or (d - today).days > MAX_HORIZON:
            skipped += 1
            continue
        # Use the site's own parameter spelling, not a guessed one.
        days[d.isoformat()] = base_url + "&d=" + raw
    if skipped:
        log("ignored", skipped, "out-of-range day links")
    return days


def collect():
    """Return (rows, failed_days). A day that could not be fetched is reported,
    never silently treated as 'this day has no showtimes'."""
    seen, out, failed = set(), [], []
    days = discover_days()
    if days is None:
        return [], ["<landing page>"]
    log("days on the cinema page:", len(days))
    for day, url in sorted(days.items()):
        html = fetch(url)
        if not html:
            failed.append(day)
            continue
        rows = []
        for blob in json_blobs(html):
            walk(blob, {}, rows, day)
        if not rows:
            rows = html_fallback(html, day)
        log("day", day, "->", len(rows), "showtimes")
        for r in rows:
            # Cinema is pinned to one constant so the key never drifts when the
            # site spells the venue differently between responses.
            key = "|".join([r["date"], CINEMA, r["movie"], r["time"], r["experience"]])
            if key in seen:
                continue
            seen.add(key)
            r["key"] = key
            out.append(r)
        time.sleep(1.5)
    return out, failed


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


def tg_s(text):
    return tg(text, S)


def format_message(rows):
    return fmt.format_message(rows, CINEMA_LABEL,
                              BASE + "/showtimes?c=" + CINEMA)


def one_pass():
    state = load_state()
    rows, failed = collect()

    if failed:
        log("failed days:", ", ".join(failed))

    if not rows:
        log("nothing parsed")
        if not state.get("warned"):
            state["warned"] = True
            save_state(state)
            tg_s("⚠️ البوت مقدرش يقرا أي مواعيد من فوكس " + CINEMA_LABEL + ".")
        return
    state["warned"] = False

    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")

    if not state.get("bootstrapped"):
        if failed:
            # Never set the baseline from a partial scan: every day that failed
            # would be reported as brand new on the next successful pass.
            log("baseline skipped - incomplete scan")
            return
        for r in rows:
            state["seen"].setdefault(r["key"], now)
        state["bootstrapped"] = True
        save_state(state)
        tg_s("✅ البوت اشتغل على " + CINEMA_LABEL + ". اتسجّل "
           + str(len(rows)) + " موعد كخط أساس.")
        return

    new = [r for r in rows if r["key"] not in state["seen"]]
    if new:
        log(str(len(new)) + " new")
        # Send BEFORE recording them. If Telegram is down the state does not
        # advance, so the same showtimes are retried on the next pass instead
        # of being lost.
        batch = new[:MAX_NOTIFY] if MAX_NOTIFY > 0 else new
        if not tg(format_message(batch), S):
            log("telegram failed - state not advanced, will retry")
            return
    else:
        log("no change (" + str(len(rows)) + " tracked)")

    for r in rows:
        state["seen"].setdefault(r["key"], now)
    save_state(state)


def main():
    if RUN_SECONDS < 0:
        tg_s("\U0001F7E2 البوت بدأ الشغل على الموبايل.")
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
