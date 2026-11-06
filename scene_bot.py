#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scene Cinemas (District 5) -> Telegram.

Reads the cinema's own site, which answers from GitHub runners, so this one
can run on a schedule rather than only on the phone.

Each showtime carries a unique id in its booking link, so that id is the
dedup key: no guessing from date + title + time, and nothing is re-announced
when the site words something differently between passes.
"""
import datetime as dt
import html as htmllib
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

BASE = os.getenv("SCENE_BASE", "https://district5.scenecinemas.com").rstrip("/")
LABEL = os.getenv("SCENE_LABEL", "Scene District 5")
STATE_FILE = pathlib.Path(os.getenv("STATE_FILE", "scene_state.json"))
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "600"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "0"))
TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "30"))
PAUSE = float(os.getenv("REQUEST_PAUSE", "0.4"))
DRY_RUN = os.getenv("DRY_RUN", "") == "1"

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

SLUG_RE = re.compile(r"/movie-details/([a-z0-9\-]+)\.html")
DAY_TAB_RE = re.compile(r"id='data-(\d{2}-\d{2}-\d{4})'")
TITLE_RE = re.compile(r'"movie_name"\s*:\s*"([^"]+)"')
H1_RE = re.compile(r"<h1[^>]*>\s*([^<]{2,120}?)\s*</h1>")
# Each experience has its own class (ex_stand, ex_imax, ...), so match
# the family rather than one spelling.
EXP_RE = re.compile(r'class="ex_[a-z_]*"[^>]*>\s*([^<]{1,60}?)\s*<')
SHOW_RE = re.compile(r'/showtime-([0-9a-zA-Z]+)"[^>]*>\s*(\d{1,2}:\d{2})\s*(AM|PM)', re.I)

AR_DAYS = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس",
           "الجمعة", "السبت", "الأحد"]
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


def log(*a):
    print(dt.datetime.now(CAIRO).strftime("[%H:%M:%S]"), *a, flush=True)


def to_iso(ddmmyyyy):
    """'06-11-2026' -> '2026-11-06'."""
    d, m, y = ddmmyyyy.split("-")
    return "%s-%s-%s" % (y, m, d)


def to_24h(hhmm, ampm):
    h, mi = hhmm.split(":")
    h = int(h) % 12
    if ampm.upper() == "PM":
        h += 12
    return "%02d:%s" % (h, mi)


def h12(hhmm):
    h, mi = hhmm.split(":")
    h = int(h)
    return "%d:%s %s" % (h % 12 or 12, mi, "AM" if h < 12 else "PM")


def night_key(hhmm):
    """Sort a cinema day, not a clock day: a 00:15 show ends the night, so
    anything before 06:00 sorts after the evening shows."""
    h, mi = hhmm.split(":")
    h = int(h)
    return (h + 24 if h < 6 else h, int(mi))


def pretty_date(iso):
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    label = "%s %d %s" % (AR_DAYS[d.weekday()], d.day, AR_MONTHS[d.month - 1])
    delta = (d - dt.datetime.now(CAIRO).date()).days
    if delta == 0:
        return "النهاردة · " + label
    if delta == 1:
        return "بكرة · " + label
    return label


def fetch(url):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            log("HTTP", r.status_code, url)
            if r.status_code == 404:
                return None
        except Exception as e:
            log("fetch error:", type(e).__name__, url.rsplit("/", 1)[-1][:40])
        time.sleep(2 + attempt * 3 + random.random())
    return None


def parse_showtimes(fragment, movie, iso_date):
    """Walk the fragment in document order so each time is attributed to the
    experience heading above it."""
    events = []
    for m in EXP_RE.finditer(fragment):
        events.append((m.start(), "exp", htmllib.unescape(m.group(1)), ""))
    for m in SHOW_RE.finditer(fragment):
        events.append((m.start(), "time", m.group(1),
                       to_24h(m.group(2), m.group(3))))
    events.sort(key=lambda x: x[0])

    rows, exp = [], ""
    for _, kind, a, b in events:
        if kind == "exp":
            exp = a
        else:
            rows.append({"id": a, "date": iso_date, "time": b,
                         "movie": movie, "experience": exp})
    return rows


def movie_title(page, slug):
    m = TITLE_RE.search(page)
    if m:
        return htmllib.unescape(m.group(1)).strip()
    m = H1_RE.search(page)
    if m and "showtimes" not in m.group(1).lower():
        return htmllib.unescape(m.group(1)).strip()
    return slug.replace("-", " ").title()


def collect():
    """Return (rows, failed). A page that could not be read is reported, never
    treated as 'this movie has no showtimes'."""
    home = fetch(BASE + "/")
    if not home:
        log("home page unreadable")
        return [], ["<home>"]

    slugs = sorted(set(SLUG_RE.findall(home)))
    log("movies listed:", len(slugs))

    rows, failed, seen_ids = [], [], set()
    for slug in slugs:
        page = fetch(BASE + "/movie-details/" + slug + ".html")
        if not page:
            failed.append(slug)
            continue
        title = movie_title(page, slug)
        days = DAY_TAB_RE.findall(page)
        if not days:
            log("no day tabs for", slug)

        # The page already carries the first day's showtimes; the rest come
        # from the same URL with the business_day parameter the site uses.
        for i, day in enumerate(dict.fromkeys(days)):
            iso = to_iso(day)
            if i == 0:
                frag = page
            else:
                frag = fetch(BASE + "/movie-details/" + slug
                             + ".html?business_day=" + day + "&ajax=1")
                if not frag:
                    failed.append(slug + "@" + day)
                    continue
                time.sleep(PAUSE)
            for r in parse_showtimes(frag, title, iso):
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                rows.append(r)
        time.sleep(PAUSE)
    return rows, failed


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"seen": {}, "bootstrapped": False}


def save_state(state):
    cutoff = (dt.datetime.now(CAIRO).date() - dt.timedelta(days=1)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items()
                     if not isinstance(v, dict) or v.get("date", "9") >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True),
                          "utf-8")


def chunk_lines(text, limit=3500):
    """Split on line boundaries, never mid-tag: a <b> cut in half makes
    Telegram reject the whole message."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        out.append(cur)
    return out


def tg(text):
    """Returns True only if every chunk went through, so the caller can hold
    back its state and retry instead of losing the alert."""
    if DRY_RUN:
        print(text)
        return True
    if not TOKEN or not CHAT_ID:
        log("!! telegram secrets missing")
        return False
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    ok_all = True
    for chunk in chunk_lines(text):
        sent = False
        for attempt in range(3):
            try:
                r = S.post(url, data={"chat_id": CHAT_ID, "text": chunk,
                                      "parse_mode": "HTML",
                                      "disable_web_page_preview": True},
                           timeout=20)
                if r.ok:
                    sent = True
                    break
                log("telegram error:", r.status_code, r.text[:200])
                if r.status_code == 400:
                    plain = re.sub(r"</?b>", "", chunk)
                    if S.post(url, data={"chat_id": CHAT_ID, "text": plain,
                                         "disable_web_page_preview": True},
                              timeout=20).ok:
                        sent = True
                        break
            except Exception as e:
                log("telegram exception:", type(e).__name__)
            time.sleep(3 * (attempt + 1))
        if not sent:
            ok_all = False
    return ok_all


def format_message(rows):
    parts = ["\U0001F3AC <b>نزلت " + str(len(rows)) + " حفلة جديدة — "
             + LABEL + "</b>"]
    by_movie = {}
    for r in rows:
        by_movie.setdefault(r["movie"], []).append(r)
    for movie in sorted(by_movie):
        parts.append("")
        parts.append("<b>" + movie + "</b>")
        by_date = {}
        for r in by_movie[movie]:
            by_date.setdefault(r["date"], []).append(r)
        for date in sorted(by_date):
            parts.append("▸ " + pretty_date(date))
            by_exp = {}
            for r in by_date[date]:
                by_exp.setdefault(r["experience"], set()).add(r["time"])
            for exp in sorted(by_exp):
                times = " · ".join(h12(t) for t in sorted(by_exp[exp],
                                                          key=night_key))
                parts.append(("   " + exp + ": " + times) if exp
                             else ("   " + times))
    parts.append("")
    parts.append("\U0001F517 " + BASE + "/")
    return "\n".join(parts)


def one_pass():
    state = load_state()
    rows, failed = collect()
    if failed:
        log("failed:", ", ".join(failed[:8]), "..." if len(failed) > 8 else "")

    if not rows:
        log("nothing parsed")
        if not state.get("warned"):
            state["warned"] = True
            save_state(state)
            tg("⚠️ البوت مقدرش يقرا أي مواعيد من " + LABEL + ".")
        return
    state["warned"] = False
    log("parsed", len(rows), "showtimes")

    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")

    if not state.get("bootstrapped"):
        if failed:
            # Never set the baseline from a partial scan: everything that
            # failed would look brand new on the next successful pass.
            log("baseline skipped - incomplete scan")
            return
        for r in rows:
            state["seen"][r["id"]] = {"at": now, "date": r["date"]}
        state["bootstrapped"] = True
        save_state(state)
        tg("✅ البوت اشتغل على " + LABEL + ". اتسجّل "
           + str(len(rows)) + " موعد كخط أساس.")
        return

    new = [r for r in rows if r["id"] not in state["seen"]]
    if new:
        log(len(new), "new")
        # Send BEFORE recording, so a failed send is retried next pass
        # instead of being lost.
        if not tg(format_message(new)):
            log("telegram failed - state not advanced, will retry")
            return
    else:
        log("no change (" + str(len(rows)) + " tracked)")

    for r in rows:
        state["seen"].setdefault(r["id"], {"at": now, "date": r["date"]})
    save_state(state)


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
