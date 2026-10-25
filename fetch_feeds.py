"""Holt alle Standardquellen aus index.html und schreibt site/feeds.json."""
import email.utils, html, json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import xml.etree.ElementTree as ET

src = open("index.html", encoding="utf-8").read()
block = src[src.index("const DEFAULT_SOURCES"):src.index("];", src.index("const DEFAULT_SOURCES"))]
urls = re.findall(r'url:"([^"]+)"', block)
for q in re.findall(r'url:GN\("([^"]+)"\)', block):
    urls.append("https://news.google.com/rss/search?q=" + urllib.parse.quote(q, safe="-_.!~*'()") + "&hl=de&gl=DE&ceid=DE:de")
try:
    urls += [l.strip() for l in open("extra-feeds.txt", encoding="utf-8") if l.strip().startswith("http")]
except FileNotFoundError:
    pass

def local(tag):
    return tag.rsplit("}", 1)[-1]

def kid(el, names):
    for n in names:
        for c in el:
            if local(c.tag) == n and (c.text or "").strip():
                return c.text.strip()
    return ""

def to_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def parse_date(s):
    if not s:
        return int(time.time() * 1000)
    try:
        return int(email.utils.parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)

def parse(xml):
    root = ET.fromstring(xml)
    out = []
    for e in root.iter():
        if local(e.tag) not in ("item", "entry"):
            continue
        title = to_text(kid(e, ["title"]))
        link = kid(e, ["link"])
        if not link:
            for c in e:
                if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
                    link = c.get("href"); break
        if not link:
            link = kid(e, ["guid", "id"])
        if not title or not link.startswith("http"):
            continue
        teaser = to_text(kid(e, ["description", "summary", "encoded", "content"]))
        if len(teaser) > 260:
            teaser = re.sub(r"\s\S*$", "", teaser[:250]) + " …"
        cats = [(c.text or c.get("term") or "").lower() for c in e if local(c.tag) in ("category", "subject")]
        out.append({"title": title, "teaser": teaser, "link": link,
                    "date": parse_date(kid(e, ["pubDate", "date", "published", "updated"])),
                    "cats": [c for c in cats if c][:5], "via": kid(e, ["source"])})
    return out[:80]

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 Lagebild-Feedreader",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
        with urllib.request.urlopen(req, timeout=25) as r:
            items = parse(r.read())
        return url, {"ok": True, "items": items}
    except Exception as ex:
        return url, {"ok": False, "error": str(ex)[:160]}

with ThreadPoolExecutor(8) as pool:
    feeds = dict(pool.map(fetch, urls))

for u, f in feeds.items():
    print(("OK   %3d" % len(f["items"])) if f["ok"] else "FAIL    ", u, "" if f["ok"] else f["error"])

# ---------- Google-News-Weiterleitungen in echte Artikeladressen auflösen ----------
# Bereits aufgelöste Links aus der zuletzt veröffentlichten feeds.json übernehmen,
# damit pro Lauf nur neue Meldungen bei Google nachgefragt werden.
GN_ID = re.compile(r"news\.google\.com/(?:rss/)?articles/([A-Za-z0-9_-]+)")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
known = {}
try:
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
    prev_url = "https://%s.github.io/%s/feeds.json" % (owner.lower(), repo)
    with urllib.request.urlopen(urllib.request.Request(prev_url, headers={"User-Agent": UA}), timeout=20) as r:
        prev = json.load(r)
    for f in prev.get("feeds", {}).values():
        for it in f.get("items", []) or []:
            if it.get("gn") and "news.google." not in it["link"]:
                known[it["gn"]] = it["link"]
    print("Cache: %d aufgelöste Google-Links übernommen" % len(known))
except Exception as ex:
    print("Cache nicht verfügbar:", ex)

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")

def gn_resolve(aid):
    sig = ts = None
    for page in ("https://news.google.com/articles/" + aid, "https://news.google.com/rss/articles/" + aid):
        try:
            h = http(page)
        except Exception:
            continue
        ms, mt = re.search(r'data-n-a-sg="([^"]+)"', h), re.search(r'data-n-a-ts="([^"]+)"', h)
        if ms and mt:
            sig, ts = ms.group(1), mt.group(1); break
    if not sig:
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]') % (aid, ts, sig)
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
    txt = http("https://news.google.com/_/DotsSplashUi/data/batchexecute", body.encode(),
               {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    data = json.loads(txt.split("\n\n", 1)[1])[:-2]
    url = json.loads(data[0][2])[1]
    return url if isinstance(url, str) and url.startswith("http") else None

resolved = failed = 0
budget = 150
for f in feeds.values():
    for it in f.get("items", []) if f["ok"] else []:
        m = GN_ID.search(it["link"])
        if not m:
            continue
        gn = it["link"]; it["gn"] = gn
        if gn in known:
            it["link"] = known[gn]; continue
        if budget <= 0:
            continue
        budget -= 1
        try:
            real = gn_resolve(m.group(1))
        except Exception:
            real = None
        if real:
            it["link"] = known[gn] = real; resolved += 1
        else:
            failed += 1
        time.sleep(0.4)
print("Google-Links: %d neu aufgelöst, %d nicht auflösbar" % (resolved, failed))

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
with open(out_dir + "/feeds.json", "w", encoding="utf-8") as fh:
    json.dump({"at": int(time.time() * 1000), "feeds": feeds}, fh, ensure_ascii=False, separators=(",", ":"))
