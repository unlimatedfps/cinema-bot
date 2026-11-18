#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-off check for the VOX "Today / Tomorrow" problem. Changes nothing in the bot.

The cinema page labels the first tabs "Today" / "Tomorrow" instead of a date,
and the Today tab points at the bare page (no &d=...), which is the URL that
times out from Termux. This script asks the site for today's and tomorrow's
pages by date instead, and reports what comes back.

It also copies the pages and your current bot file to Downloads so they can
be attached in the chat.

Run:  python test_today.py
"""

import datetime as dt
import pathlib
import re
import shutil
import time

import requests

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO = dt.timezone(dt.timedelta(hours=2))

BASE = "https://egy.voxcinemas.com/showtimes?c=city-centre-almaza"
OUT = pathlib.Path.home() / "storage" / "downloads"

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.I)
TAB_RE = re.compile(r"<a[^>]+[?&]d=(\d{8})[^>]*>(.*?)</a>", re.S | re.I)


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def get(label, url, timeout):
    t0 = time.time()
    try:
        r = S.get(url, timeout=timeout)
        secs = time.time() - t0
        html = r.text
        print("%-9s HTTP %s in %.1fs, %d bytes, %d times on page"
              % (label, r.status_code, secs, len(html), len(TIME_RE.findall(html))))
        return html
    except Exception as e:
        print("%-9s FAILED after %.1fs: %s" % (label, time.time() - t0, type(e).__name__))
        return None


def save(name, text):
    if text is None or not OUT.exists():
        return
    (OUT / name).write_text(text, "utf-8")


now = dt.datetime.now(CAIRO).date()
today = now.strftime("%Y%m%d")
tomorrow = (now + dt.timedelta(days=1)).strftime("%Y%m%d")
print("Cairo date:", now.isoformat(), "\n")

h_today = get("today", BASE + "&d=" + today, 40)
time.sleep(2)
h_tom = get("tomorrow", BASE + "&d=" + tomorrow, 40)

print("\nDay tabs the page shows (date in link -> label on screen):")
seen = set()
for html in (h_today, h_tom):
    for d, label in TAB_RE.findall(html or ""):
        if d not in seen:
            seen.add(d)
            print("  ", d, "->", clean(label)[:40] or "(no text)")
if not seen:
    print("   none found")

if OUT.exists():
    save("vox_today.html", h_today)
    save("vox_tomorrow.html", h_tom)
    bot = pathlib.Path.home() / "vox_bot.py"
    if bot.exists():
        shutil.copy(bot, OUT / "vox_bot_phone.py")
    print("\nSaved to Downloads: vox_today.html, vox_tomorrow.html, vox_bot_phone.py")
else:
    print("\nDownloads not linked. Run  termux-setup-storage  once, allow it, then run this again.")
