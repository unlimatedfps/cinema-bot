#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared Telegram formatting and delivery for both watchers.

Both bots report the same cinema from different sources, so the message they
send should look identical. Keeping it in one place stops the two from
drifting apart.
"""
import datetime as dt
import os
import re
import time

import requests

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO = dt.timezone(dt.timedelta(hours=2))

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

AR_DAYS = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس",
           "الجمعة", "السبت", "الأحد"]
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

# Words a site prints instead of a real date.
WORD_DAYS = {
    "today": 0, "tonight": 0, "اليوم": 0, "النهاردة": 0, "النهارده": 0,
    "tomorrow": 1, "بكرة": 1, "بكره": 1, "غدا": 1, "غداً": 1,
}

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
COMPACT_DATE = re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b")


def log(*a):
    print(dt.datetime.now(CAIRO).strftime("[%H:%M:%S]"), *a, flush=True)


def word_date(raw):
    """'Today' / 'اليوم' / 'Tomorrow' -> a real yyyy-mm-dd in Cairo time.

    Resolved at call time, not at startup, so a bot left running overnight
    still gives the right answer after midnight.
    """
    s = raw.lower()
    for w, delta in WORD_DAYS.items():
        if w in s:
            return (dt.datetime.now(CAIRO).date() + dt.timedelta(days=delta)).isoformat()
    return None


def any_date(raw):
    """Pull a date out of a string in any shape the sites use."""
    m = ISO_DATE.search(raw)
    if m:
        return m.group(0)
    m = COMPACT_DATE.search(raw)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    return word_date(raw)


def pretty_date(iso):
    """2026-11-07 -> 'السبت 7 نوفمبر', with النهاردة/بكرة when it applies."""
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


def h12(hhmm):
    """'19:30' -> '7:30 PM'."""
    h, mi = hhmm.split(":")
    h = int(h)
    return "%d:%s %s" % (h % 12 or 12, mi, "AM" if h < 12 else "PM")


def night_key(hhmm):
    """Sort a cinema day, not a clock day: a 00:15 show is the end of the
    night, so anything before 06:00 sorts after the evening shows."""
    h, mi = hhmm.split(":")
    h = int(h)
    return (h + 24 if h < 6 else h, int(mi))


def format_message(rows, label, link=None):
    """Group by movie, then by date, then by experience — so a date is
    printed once with its times on one line, instead of repeating the raw
    date on every single showtime."""
    parts = ["\U0001F3AC <b>نزلت " + str(len(rows)) + " حفلة جديدة — "
             + label + "</b>"]
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
                by_exp.setdefault(r.get("experience", ""), set()).add(r["time"])
            for exp in sorted(by_exp):
                times = " · ".join(h12(t) for t in sorted(by_exp[exp],
                                                          key=night_key))
                parts.append(("   " + exp + ": " + times) if exp
                             else ("   " + times))
    if link:
        parts.append("")
        parts.append("\U0001F517 " + link)
    return "\n".join(parts)


def chunk_lines(text, limit=3500):
    """Split on line boundaries, never mid-tag: a <b> cut in half makes
    Telegram reject the whole message with HTTP 400."""
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


def tg(text, session=None):
    """Send to Telegram. Returns True only if every chunk went through, so
    the caller can hold back its state and retry instead of losing alerts."""
    if not TOKEN or not CHAT_ID:
        log("!! telegram secrets missing")
        return False
    S = session or requests
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    ok_all = True
    for chunk in chunk_lines(text):
        sent = False
        for attempt in range(3):
            try:
                r = S.post(url, data={"chat_id": CHAT_ID, "text": chunk,
                                      "parse_mode": "HTML",
                                      "disable_web_page_preview": True}, timeout=20)
                if r.ok:
                    sent = True
                    break
                log("telegram error:", r.status_code, r.text[:200])
                if r.status_code == 400:
                    # Malformed HTML would retry forever and block the state.
                    # Resend once as plain text so the alert still arrives.
                    plain = re.sub(r"</?b>", "", chunk)
                    r2 = S.post(url, data={"chat_id": CHAT_ID, "text": plain,
                                           "disable_web_page_preview": True},
                                timeout=20)
                    if r2.ok:
                        sent = True
                        break
            except Exception as e:
                log("telegram exception:", type(e).__name__)
            time.sleep(3 * (attempt + 1))
        if not sent:
            ok_all = False
    return ok_all
