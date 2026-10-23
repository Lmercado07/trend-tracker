"""Trend Radar collector. Runs every 10 minutes via GitHub Actions.
Reads Google Trends (US) + news RSS + Bluesky, saves history, scores topics."""
import os, re, json, math, html, datetime as dt, xml.etree.ElementTree as ET
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
MAX_TOPICS, KEEP, SERIES, WINDOW_MIN = 25, 288, 144, 30
WEIGHTS = {"social": .4, "search": .35, "news": .25}
UA = {"User-Agent": "Mozilla/5.0 (trend-radar)"}
now = dt.datetime.now(dt.timezone.utc)


def get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=20, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        print("fetch failed:", url, e)


def traffic_to_int(s):
    m = re.match(r"([\d,.]+)\s*([KkMm]?)", s or "")
    if not m:
        return 0
    return int(float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()])


def read_trends():
    r = get(TRENDS_URL)
    out = []
    if not r:
        return out
    for it in ET.fromstring(r.content).iter("item"):
        traffic, heads = "", []
        for ch in it:
            tag = ch.tag.split("}")[-1]
            if tag == "approx_traffic":
                traffic = ch.text
            elif tag == "news_item":
                d = {c.tag.split("}")[-1]: html.unescape(c.text or "") for c in ch}
                heads.append(f"{d.get('news_item_title', '')} ({d.get('news_item_source', '')})")
        out.append({"name": (it.findtext("title") or "").strip(),
                    "search": traffic_to_int(traffic), "heads": heads})
    return out[:MAX_TOPICS]


def read_news():
    texts = []
    for url in NEWS_FEEDS:
        r = get(url)
        if not r:
            continue
        try:
            for it in ET.fromstring(r.content).iter("item"):
                texts.append(html.unescape(((it.findtext("title") or "") + " " + (it.findtext("description") or ""))).lower())
        except ET.ParseError:
            print("bad feed:", url)
    return texts


def news_count(name, texts):
    toks = [t for t in re.findall(r"\w+", name.lower()) if len(t) > 1]
    return sum(all(t in x for t in toks) for x in texts) if toks else 0


def bsky_login():
    h, p = os.getenv("BSKY_HANDLE"), os.getenv("BSKY_APP_PASSWORD")
    if not (h and p):
        print("No Bluesky login set: social counts will be 0.")
        return None
    r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                      json={"identifier": h, "password": p}, timeout=20)
    if not r.ok:
        print("Bluesky login failed:", r.text)
        return None
    return r.json()["accessJwt"]


def bsky_count(name, token):
    if not token:
        return 0
    since = (now - dt.timedelta(minutes=WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get("https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                         params={"q": name, "since": since, "limit": 100, "sort": "latest"},
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
        return len(r.json().get("posts", [])) if r.ok else 0
    except Exception as e:
        print("bluesky error:", name, e)
        return 0


def mean(a):
    return sum(a) / len(a) if a else 0


def analyze(name, hist):
    sers, onset, sig = {}, {}, {}
    n = len(hist)
    for i, k in enumerate(WEIGHTS_ORDER):
        sers[k] = [h["t"].get(name, [0, 0, 0])[i] for h in hist]
    base_len = max(3, int(n * .6))
    for k, a in sers.items():
        base, recent = mean(a[:base_len]), mean(a[-3:])
        sig[k] = max(0, min(100, math.log2((recent + 1) / (base + 1)) / 3 * 100))
        onset[k] = next((i for i in range(base_len, n - 2) if mean(a[i:i + 3]) > 1.5 * (base + 1)), None)
    active = sum(v > 25 for v in sig.values())
    score = round(min(100, sum(WEIGHTS[k] * sig[k] for k in sig) * (1 + .25 * max(0, active - 1))))
    if n < 6:
        stage, score = "Collecting data", 0
    elif sig["news"] > 25 and active > 1:
        stage = "Breaking"
    elif sig["social"] > 25 and sig["search"] > 25:
        stage = "Building"
    elif sig["social"] > 25:
        stage = "Early signal"
    else:
        stage = "Watching"
    lead = None
    if onset["social"] is not None and onset["news"] is not None:
        t = lambda i: dt.datetime.fromisoformat(hist[i]["ts"])
        lead = round((t(onset["news"]) - t(onset["social"])).total_seconds() / 60)
    return {"name": name, "score": score, "stage": stage,
            "sig": {k: round(v) for k, v in sig.items()}, "series": sers,
            "onset": onset, "onset_ts": {k: (hist[v]["ts"] if v is not None else None) for k, v in onset.items()},
            "lead_min": lead}


WEIGHTS_ORDER = ["social", "search", "news"]

if __name__ == "__main__":
    topics = read_trends()
    if not topics:
        raise SystemExit("No topics fetched; leaving data unchanged.")
    texts, token = read_news(), bsky_login()
    snap = {}
    for t in topics:
        t["news"] = news_count(t["name"], texts) + len(t["heads"])
        snap[t["name"]] = [bsky_count(t["name"], token), t["search"], t["news"]]
    os.makedirs("data", exist_ok=True)
    try:
        history = json.load(open("data/history.json"))
    except Exception:
        history = []
    history = (history + [{"ts": now.isoformat(), "t": snap}])[-KEEP:]
    json.dump(history, open("data/history.json", "w"), separators=(",", ":"))
    recent = history[-SERIES:]
    rows = []
    for t in topics:
        r = analyze(t["name"], recent)
        r["headlines"] = t["heads"][:3]
        rows.append(r)
    rows.sort(key=lambda r: -r["score"])
    json.dump({"updated": now.isoformat(), "snapshots": len(history), "topics": rows},
              open("data/latest.json", "w"), separators=(",", ":"))
    print(f"Saved {len(rows)} topics, {len(history)} snapshots.")
