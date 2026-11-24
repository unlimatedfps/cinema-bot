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


SHOW_RE = re.compile(
    r'<article[^>]*data-slug="([^"]+)"'
    r'|<strong>\s*([^<]{1,40}?)\s*</strong>'
    r'|class="action showtime[^"]*"[^>]*>\s*((?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM|am|pm)?)\s*<')


def html_fallback(html, page_date):
    out, movie, exp = [], "?", ""
    if "movie-compare" in html:
        for m in SHOW_RE.finditer(html):
            if m.group(1):
                movie, exp = m.group(1).replace("-", " ").title(), ""
            elif m.group(2):
                exp = m.group(2).strip()
            elif m.group(3):
                d, hhmm = to_hhmm(m.group(3), page_date)
                if hhmm and movie != "?":
                    out.append({"date": d or page_date, "time": hhmm, "movie": movie,
                                "cinema": CINEMA, "experience": exp})
        if out:
            return out
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
    import subprocess
    for attempt in range(3):
        try:
            out = subprocess.run(
                ["curl", "-sL", "--compressed", "--max-time", "30", url],
                capture_output=True, timeout=45)
            txt = out.stdout.decode("utf-8", "replace")
            if len(txt) > 5000:
                return txt
            log("curl short:", out.returncode, len(txt))
        except Exception as e:
            log("fetch error:", type(e).__name__)
        time.sleep(2 + attempt * 3 + random.random())
    return None


DATE_LINK_RE = re.compile(r'/showtimes\?c(?:&#x3D;|=)[^"&]+(?:&amp;|&)d(?:&#x3D;|=)(\d{8})', re.I)


def available_days():
    html = fetch(BASE + "/showtimes?c=" + CINEMA)
    days = sorted(set(DATE_LINK_RE.findall(html or "")))
    today = dt.datetime.now(CAIRO).date().strftime("%Y%m%d")
    days = [d for d in days if d >= today]
    if not days:
        t = dt.datetime.now(CAIRO).date()
        days = [(t + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(DAYS_AHEAD)]
        log("!! no date links found, falling back to", len(days), "days")
    else:
        log("available days:", len(days), days[:5], "...")
    # The "Today" tab has no date in its link: today's showtimes are on this
    # landing page itself. Hand it back so collect() can read it.
    return days, html


def collect():
    today = dt.datetime.now(CAIRO).date()
    seen, out = set(), []
    days, landing = available_days()
    today_s = today.strftime("%Y%m%d")
    pages = []
    for d in days:
        html = fetch(BASE + "/showtimes?c=" + CINEMA + "&d=" + d)
        if not html:
            continue
        pages.append((d, parse_page(html, d)))
        log("day", d, "->", len(pages[-1][1]), "showtimes")
        time.sleep(1.5)
    if landing and today_s not in days:
        rows = parse_page(landing, today_s)
        sig = signature(rows)
        same = [d for d, r in pages if r and signature(r) == sig]
        if same:
            # The landing page fell back to another day's listing; do not
            # file those showtimes under today's date.
            log("Today tab = same as day", same[0], "-> skipped")
        else:
            log("day", today_s, "(Today tab) ->", len(rows), "showtimes")
            pages.insert(0, (today_s, rows))
    for d, rows in pages:
        for r in rows:
            key = "|".join([r["date"], r["cinema"], r["movie"], r["time"], r["experience"]])
            if key in seen:
                continue
            seen.add(key)
            r["key"] = key
            out.append(r)
    return out


def parse_page(html, d):
    rows = []
    for blob in json_blobs(html):
        walk(blob, {}, rows, d)
    if not rows:
        rows = html_fallback(html, d)
    return rows


def signature(rows):
    return sorted(set((r["movie"], r["time"], r["experience"]) for r in rows))


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


MSG_LIMIT = 3500
PRIORITY = ["doomsday"]


def tg(text):
    """Send one message. True only if Telegram accepted it."""
    if not TOKEN or not CHAT_ID:
        log("!! telegram secrets missing")
        return False
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            r = S.post(url, data=data, timeout=20)
            if r.ok:
                return True
            log("telegram error:", r.status_code, r.text[:200])
        except Exception as e:
            log("telegram exception:", type(e).__name__)
        # Same trick as fetch(): on this phone curl often gets through
        # when requests times out.
        if tg_curl(url, data):
            return True
        time.sleep(3 * (attempt + 1))
    return False


def tg_curl(url, data):
    import subprocess
    cmd = ["curl", "-s", "--max-time", "30", url]
    for k, v in data.items():
        cmd += ["--data-urlencode", "%s=%s" % (k, v)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=40)
        body = out.stdout.decode("utf-8", "replace")
        if '"ok":true' in body:
            log("telegram sent via curl")
            return True
        log("telegram curl failed:", out.returncode, body[:150])
    except Exception as e:
        log("telegram curl exception:", type(e).__name__)
    return False


def build_messages(rows):
    """Group by movie, then pack whole lines into messages under the limit.
    Nothing is dropped and no HTML tag is cut."""
    by_movie = {}
    for r in rows:
        by_movie.setdefault(r["movie"], []).append(r)
    blocks = []
    def is_vip(m):
        return any(k in m.lower() for k in PRIORITY)
    for movie in sorted(by_movie, key=lambda m: not is_vip(m)):
        items = by_movie[movie]
        head = ("\U0001F525 " if is_vip(movie) else "") + "<b>" + movie + "</b>"
        cur = "\n" + head
        for r in sorted(items, key=lambda x: (x["date"], x["time"])):
            exp = " · " + r["experience"] if r["experience"] else ""
            ln = "\n• " + r["date"] + " — " + r["time"] + exp
            if len(cur) + len(ln) > MSG_LIMIT:
                blocks.append(cur)
                cur = "\n" + head + " (تكملة)"
            cur += ln
        blocks.append(cur)
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > MSG_LIMIT:
            msgs.append(cur)
            cur = ""
        cur += b
    msgs.append(cur)
    head = "\U0001F3AC <b>نزلت " + str(len(rows)) + " حفلة جديدة - الماظة</b>"
    if any(is_vip(m) for m in by_movie):
        head = "\U0001F6A8 <b>نزلت حفلات Avengers: Doomsday!</b>\n" + head
    msgs[0] = head + msgs[0]
    msgs[-1] += "\n\n\U0001F517 " + BASE + "/showtimes?c=" + CINEMA
    return msgs


def one_pass():
    state = load_state()
    rows = collect()
    if not rows:
        log("nothing parsed")
        return
    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")
    if not state.get("bootstrapped"):
        for r in rows:
            state["seen"].setdefault(r["key"], now)
        state["bootstrapped"] = True
        save_state(state)
        tg("\u2705 البوت اشتغل من الموبايل. اتسجّل " + str(len(rows)) + " موعد كخط أساس.")
        return
    if not state.get("formats_v1"):
        # One-time switch: showtimes already seen without a format are
        # filed under their new keys silently, so no flood of old alerts.
        for r in rows:
            old = r["key"].rsplit("|", 1)[0] + "|"
            if old in state["seen"]:
                state["seen"].setdefault(r["key"], state["seen"][old])
        state["formats_v1"] = True
        log("formats enabled")
    new = [r for r in rows if r["key"] not in state["seen"]]
    if not new:
        log("no change (" + str(len(rows)) + " tracked)")
        save_state(state)
        return
    log(str(len(new)) + " new")
    # Send first. Only mark as seen once Telegram has accepted everything,
    # so a failed send is retried on the next pass instead of lost.
    if not all(tg(m) for m in build_messages(new)):
        log("!! telegram failed, not marked as seen; will retry next pass")
        return
    for r in new:
        state["seen"][r["key"]] = now
    save_state(state)


if __name__ == "__main__":
    tg("\U0001F7E2 البوت بدأ الشغل على الموبايل.")
    while True:
        try:
            one_pass()
        except Exception as e:
            log("pass failed:", repr(e))
        time.sleep(INTERVAL)
