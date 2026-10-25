# gw2events.py: GW2 timed events (world bosses, map metas, day/night) from the wiki's event timer data.
# data/gw2-events.json comes from github.com/giovazz89/gw2-api-event-timers (events.json: the wiki's
# "Widget:Event timer" data, same format); refresh with: python3 gw2events.py --update
# Each section's "sequences" start at 00:00 UTC: first the "partial" entries, then "pattern" repeated;
# r = segment id (0 = nothing), d = minutes.
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gw2-events.json")
SOURCE = "https://raw.githubusercontent.com/giovazz89/gw2-api-event-timers/HEAD/events.json"


def load():
    try:
        return json.load(open(DATA))
    except (OSError, ValueError):
        return []


def timeline(section, day):
    """[(start, end, segment dict)] for one UTC day (datetime at 00:00 UTC), gaps (r 0) left out."""
    seq = section.get("sequences", {})
    segs = {s["id"]: s for s in section.get("segments", [])}
    out, t = [], day
    end_of_day = day + timedelta(days=1)
    entries = list(seq.get("partial", []))
    pattern = seq.get("pattern", [])
    i = 0
    while t < end_of_day:
        if i < len(entries):
            e = entries[i]
        elif pattern:
            e = pattern[(i - len(entries)) % len(pattern)]
        else:
            break
        i += 1
        nxt = t + timedelta(minutes=e["d"])
        if e["r"] and e["r"] in segs:
            out.append((t, nxt, segs[e["r"]]))
        t = nxt
    return out


def window(section, start, end):
    """Events of a section overlapping [start, end) (UTC datetimes), across day borders."""
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while day < end:
        out += [x for x in timeline(section, day) if x[1] > start and x[0] < end]
        day += timedelta(days=1)
    return out


def upcoming(sections, now, horizon_min=180):
    """[(start, end, section name, segment)] of the given sections from now on, sorted by start; events
    already running come first."""
    out = []
    for s in sections:
        for a, b, seg in window(s, now - timedelta(hours=3), now + timedelta(minutes=horizon_min)):
            if b > now:
                out.append((a, b, s["name"], seg))
    return sorted(out, key=lambda x: x[0])


if __name__ == "__main__":
    if "--update" in sys.argv:
        with urllib.request.urlopen(SOURCE, timeout=20) as r:
            data = r.read()
        json.loads(data)
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        with open(DATA, "wb") as fh:
            fh.write(data)
        print("updated", DATA)
    else:
        now = datetime.now(timezone.utc)
        secs = [s for s in load() if s["name"] in sys.argv[1:]] or [s for s in load() if s["name"] == "World bosses"]
        for a, b, name, seg in upcoming(secs, now, 120)[:12]:
            print(f"{a.astimezone():%H:%M}-{b.astimezone():%H:%M}  {name}: {seg['name']}")
