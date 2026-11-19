#!/usr/bin/env python3
"""
Build a subscribable ICS feed from the Redwood City Parks, Recreation &
Community Services calendar.

The city site has no calendar-wide feed. This script:
  1. Reads the calendar month pages (this month + MONTHS_AHEAD). Each page gives
     every event's date, start time, title and ID. That alone is enough for a feed.
  2. Enriches events with end time, location and description from each event's
     own .ics export. Those requests are slow, sequential, capped per run, and
     cached in enrich_cache.json so each event is fetched only once. The city's
     CDN (Akamai) rate-limits bursts, so on the first refusal enrichment stops
     for that run and the feed is written with what is available.
  3. Writes one VCALENDAR with stable UIDs and Pacific time.

Standard library only. Usage:
    python build_feed.py                       # writes calendar.ics
    python build_feed.py -o out.ics --max-enrich 0
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

SITE = "https://www.redwoodcity.org"

# Calendars to include: (month-page base URL, label).
CALENDARS = [
    (SITE + "/departments/parks-recreation-and-community-services/calendar", "Parks & Rec"),
    # (SITE + "/residents/city-events-calendar", "City Events"),
]

MONTHS_AHEAD = 2           # current month + this many following months
MAX_ENRICH_PER_RUN = 60    # per-event .ics fetches per run (spreads load over days)
ENRICH_DELAY = 2.0         # seconds between per-event fetches
PAGE_DELAY = 3.0           # seconds between month pages
DEFAULT_DURATION = dt.timedelta(hours=1)   # used until an event is enriched
CACHE_FILE = "enrich_cache.json"
CAL_NAME = "Redwood City Parks & Rec"
TZID = "America/Los_Angeles"
TZ = ZoneInfo(TZID)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
TZNAME:PDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
TZNAME:PST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


class Blocked(Exception):
    pass


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"HTTP {e.code} for {url}") from e
        raise
    if "<TITLE>Access Denied</TITLE>" in body:
        raise Blocked(f"Access Denied page for {url}")
    return body


def month_urls(base, today, months_ahead):
    y, m = today.year, today.month
    for _ in range(months_ahead + 1):
        yield f"{base}/-curm-{m}/-cury-{y}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


# ---------- month page parsing ----------

DAY_RE = re.compile(
    r'aria-label="Scheduled events, \w+, (\w+ \d{1,2}, \d{4})"(.*?)</td>', re.S)
ITEM_RE = re.compile(
    r'<div class="calendar_item">(?:<span class="calendar_eventtime">([^<]*)</span>)?'
    r'<a class="calendar_eventlink" href="/Home/Components/Calendar/Event/(\d+)/[^"]*"'
    r'[^>]*>(.*?)</a>', re.S)


def parse_month(page_html):
    """Return list of dicts: id, date, time (or None), title."""
    events = []
    for date_str, cell in DAY_RE.findall(page_html):
        day = dt.datetime.strptime(date_str, "%B %d, %Y").date()
        for time_str, eid, title in ITEM_RE.findall(cell):
            t = None
            time_str = time_str.strip()
            if time_str:
                try:
                    t = dt.datetime.strptime(time_str, "%I:%M %p").time()
                except ValueError:
                    t = None
            events.append({"id": eid, "date": day, "time": t,
                           "title": html.unescape(re.sub(r"<[^>]+>", "", title)).strip()})
    return events


# ---------- per-event .ics parsing ----------

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def parse_vevent(ics_text):
    lines = unfold(ics_text).splitlines()
    props, inside = {}, False
    for line in lines:
        if line == "BEGIN:VEVENT":
            inside = True
            continue
        if line == "END:VEVENT":
            break
        if not inside or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *params = head.split(";")
        props.setdefault(name.upper(), (params, value))
    return props


def ics_unescape(s):
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def ics_escape(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n"))


def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h\d|li)\s*>", "\n", s)
    s = re.sub(r'(?is)<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def enrichment_from_ics(ics_text):
    p = parse_vevent(ics_text)
    if "DTSTART" not in p:
        return None
    return {
        "dtstart": p["DTSTART"][1].strip(),
        "dtend": p.get("DTEND", ([], ""))[1].strip(),
        "location": ics_unescape(p.get("LOCATION", ([], ""))[1]),
        "description": html_to_text(ics_unescape(p.get("DESCRIPTION", ([], ""))[1])),
        "dtstamp": p.get("DTSTAMP", ([], ""))[1].strip(),
    }


# ---------- output ----------

def fold(line):
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 75:
            out.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def fmt_local(d):
    return d.strftime("%Y%m%dT%H%M%S")


def ics_time_prop(name, value):
    """value: floating local 'YYYYMMDDTHHMMSS', date 'YYYYMMDD', or UTC '...Z'."""
    if re.fullmatch(r"\d{8}", value):
        return f"{name};VALUE=DATE:{value}"
    if value.endswith("Z"):
        return f"{name}:{value}"
    return f"{name};TZID={TZID}:{value}"


def stamp_utc(local_floating, fallback):
    if re.fullmatch(r"\d{8}T\d{6}", local_floating or ""):
        d = dt.datetime.strptime(local_floating, "%Y%m%dT%H%M%S").replace(tzinfo=TZ)
        return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return fallback


def build_vevent(ev, enr, label, fallback_stamp):
    url = f"{SITE}/Home/Components/Calendar/Event/{ev['id']}/"
    lines = ["BEGIN:VEVENT", f"UID:rwc-event-{ev['id']}@redwoodcity.org"]

    if enr and enr.get("dtstart"):
        lines.append("DTSTAMP:" + stamp_utc(enr.get("dtstamp"), fallback_stamp))
        lines.append(ics_time_prop("DTSTART", enr["dtstart"]))
        if enr.get("dtend"):
            lines.append(ics_time_prop("DTEND", enr["dtend"]))
    else:
        lines.append("DTSTAMP:" + fallback_stamp)
        if ev["time"] is None:
            lines.append(ics_time_prop("DTSTART", ev["date"].strftime("%Y%m%d")))
            lines.append(ics_time_prop("DTEND", (ev["date"] + dt.timedelta(days=1)).strftime("%Y%m%d")))
        else:
            start = dt.datetime.combine(ev["date"], ev["time"])
            lines.append(ics_time_prop("DTSTART", fmt_local(start)))
            lines.append(ics_time_prop("DTEND", fmt_local(start + DEFAULT_DURATION)))

    lines.append("SUMMARY:" + ics_escape(ev["title"]))
    if enr and enr.get("location"):
        lines.append("LOCATION:" + ics_escape(enr["location"]))
    desc = (enr or {}).get("description", "")
    if not enr:
        desc = "End time and location not loaded yet; see the event page."
    desc = (desc + "\n\n" if desc else "") + f"Event page: {url}"
    lines.append("DESCRIPTION:" + ics_escape(desc))
    lines.append("URL:" + url)
    lines.append("CATEGORIES:" + ics_escape(label))
    lines.append("END:VEVENT")
    return lines


def build_calendar(vevents):
    out = ["BEGIN:VCALENDAR", "VERSION:2.0",
           "PRODID:-//personal//Redwood City calendar feed//EN",
           "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
           f"X-WR-CALNAME:{CAL_NAME}", f"X-WR-TIMEZONE:{TZID}",
           "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
           *VTIMEZONE.splitlines()]
    for ev in vevents:
        out.extend(ev)
    out.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in out) + "\r\n"


def load_cache(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="calendar.ics")
    ap.add_argument("--cache", default=CACHE_FILE)
    ap.add_argument("--max-enrich", type=int, default=MAX_ENRICH_PER_RUN)
    args = ap.parse_args()

    fallback_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    today = dt.datetime.now(TZ).date()
    cache = load_cache(args.cache)

    # 1. month pages
    events, labels, seen = [], {}, set()
    for base, label in CALENDARS:
        for murl in month_urls(base, today, MONTHS_AHEAD):
            try:
                found = parse_month(fetch(murl))
            except Exception as e:  # noqa: BLE001
                print(f"WARN month page failed: {murl}: {e}", file=sys.stderr)
                found = []
            print(f"{murl}: {len(found)} events")
            for ev in found:
                if ev["id"] not in seen:
                    seen.add(ev["id"])
                    events.append(ev)
                    labels[ev["id"]] = label
            time.sleep(PAGE_DELAY)

    if not events:
        print("ERROR: no events collected; leaving existing feed untouched", file=sys.stderr)
        sys.exit(1)

    # 2. enrichment, soonest events first.
    # Invalidate cached details when the month page shows a different start
    # (event rescheduled), so the stale time is not kept.
    for e in events:
        c = cache.get(e["id"])
        if c and e["time"] is not None:
            listed = dt.datetime.combine(e["date"], e["time"]).strftime("%Y%m%dT%H%M")
            if not c.get("dtstart", "").startswith(listed):
                del cache[e["id"]]
    todo = sorted((e for e in events if e["id"] not in cache), key=lambda e: e["date"])
    done = 0
    for ev in todo[:max(args.max_enrich, 0)]:
        try:
            enr = enrichment_from_ics(fetch(
                f"{SITE}/Home/Components/Calendar/Event/ICalendar?ID={ev['id']}&IsPublished=True"))
        except Blocked as e:
            print(f"WARN rate-limited, stopping enrichment for this run: {e}", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001
            print(f"WARN event {ev['id']} failed: {e}", file=sys.stderr)
            enr = None
        if enr:
            cache[ev["id"]] = enr
            done += 1
        time.sleep(ENRICH_DELAY)

    # drop cache entries for events no longer listed
    cache = {k: v for k, v in cache.items() if k in seen}
    with open(args.cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)

    # 3. write feed
    vevents = [build_vevent(e, cache.get(e["id"]), labels[e["id"]], fallback_stamp)
               for e in sorted(events, key=lambda e: (e["date"], e["time"] or dt.time(0)))]
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(build_calendar(vevents))
    pending = sum(1 for e in events if e["id"] not in cache)
    print(f"Wrote {len(vevents)} events to {args.output}; "
          f"enriched {done} this run, {pending} still pending")


if __name__ == "__main__":
    main()
