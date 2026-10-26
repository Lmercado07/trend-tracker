"""Trend Radar collector v2. Runs every 10 minutes via GitHub Actions.
Sources: Google Trends (US), news RSS, Bluesky. Also discovers topics before they trend."""
import os, re, json, math, time, html, datetime as dt, xml.etree.ElementTree as ET
import requests

TRENDS_URL = "https://trends.google.com/trending/rss?geo=US"
NEWS_FEEDS = [
    "https://feeds.npr.org/1001/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://www.theguardian.com/us-news/rss",
    "https://abcnews.go.com/abcnews/topstories",
    "https://www.cbsnews.com/latest/rss/main",
    "https://feeds.foxnews.com/foxnews/latest",
]
MAX_TOPICS, MAX_FOUND, KEEP, SERIES, WIN = 25, 10, 288, 144, 30
MIN_SOCIAL, MIN_NEWS = 5, 2   # ignore tiny counts, they are noise
WEIGHTS = {"social": .4, "search": .35, "news": .25}
ORDER = ["social", "search", "news"]
UA = {"User-Agent": "Mozilla/5.0 (trend-radar)"}
BSKY = "https://bsky.social/xrpc/"
STOP = {"fox news", "new york", "new york times", "associated press", "live updates", "breaking news",
        "white house", "united states", "washington post", "bbc news", "cbs news", "abc news"}
CAP = re.compile(r"\b[A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+){1,2}\b")
now = dt.datetime.now(dt.timezone.utc)
health, fails = {}, 0


def get(url, tries=2, headers=None, **kw):
    for i in range(tries):
        try:
            r = requests.get(url, headers={**UA, **(headers or {})}, timeout=20, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            print("fetch failed:", url, e)
            if i + 1 < tries:
                time.sleep(2)


def traffic_to_int(s):
    m = re.match(r"([\d,.]+)\s*([KkMm]?)", s or "")
    return int(float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]) if m else 0


def read_trends():
    r = get(TRENDS_URL)
    if not r:
        return None
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return None
    out = []
    for rank, it in enumerate(root.iter("item"), 1):
        traffic, heads = "", []
        for ch in it:
            tag = ch.tag.split("}")[-1]
            if tag == "approx_traffic":
                traffic = ch.text
            elif tag == "news_item":
                d = {c.tag.split("}")[-1]: html.unescape(c.text or "") for c in ch}
                heads.append(f"{d.get('news_item_title', '')} ({d.get('news_item_source', '')})")
        out.append({"name": (it.findtext("title") or "").strip(), "search": traffic_to_int(traffic),
                    "heads": heads, "rank": rank, "origin": "trends"})
    return out[:MAX_TOPICS] or None


def read_news():
    items, ok = [], 0
    for oi, url in enumerate(NEWS_FEEDS):
        r = get(url)
        if not r:
            continue
        try:
            found = [(html.unescape(i.findtext("title") or ""), html.unescape(i.findtext("description") or ""))
                     for i in ET.fromstring(r.content).iter("item")]
        except ET.ParseError:
            continue
        ok += 1
        items += [{"o": oi, "title": t, "text": (t + " " + d).lower()} for t, d in found]
    health["news_feeds"] = ok >= len(NEWS_FEEDS) // 2
    return items


def news_stats(name, items):
    pats = [re.compile(r"\b" + re.escape(t) + r"\b") for t in re.findall(r"\w+", name.lower()) if len(t) > 1]
    hits = [x for x in items if pats and all(p.search(x["text"]) for p in pats)]
    return len(hits), len({x["o"] for x in hits})


def discover(items, known):
    """Find names that appear in 2+ outlets but are not on Google Trends yet."""
    seen = {}
    for x in items:
        for m in CAP.findall(x["title"]):
            seen.setdefault(m.strip(), set()).add(x["o"])
    kl, out = [k.lower() for k in known], []
    for k, outs in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        low = k.lower()
        if len(outs) < 2 or low in STOP or any(low in n or n in low for n in kl):
            continue
        out.append({"name": k, "search": 0, "heads": [], "rank": None, "origin": "discovered"})
        kl.append(low)
        if len(out) >= MAX_FOUND:
            break
    return out


def bsky_trending():
    r = get("https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics", tries=1, params={"limit": 10})
    try:
        return [t.get("topic", "") for t in r.json().get("topics", [])] if r else []
    except Exception:
        return []


def bsky_login():
    h, p = os.getenv("BSKY_HANDLE"), os.getenv("BSKY_APP_PASSWORD")
    if not (h and p):
        print("No Bluesky login set.")
        return None
    r = requests.post(BSKY + "com.atproto.server.createSession", json={"identifier": h, "password": p}, timeout=20)
    if not r.ok:
        print("Bluesky login failed:", r.text)
        return None
    return r.json()["accessJwt"]


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def bsky_posts(q, token, start, end):
    r = get(BSKY + "app.bsky.feed.searchPosts", tries=1, headers={"Authorization": f"Bearer {token}"},
            params={"q": q, "since": iso(start), "until": iso(end), "limit": 100, "sort": "latest"})
    return r.json().get("posts", []) if r else None


def social(name, token):
    """Post counts for the last three 30-minute windows [now, previous, before that]."""
    global fails
    if not token or fails >= 3:
        return None, []
    q = f'"{name}"' if " " in name else name
    counts, samples = [], []
    for i in range(3):
        end = now - dt.timedelta(minutes=WIN * i)
        posts = bsky_posts(q, token, end - dt.timedelta(minutes=WIN), end)
        if posts is None:
            fails += 1
            return None, []
        counts.append(len(posts))
        if i == 0:
            for p in sorted(posts, key=lambda p: -(p.get("likeCount", 0) + p.get("repostCount", 0)))[:2]:
                h, rk = p.get("author", {}).get("handle", ""), p.get("uri", "").split("/")[-1]
                t = (p.get("record", {}).get("text", "") or "").replace("\n", " ")[:160]
                if h and rk and t:
                    samples.append({"text": t, "url": f"https://bsky.app/profile/{h}/post/{rk}"})
        time.sleep(.1)
    fails = 0
    return counts, samples


def mean(a):
    return sum(a) / len(a) if a else 0


def ffill(a):
    last = next((x for x in a if x is not None), 0)
    out = []
    for x in a:
        last = last if x is None else x
        out.append(last)
    return out


def analyze(c, hist, live, samples, outlets):
    name, n = c["name"], len(hist)
    cols = {k: [h["t"].get(name, [None, 0, 0])[i] for h in hist] for i, k in enumerate(ORDER)}
    sers = {k: ffill(a) for k, a in cols.items()}
    base_len = max(3, int(n * .6))
    sig, onset = {}, {}
    for k, a in sers.items():
        base, recent = mean(a[:base_len]), mean(a[-3:])
        s = math.log2((recent + 1) / (base + 1)) / 3 * 100
        if n >= 2 and min(a[-2:]) <= base:
            s *= .5                      # a single noisy reading counts for less
        if (k == "social" and recent < MIN_SOCIAL) or (k == "news" and recent < MIN_NEWS):
            s = 0
        if k == "social" and live and live[0] >= MIN_SOCIAL:
            s = max(s, math.log2((live[0] + 1) / ((live[1] + live[2]) / 2 + 1)) / 3 * 100)
        sig[k] = max(0, min(100, s))
        onset[k] = next((i for i in range(base_len, n - 2) if mean(a[i:i + 3]) > 1.5 * (base + 1)), None)
    active = sum(v > 25 for v in sig.values())
    score = round(min(100, sum(WEIGHTS[k] * sig[k] for k in sig) * (1 + .25 * max(0, active - 1))))
    vol_ok = bool(live and live[0] >= 15) or outlets >= 3
    conf = "High" if active >= 2 and vol_ok else "Medium" if active >= 1 and (vol_ok or (live and live[0] >= MIN_SOCIAL)) else "Low"
    if conf == "Low":
        score = round(score * .6)
    if sig["news"] > 25 and active > 1:
        stage = "Breaking"
    elif sig["social"] > 25 and sig["search"] > 25:
        stage = "Building"
    elif sig["social"] > 25:
        stage = "Early signal"
    else:
        stage = "Watching"
    if n < 4 and sig["social"] <= 25:
        stage, score = "Collecting data", 0
    present = [h["ts"] for h in hist if name in h["t"]]
    is_new = len(present) <= 3
    why = []
    if live and live[0] >= MIN_SOCIAL:
        why.append(f"{live[0]} Bluesky posts in the last {WIN} min vs {(live[1] + live[2]) / 2:g} per {WIN} min before"
                   + (" (hit the 100-post cap)" if live[0] >= 100 else ""))
    if outlets:
        why.append(f"Covered by {outlets} outlet{'s' if outlets != 1 else ''} in the tracked feeds right now")
    elif sig["social"] > 25:
        why.append("No tracked outlet has covered it yet, so social is ahead of news")
    why.append(f"Google Trends rank #{c['rank']}" + (" (just entered the list)" if is_new else "") if c["origin"] == "trends"
               else "Not on Google Trends yet, so search interest has not started")
    t = lambda i: dt.datetime.fromisoformat(hist[i]["ts"])
    lead = round((t(onset["news"]) - t(onset["social"])).total_seconds() / 60) \
        if onset["social"] is not None and onset["news"] is not None else None
    return {"name": name, "score": score, "stage": stage, "confidence": conf, "origin": c["origin"],
            "is_new": is_new, "first_seen": present[0] if present else None,
            "sig": {k: round(v) for k, v in sig.items()}, "series": sers, "onset": onset,
            "onset_ts": {k: (hist[v]["ts"] if v is not None else None) for k, v in onset.items()},
            "lead_min": lead, "reasons": why, "samples": samples, "headlines": c["heads"][:3]}


def main():
    trends = read_trends()
    health["google_trends"] = trends is not None
    items = read_news()
    try:
        history = [h for h in json.load(open("data/history.json")) if h.get("v") == 2]
    except Exception:
        history = []
    if trends is None:   # keep going with the last known topics
        trends = [{"name": n, "search": None, "heads": [], "rank": None, "origin": "trends"}
                  for n in (history[-1]["t"] if history else {})][:MAX_TOPICS]
    if not trends and not items:
        raise SystemExit("Nothing fetched; leaving data unchanged.")
    found = discover(items, [t["name"] for t in trends])
    known = [t["name"].lower() for t in trends + found]
    for nm in bsky_trending():
        if nm and len(found) < MAX_FOUND + 5 and not any(nm.lower() in k or k in nm.lower() for k in known):
            found.append({"name": nm, "search": 0, "heads": [], "rank": None, "origin": "discovered"})
            known.append(nm.lower())
    token = bsky_login()
    snap, aux = {}, {}
    for c in trends + found:
        live, samples = social(c["name"], token)
        nn, outlets = news_stats(c["name"], items)
        snap[c["name"]] = [live[0] if live else None, c["search"], nn + len(c["heads"])]
        aux[c["name"]] = (live, samples, outlets)
    health["bluesky"] = token is not None and fails < 3
    os.makedirs("data", exist_ok=True)
    history = (history + [{"v": 2, "ts": now.isoformat(), "t": snap}])[-KEEP:]
    json.dump(history, open("data/history.json", "w"), separators=(",", ":"))
    recent = history[-SERIES:]
    rows = sorted((analyze(c, recent, *aux[c["name"]]) for c in trends + found), key=lambda r: -r["score"])
    json.dump({"updated": now.isoformat(), "snapshots": len(history), "health": health, "topics": rows},
              open("data/latest.json", "w"), separators=(",", ":"))
    print(f"Saved {len(rows)} topics ({len(found)} discovered), {len(history)} snapshots. Health: {health}")


if __name__ == "__main__":
    main()
