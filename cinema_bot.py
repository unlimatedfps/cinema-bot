#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOX Cinemas Egypt -> Telegram notifier for newly published showtimes."""

from __future__ import annotations
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO = dt.timezone(dt.timedelta(hours=2))

BASE         = os.getenv("VOX_BASE", "https://egy.voxcinemas.com").rstrip("/")
DAYS_AHEAD   = int(os.getenv("DAYS_AHEAD", "7"))
STATE_FILE   = pathlib.Path(os.getenv("STATE_FILE", "state.json"))
TOKEN        = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL     = int(os.getenv("INTERVAL_SECONDS", "120"))
RUN_SECONDS  = int(os.getenv("RUN_SECONDS", "0"))
MOVIES_ONLY  = [s.strip().lower() for s in os.getenv("MOVIES", "").split(",") if s.strip()]
CINEMAS_ONLY = [s.strip().lower() for s in os.getenv("CINEMAS", "").split(",") if s.strip()]
MAX_NOTIFY   = int(os.getenv("MAX_NOTIFY", "40"))
DEBUG        = os.getenv("DEBUG", "") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
S = requests.Session()
S.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

TIME_TXT = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(AM|PM|am|pm)?")
ISO_DT   = re.compile(r"(\d{4}-\d{2}-\d{2})[T ]([0-2]\d:[0-5]\d)")

MOVIE_KEYS  = {"moviename", "movietitle", "filmtitle", "filmname", "eventname"}
CINEMA_KEYS = {"cinemaname", "locationname", "venuename", "sitename", "theatrename"}
EXP_KEYS    = {"experience", "format", "screentype", "sessionattribute", "attribute"}
TIME_KEYS   = {"time", "starttime", "showtime", "sessiontime", "start", "showdatetime",
               "datetime", "showtimedate", "sessiondatetime", "startsat"}
LINK_KEYS   = {"url", "link", "bookingurl", "bookinglink", "deeplink", "href"}


def log(*a):
    print(dt.datetime.now(CAIRO).strftime("[%H:%M:%S]"), *a, flush=True)


def norm(v):
    return re.sub(r"\s+", " ", str(v)).strip()


def nkey(k):
    return k.lower().replace("_", "").replace("-", "")


def to_hhmm(raw, fallback_date):
    m = ISO_DT.search(raw)
    if m:
        return m.group(1), m.group(2)
    m = TIME_TXT.search(raw)
    if not m:
        return None, None
    h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return fallback_date, f"{h:02d}:{mi}"


def json_blobs(soup):
    for tag in soup.find_all("script"):
        txt = (tag.string or tag.get_text() or "").strip()
        if len(txt) < 20:
            continue
        if txt.startswith("{") or txt.startswith("["):
            try:
                yield json.loads(txt)
                continue
            except Exception:
                pass
        for m in re.finditer(r"=\s*(\{.{200,}?\})\s*[;\n]", txt, re.S):
            try:
                yield json.loads(m.group(1))
            except Exception:
                pass


def walk_json(node, ctx, out, page_date):
    if isinstance(node, dict):
        local = dict(ctx)
        hint = local.pop("_hint", None)

        for k, v in node.items():
            if not isinstance(v, (str, int, float)):
                continue
            lk, sv = nkey(k), norm(v)
            if not sv or sv.lower() in ("none", "null"):
                continue
            if lk in MOVIE_KEYS:
                local["movie"] = sv
            elif lk in CINEMA_KEYS:
                local["cinema"] = sv
            elif lk in EXP_KEYS and len(sv) < 40:
                local["experience"] = sv
            elif lk in LINK_KEYS and "/" in sv:
                local["link"] = sv
            elif lk in ("title", "name", "slug"):
                if hint == "movie":
                    local.setdefault("movie", sv)
                elif hint == "cinema":
                    local.setdefault("cinema", sv)

        raw_time = None
        for k, v in node.items():
            if isinstance(v, str) and nkey(k) in TIME_KEYS:
                if ISO_DT.search(v) or TIME_TXT.search(v):
                    raw_time = v
                    break
        if raw_time:
            d, hhmm = to_hhmm(raw_time, page_date)
            if hhmm:
                out.append({
                    "date": d or page_date,
                    "time": hhmm,
                    "movie": local.get("movie", "?"),
                    "cinema": local.get("cinema", "?"),
                    "experience": local.get("experience", ""),
                    "link": local.get("link", ""),
                })

        for k, v in node.items():
            if isinstance(v, (dict, list)):
                child = dict(local)
                lk = nkey(k)
                if lk.startswith("movie") or lk.startswith("film"):
                    child["_hint"] = "movie"
                elif lk.startswith("cinema") or lk.startswith("location") or lk.startswith("venue"):
                    child["_hint"] = "cinema"
                walk_json(v, child, out, page_date)

    elif isinstance(node, list):
        for item in node:
            walk_json(item, dict(ctx), out, page_date)


TOKEN_RE = re.compile(
    r"/movies/([a-z0-9\-]{3,})"
    r"|[?&]c=([a-z0-9\-]{3,})"
    r"|>\s*((?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM|am|pm)?)\s*<"
)


def parse_html_fallback(markup, page_date):
    out, movie, cinema = [], "?", "?"
    for m in TOKEN_RE.finditer(markup):
        if m.group(1):
            movie = m.group(1).replace("-", " ").title()
        elif m.group(2):
            cinema = m.group(2).replace("-", " ").title()
        elif m.group(3):
            d, hhmm = to_hhmm(m.group(3), page_date)
            if hhmm and movie != "?":
                out.append({"date": d or page_date, "time": hhmm, "movie": movie,
                            "cinema": cinema, "experience": "", "link": ""})
    return out


def get(url):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
            log("HTTP", r.status_code, "->", url)
        except Exception as e:
            log("fetch error:", e)
        time.sleep(2 + attempt * 3 + random.random())
    return None


def scan_day(date_str):
    for url in (f"{BASE}/showtimes?d={date_str}",
                f"{BASE}/showtimes?date={date_str}",
                f"{BASE}/showtimes"):
        markup = get(url)
        if not markup:
            continue
        soup = BeautifulSoup(markup, "html.parser")
        records = []
        for blob in json_blobs(soup):
            walk_json(blob, {}, records, date_str)
        if not records:
            records = parse_html_fallback(markup, date_str)
        log(f"scan {url} -> {len(records)} records")
        if DEBUG and records:
            log("sample:", json.dumps(records[:3], ensure_ascii=False))
        if records:
            return records
    return []


def collect():
    today = dt.datetime.now(CAIRO).date()
    seen, all_rows = set(), []
    for i in range(DAYS_AHEAD):
        d = (today + dt.timedelta(days=i)).isoformat()
        for r in scan_day(d):
            if MOVIES_ONLY and not any(x in r["movie"].lower() for x in MOVIES_ONLY):
                continue
            if CINEMAS_ONLY and not any(x in r["cinema"].lower() for x in CINEMAS_ONLY):
                continue
            key = f'{r["date"]}|{r["cinema"]}|{r["movie"]}|{r["time"]}|{r["experience"]}'
            if key in seen:
                continue
            seen.add(key)
            r["key"] = key
            all_rows.append(r)
        time.sleep(1.2)
    return all_rows


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"seen": {}, "bootstrapped": False}


def save_state(state):
    today = dt.datetime.now(CAIRO).date()
    cutoff = (today - dt.timedelta(days=1)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if k.split("|")[0] >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), "utf-8")


def tg(text):
    if not TOKEN or not CHAT_ID:
        log("!! TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for i in range(0, len(text), 3800):
        payload = {"chat_id": CHAT_ID, "text": text[i:i + 3800],
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                r = S.post(url, data=payload, timeout=20)
                if r.ok:
                    break
                log("telegram error:", r.status_code, r.text[:200])
            except Exception as e:
                log("telegram exception:", e)
            time.sleep(3 * (attempt + 1))


def format_message(rows):
    by_movie = {}
    for r in rows:
        by_movie.setdefault(r["movie"], []).append(r)
    parts = ["🎬 <b>نزلت " + str(len(rows)) + " حفلة جديدة في VOX مصر</b>"]
    for movie, items in by_movie.items():
        parts.append("\n<b>" + movie + "</b>")
        for r in sorted(items, key=lambda x: (x["date"], x["time"]))[:12]:
            exp = " · " + r["experience"] if r["experience"] else ""
            parts.append("• " + r["date"] + " — " + r["time"] + " — " + r["cinema"] + exp)
        if len(items) > 12:
            parts.append("• … و" + str(len(items) - 12) + " حفلة كمان")
    parts.append("\n🔗 " + BASE + "/showtimes")
    return "\n".join(parts)


def one_pass():
    state = load_state()
    rows = collect()
    if not rows:
        log("no showtimes parsed this pass")
        if not state.get("warned"):
            state["warned"] = True
            save_state(state)
            tg("⚠️ البوت شغال بس مقدرش يقرا مواعيد من موقع VOX. محتاج تظبيط.")
        return
    new = [r for r in rows if r["key"] not in state["seen"]]
    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")
    for r in rows:
        state["seen"].setdefault(r["key"], now)

    if not state.get("bootstrapped"):
        state["bootstrapped"] = True
        save_state(state)
        tg("✅ البوت اشتغل. اتسجّل " + str(len(rows)) + " موعد حالي كخط أساس.\n"
           "من دلوقتي هيجيلك إشعار بأي حفلة جديدة تنزل.")
        return

    save_state(state)
    if new:
        log(str(len(new)) + " new showtimes")
        tg(format_message(new[:MAX_NOTIFY]))
    else:
        log("no change (" + str(len(rows)) + " showtimes tracked)")


def main():
    if "--chatid" in sys.argv:
        r = S.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20)
        print(r.text)
        return
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
