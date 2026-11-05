#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOX Egypt (City Centre Almaza) -> Telegram. Runs on your phone via Termux."""

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

BASE = "https://egy.voxcinemas.com"
CINEMA = os.getenv("CINEMA", "city-centre-almaza")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "120"))
STATE_FILE = pathlib.Path(os.getenv("STATE_FILE", "vox_state.json"))
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_NOTIFY = int(os.getenv("MAX_NOTIFY", "40"))

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

TIME_TXT = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(AM|PM|am|pm)?")
ISO_DT = re.compile(r"(\d{4}-\d{2}-\d{2})[T ]([0-2]\d:[0-5]\d)")
MOVIE_KEYS = {"moviename", "movietitle", "filmtitle", "filmname", "eventname"}
CINEMA_KEYS = {"cinemaname", "locationname", "venuename", "sitename", "theatrename"}
EXP_KEYS = {"experience", "format", "screentype", "sessionattribute", "attribute"}
TIME_KEYS = {"time", "starttime", "showtime", "sessiontime", "start", "showdatetime",
             "datetime", "showtimedate", "sessiondatetime", "startsat"}
TOKEN_RE = re.compile(r"/movies/([a-z0-9\-]{3,})"
                      r"|>\s*((?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM|am|pm)?)\s*<")


def log(*a):
    print(dt.datetime.now(CAIRO).strftime("[%H:%M:%S]"), *a, flush=True)


def nkey(k):
    return k.lower().replace("_", "").replace("-", "")


def to_hhmm(raw, fallback):
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
    return fallback, "%02d:%s" % (h, mi)


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
            elif lk in CINEMA_KEYS:
                local["cinema"] = sv
            elif lk in EXP_KEYS and len(sv) < 40:
                local["experience"] = sv
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
                out.append({"date": d or page_date, "time": hhmm,
                            "movie": local.get("movie", "?"),
                            "cinema": local.get("cinema", CINEMA),
                            "experience": local.get("experience", "")})
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                child = dict(local)
                lk = nkey(k)
                if lk.startswith("movie") or lk.startswith("film"):
                    child["_hint"] = "movie"
                elif lk.startswith("cinema") or lk.startswith("venue"):
                    child["_hint"] = "cinema"
                walk(v, child, out, page_date)
    elif isinstance(node, list):
        for it in node:
            walk(it, dict(ctx), out, page_date)


def html_fallback(html, page_date):
    out, movie = [], "?"
    for m in TOKEN_RE.finditer(html):
        if m.group(1):
            movie = m.group(1).replace("-", " ").title()
        elif m.group(2):
            d, hhmm = to_hhmm(m.group(2), page_date)
            if hhmm and movie != "?":
                out.append({"date": d or page_date, "time": hhmm, "movie": movie,
                            "cinema": CINEMA, "experience": ""})
    return out


def fetch(url):
    for attempt in range(2):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            log("HTTP", r.status_code, url)
        except Exception as e:
            log("fetch error:", type(e).__name__)
        time.sleep(3 + attempt * 3 + random.random())
    return None


def collect():
    today = dt.datetime.now(CAIRO).date()
    seen, out = set(), []
    for i in range(DAYS_AHEAD):
        d = (today + dt.timedelta(days=i)).isoformat()
        html = fetch(BASE + "/showtimes?c=" + CINEMA + "&d=" + d)
        if not html:
            continue
        rows = []
        for blob in json_blobs(html):
            walk(blob, {}, rows, d)
        if not rows:
            rows = html_fallback(html, d)
        log("day", d, "->", len(rows), "showtimes")
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
    parts = ["\U0001F3AC <b>نزلت " + str(len(rows)) + " حفلة جديدة - الماظة</b>"]
    for movie, items in by_movie.items():
        parts.append("\n<b>" + movie + "</b>")
        for r in sorted(items, key=lambda x: (x["date"], x["time"]))[:12]:
            exp = " · " + r["experience"] if r["experience"] else ""
            parts.append("• " + r["date"] + " — " + r["time"] + exp)
        if len(items) > 12:
            parts.append("• … و" + str(len(items) - 12) + " حفلة كمان")
    parts.append("\n\U0001F517 " + BASE + "/showtimes?c=" + CINEMA)
    return "\n".join(parts)


def one_pass():
    state = load_state()
    rows = collect()
    if not rows:
        log("nothing parsed")
        return
    new = [r for r in rows if r["key"] not in state["seen"]]
    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")
    for r in rows:
        state["seen"].setdefault(r["key"], now)
    if not state.get("bootstrapped"):
        state["bootstrapped"] = True
        save_state(state)
        tg("\u2705 البوت اشتغل من الموبايل. اتسجّل " + str(len(rows)) + " موعد كخط أساس.")
        return
    save_state(state)
    if new:
        log(str(len(new)) + " new")
        tg(format_message(new[:MAX_NOTIFY]))
    else:
        log("no change (" + str(len(rows)) + " tracked)")


if __name__ == "__main__":
    tg("\U0001F7E2 البوت بدأ الشغل على الموبايل.")
    while True:
        try:
            one_pass()
        except Exception as e:
            log("pass failed:", repr(e))
        time.sleep(INTERVAL)
