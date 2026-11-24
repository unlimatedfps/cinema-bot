#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tazkarti -> Telegram: alert when a new Al Ahly match appears with tickets.

Runs once per call (GitHub Actions schedule). State lives in ahly_state.json,
which the workflow commits back to the repo.
"""

import json
import os
import pathlib
import sys
import time

import requests

LIST_URL = "https://www.tazkarti.com/data/matches-list-json.json"
SITE = "https://www.tazkarti.com/#/matches"
STATE_FILE = pathlib.Path(os.getenv("STATE_FILE", "ahly_state.json"))
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.getenv("DRY_RUN") == "1"

# Al Ahly's id on Tazkarti is 77; the names are a fallback in case it changes.
TEAM_IDS = {77}
TEAM_NAMES = {"al ahly fc", "al ahly", "النادي الأهلى", "النادي الأهلي"}
# Tazkarti uses the same team for every sport; tournament.gameId 1 is football
# (3 is basketball). A match with no gameId is kept, so football is never missed.
FOOTBALL_GAME_ID = 1

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def fetch_matches():
    """Return the match list, or None if the site gave us anything unexpected."""
    for attempt in range(3):
        try:
            r = requests.get(LIST_URL, params={"_": int(time.time() * 1000)},
                             headers={"User-Agent": UA, "Cache-Control": "no-cache"},
                             timeout=30)
            if r.ok:
                data = json.loads(r.content.decode("utf-8-sig"))
                if isinstance(data, list):
                    return data
                log("unexpected payload type:", type(data).__name__)
            else:
                log("http", r.status_code)
        except Exception as e:
            log("fetch error:", type(e).__name__, str(e)[:120])
        time.sleep(3 * (attempt + 1))
    return None


def is_football(m):
    gid = (m.get("tournament") or {}).get("gameId")
    return gid is None or gid == FOOTBALL_GAME_ID


def is_ahly(m):
    for side in ("1", "2"):
        if m.get("teamId" + side) in TEAM_IDS:
            return True
        for key in ("teamName" + side, "teamNameAr" + side):
            name = (m.get(key) or "").strip().lower()
            if name in TEAM_NAMES:
                return True
    return False


def nice_dt(iso):
    """'2026-11-27T19:00:00' -> ('2026-11-27', '7:00 PM')."""
    try:
        d, t = iso.split("T")
        h, mi = int(t[:2]), t[3:5]
        return d, "%d:%s %s" % (h % 12 or 12, mi, "AM" if h < 12 else "PM")
    except Exception:
        return str(iso), ""


def describe(m):
    t1 = m.get("teamNameAr1") or m.get("teamName1") or "?"
    t2 = m.get("teamNameAr2") or m.get("teamName2") or "?"
    d, t = nice_dt(m.get("kickOffTime") or m.get("date") or "")
    tour = (m.get("tournament") or {})
    tour_name = tour.get("nameAr") or tour.get("nameEn") or ""
    rnd = m.get("roundNameAr") or m.get("teamGroupNameAr") or m.get("roundName") or ""
    stadium = m.get("stadiumNameAr") or m.get("stadiumName") or ""
    lines = ["⚽ <b>" + esc(t1) + " × " + esc(t2) + "</b>",
             "📅 " + d + (" — " + t if t else "")]
    if tour_name:
        lines.append("🏆 " + esc(tour_name) + (" · " + esc(rnd.strip()) if rnd.strip() else ""))
    if stadium:
        lines.append("🏟 " + esc(stadium))
    return "\n".join(lines)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg(text):
    if DRY_RUN:
        print("----- DRY RUN message -----\n" + text + "\n---------------------------")
        return True
    if not TOKEN or not CHAT_ID:
        log("!! telegram secrets missing")
        return False
    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=20)
            if r.ok:
                return True
            log("telegram error:", r.status_code, r.text[:200])
        except Exception as e:
            log("telegram exception:", type(e).__name__)
        time.sleep(3 * (attempt + 1))
    return False


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            log("!! state file unreadable, refusing to start fresh")
            sys.exit(1)
    return {"seen": {}, "bootstrapped": False}


def save_state(state):
    if DRY_RUN:
        log("dry run: state not saved")
        return
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1), "utf-8")


def main():
    matches = fetch_matches()
    if matches is None:
        log("!! could not read Tazkarti this time; nothing changed")
        return
    ahly = [m for m in matches
            if is_ahly(m) and is_football(m) and m.get("matchId") is not None]
    log("matches on Tazkarti:", len(matches), "| Al Ahly football:", len(ahly))
    state = load_state()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not state.get("bootstrapped"):
        body = ("✅ <b>بوت تذاكر الأهلي اشتغل على GitHub</b>\n"
                "هبعتلك أول ما ينزل ماتش جديد للأهلي على تذكرتي.")
        if ahly:
            body += "\n\nالمتاح دلوقتي:\n\n" + "\n\n".join(describe(m) for m in ahly)
        body += "\n\n🔗 " + SITE
        if not tg(body):
            log("!! telegram failed; will try again next run")
            return
        for m in ahly:
            state["seen"][str(m["matchId"])] = now
        state["bootstrapped"] = True
        save_state(state)
        return

    new = [m for m in ahly if str(m["matchId"]) not in state["seen"]]
    if not new:
        log("no new Al Ahly match")
        return
    log(len(new), "new Al Ahly match(es)")
    text = ("🚨 <b>نزلت تذاكر ماتش للأهلي!</b>\n\n"
            + "\n\n".join(describe(m) for m in new)
            + "\n\n🔗 " + SITE)
    # Mark as seen only after Telegram accepted the message, so a failed
    # send is retried on the next run instead of being lost.
    if not tg(text):
        log("!! telegram failed; not marked as seen, will retry next run")
        return
    for m in new:
        state["seen"][str(m["matchId"])] = now
    save_state(state)


if __name__ == "__main__":
    main()
