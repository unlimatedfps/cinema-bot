#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elCinema -> Telegram: alert when a movie gets new showtimes in ANY cinema
in Egypt.

Reads elCinema's "Playing in Cinemas Egypt" page for one movie. That page
opens from GitHub's servers (the booking pages do not: they are Egypt-only),
and it lists every cinema with its times, one tab per day.

One run = one check (GitHub Actions schedule). State: elcinema_state.json.
"""
import datetime as dt
import html as htmllib
import json
import os
import pathlib
import re
import sys
import time

import requests

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO = dt.timezone(dt.timedelta(hours=2))

WORK_ID = os.getenv("ELCINEMA_WORK_ID", "2097036")          # Avengers: Doomsday
MOVIE_LABEL = os.getenv("ELCINEMA_LABEL", "Avengers: Doomsday")
PAGE = "https://elcinema.com/en/work/" + WORK_ID + "/theater/EG/"
STATE_FILE = pathlib.Path(os.getenv("STATE_FILE", "elcinema_state.json"))
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.getenv("DRY_RUN") == "1"
FAILS_BEFORE_ALERT = 6

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TAB_RE = re.compile(r'<div class="content[^"]*" id="wtheater(\d{8})">')
THEATER_RE = re.compile(r'<a href="/en/theater/(\d+)/">\s*([^<]+?)\s*</a>')
TIME_RE = re.compile(r'<li[^>]*>\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*</li>', re.I)

AR_DAYS = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def log(*a):
    print(dt.datetime.now(CAIRO).strftime("[%H:%M:%S]"), *a, flush=True)


def fetch():
    """Page text, or None when it could not be read properly."""
    for attempt in range(3):
        try:
            r = requests.get(PAGE, headers={"User-Agent": UA,
                                            "Accept-Language": "en-US,en;q=0.9"},
                             timeout=30)
            # "Playing in" is the page's own heading; without it we got an
            # error or a changed page, which must not be read as "no shows".
            if r.ok and "Playing in" in r.text:
                return r.text
            log("unexpected page: HTTP", r.status_code, "len", len(r.text))
        except Exception as e:
            log("fetch error:", type(e).__name__, str(e)[:120])
        time.sleep(3 * (attempt + 1))
    return None


def parse(page):
    """[{key, date, theater_id, theater, time}] in page order."""
    page = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    tabs = list(TAB_RE.finditer(page))
    rows, seen = [], set()
    for i, tab in enumerate(tabs):
        day = tab.group(1)
        end = tabs[i + 1].start() if i + 1 < len(tabs) else len(page)
        block = page[tab.end():end]
        # Each cinema is one "row": its link, then its times, then the next
        # cinema's link.
        theaters = list(THEATER_RE.finditer(block))
        for j, th in enumerate(theaters):
            tend = theaters[j + 1].start() if j + 1 < len(theaters) else len(block)
            name = re.sub(r"\s+", " ", htmllib.unescape(th.group(2))).strip()
            for tm in TIME_RE.finditer(block[th.end():tend]):
                h, mi, ap = int(tm.group(1)), tm.group(2), tm.group(3).upper()
                hh = (h % 12) + (12 if ap == "PM" else 0)
                t24 = "%02d:%s" % (hh, mi)
                key = "|".join([day, th.group(1), t24])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"key": key, "date": day, "theater_id": th.group(1),
                             "theater": name, "time": t24})
    return rows


def nice_time(t24):
    h, m = int(t24[:2]), t24[3:]
    return "%d:%s %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def nice_date(d):
    try:
        day = dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        return AR_DAYS[day.weekday()] + " " + day.isoformat()
    except Exception:
        return d


def night_order(t24):
    # Times after midnight (e.g. 1:15 AM) belong to the end of that day's list.
    h = int(t24[:2])
    return (h + 24 if h < 6 else h, t24)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(rows):
    lines = ["🚨 <b>نزلت حفلات " + esc(MOVIE_LABEL) + "!</b>",
             "🎬 " + str(len(rows)) + " حفلة جديدة على السينما.كوم"]
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], {}).setdefault(
            (r["theater"], r["theater_id"]), []).append(r["time"])
    for day in sorted(by_day):
        lines.append("")
        lines.append("📅 <b>" + nice_date(day) + "</b>")
        for (name, tid), times in sorted(by_day[day].items()):
            ts = " · ".join(nice_time(t) for t in sorted(set(times), key=night_order))
            lines.append("🎦 " + esc(name) + ": " + ts)
            lines.append("   🎟 https://elcinema.com/booking/" + WORK_ID + "/" + tid + "/")
    lines.append("")
    lines.append("🔗 " + PAGE)
    return "\n".join(lines)


def chunks(text, limit=3500):
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        out.append(cur)
    return out


def tg(text):
    if DRY_RUN:
        print("----- DRY RUN message -----\n" + text + "\n---------------------------")
        return True
    if not TOKEN or not CHAT_ID:
        log("!! telegram secrets missing")
        return False
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    for part in chunks(text):
        ok = False
        for attempt in range(3):
            try:
                r = requests.post(url, data={"chat_id": CHAT_ID, "text": part,
                                             "parse_mode": "HTML",
                                             "disable_web_page_preview": True},
                                  timeout=20)
                if r.ok:
                    ok = True
                    break
                log("telegram error:", r.status_code, r.text[:200])
            except Exception as e:
                log("telegram exception:", type(e).__name__)
            time.sleep(3 * (attempt + 1))
        if not ok:
            return False
    return True


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            log("!! state file unreadable, refusing to start fresh")
            sys.exit(1)
    return {"seen": {}, "bootstrapped": False, "fails": 0}


def save_state(state):
    if DRY_RUN:
        log("dry run: state not saved")
        return
    cutoff = (dt.datetime.now(CAIRO).date() - dt.timedelta(days=1)).strftime("%Y%m%d")
    state["seen"] = {k: v for k, v in state["seen"].items() if k.split("|")[0] >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1), "utf-8")


def main():
    state = load_state()
    page = fetch()
    if page is None:
        state["fails"] = state.get("fails", 0) + 1
        log("!! page unreadable (%d in a row); nothing changed" % state["fails"])
        if state["fails"] == FAILS_BEFORE_ALERT:
            tg("⚠️ بوت السينما.كوم مقدرش يقرا صفحة " + esc(MOVIE_LABEL)
               + " " + str(FAILS_BEFORE_ALERT) + " مرات ورا بعض. هفضل أحاول.")
        save_state(state)
        return
    state["fails"] = 0
    rows = parse(page)
    log("showtimes on elCinema:", len(rows),
        "| cinemas:", len({r["theater_id"] for r in rows}),
        "| days:", sorted({r["date"] for r in rows}))
    now = dt.datetime.now(CAIRO).isoformat(timespec="seconds")

    if not state.get("bootstrapped"):
        msg = ("✅ <b>بوت السينما.كوم اشتغل على GitHub</b>\n"
               "بيتابع " + esc(MOVIE_LABEL) + " في كل سينمات مصر.\n")
        msg += ("الحفلات الموجودة دلوقتي: " + str(len(rows)) + "، اتسجلت ومش هبعت عنها."
                if rows else "لسه مفيش ولا حفلة. أول ما ينزل حاجة هبعتلك.")
        if not tg(msg):
            log("!! telegram failed; will try again next run")
            return
        for r in rows:
            state["seen"][r["key"]] = now
        state["bootstrapped"] = True
        save_state(state)
        return

    new = [r for r in rows if r["key"] not in state["seen"]]
    if not new:
        log("no change")
        save_state(state)
        return
    log(len(new), "new showtimes")
    # Record only after Telegram accepted the message, so nothing is lost.
    if not tg(build_message(new)):
        log("!! telegram failed; not marked as seen, will retry next run")
        return
    for r in new:
        state["seen"][r["key"]] = now
    save_state(state)


if __name__ == "__main__":
    main()
