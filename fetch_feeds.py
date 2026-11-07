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
# Zusatzquelle für die Zeit: zeit.de sperrt Abrufe der GitHub-Server (403), und der Zeit-Feed
# enthält nur 15 Meldungen. Google News liefert die aktuellen zeit.de-Artikel; sie werden
# unten in den Zeit-Feed übernommen.
# Gleiches für die SZ, deren Artikelseiten den Abruf teilweise sperren.
EXTRA_GN = {
    "https://newsfeed.zeit.de/index": ("zeit.de", r"\s+-\s+(DIE )?ZEIT( ONLINE)?$"),
    "https://rss.sueddeutsche.de/rss/Topthemen": ("sueddeutsche.de", r"\s+-\s+(SZ\.de|Süddeutsche Zeitung)(\s+-\s+SZ\.de)?$"),
}
def gn_url(domain):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote("site:%s when:1d" % domain) + "&hl=de&gl=DE&ceid=DE:de"
for _d, _ in EXTRA_GN.values():
    urls.append(gn_url(_d))
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

# Google-News-Treffer in den Feed der jeweiligen Quelle übernehmen (ohne Doppelte)
for feed_url, (domain, suffix) in EXTRA_GN.items():
    zg = feeds.pop(gn_url(domain), None)
    if not (zg and zg["ok"] and feed_url in feeds and feeds[feed_url]["ok"]):
        continue
    have = {urllib.parse.urlsplit(it["link"]).path.rstrip("/") for it in feeds[feed_url]["items"]}
    extra = 0
    for it in zg["items"]:
        u = urllib.parse.urlsplit(it["link"])
        if u.netloc.endswith(domain) and u.path.rstrip("/") not in have:
            it["title"] = re.sub(suffix, "", re.sub(r"\s+-\s+[^-]{2,40}$", lambda m: m.group(0) if not re.search(suffix, m.group(0), re.I) else "", it["title"]), flags=re.I)
            it["via"] = ""
            if it["teaser"].startswith(it["title"][:30]): it["teaser"] = ""
            feeds[feed_url]["items"].append(it); have.add(u.path.rstrip("/")); extra += 1
    print("%s: %d zusätzliche Artikel über Google News" % (domain, extra))

# ---------- Startseiten auslesen ----------
# Die Feeds von Zeit und SZ enthalten nur die 15 neuesten Meldungen, nicht die Aufmacher.
# Darum werden die Startseiten gelesen: Reihenfolge = "Top"-Rang. Artikel, die in keinem
# Feed stehen, werden mit Überschrift und Teaser aus ihren og:-Metadaten ergänzt.
HOMEPAGES = [
    # (Feed-Adresse der Quelle, Startseite, Muster für Artikeladressen)
    ("https://newsfeed.zeit.de/index", "https://www.zeit.de/index",
     r"^https://www\.zeit\.de/(?:[a-z0-9-]+/)*(?:20\d\d-\d\d|20\d\d/\d\d)/[a-z0-9-]{8,}$"),
    ("https://rss.sueddeutsche.de/rss/Topthemen", "https://www.sueddeutsche.de/",
     r"^https://www\.sueddeutsche\.de/[a-z0-9/-]+-(?:li|szm)\.\d+$"),
    ("https://www.spiegel.de/schlagzeilen/index.rss", "https://www.spiegel.de/",
     r"^https://www\.spiegel\.de/[a-z0-9/-]+-a-[0-9a-f-]{36}$"),
    ("https://www.tagesschau.de/index~rss2.xml", "https://www.tagesschau.de/",
     r"^https://www\.tagesschau\.de/(?:[a-z0-9-]+/)+[a-z0-9-]+-\d+\.html$"),
]
TOP_N = 30
BROWSER_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "de-DE,de;q=0.9"}

meta_cache = {}
try:
    for f in prev.get("feeds", {}).values():
        for it in f.get("items", []) or []:
            if it.get("hp"):
                meta_cache[it["link"]] = it
except NameError:
    pass

def norm_path(u):
    try:
        p = urllib.parse.urlsplit(u)
        return (p.netloc.replace("www.", ""), p.path.rstrip("/"))
    except Exception:
        return u

def meta(h, *names):
    for n in names:
        m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(n), h, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(n), h, re.I))
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    return ""

def article_meta(url):
    h = http(url, headers=BROWSER_HEADERS)
    title = meta(h, "og:title", "twitter:title")
    if not title:
        return None
    title = re.sub(r"\s*[|–-]\s*(ZEIT ONLINE|DIE ZEIT|SZ\.de|Süddeutsche Zeitung|DER SPIEGEL|tagesschau\.de)\s*$", "", title)
    date = meta(h, "article:published_time", "date", "og:article:published_time")
    if not date:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', h)
        date = m.group(1) if m else ""
    sec = meta(h, "article:section")
    return {"title": title, "teaser": meta(h, "og:description", "description")[:260], "link": url,
            "date": parse_date(date), "cats": [sec.lower()] if sec else [], "via": "", "hp": True}

homepages = {}
for feed_url, home, pattern in HOMEPAGES:
    info = {"ok": False}
    try:
        page = http(home, headers=BROWSER_HEADERS)
        rx, seen, links = re.compile(pattern), set(), []
        for href in re.findall(r'href=["\']([^"\'#?]+)', page):
            u = urllib.parse.urljoin(home, html.unescape(href)).rstrip("/")
            if rx.match(u) and norm_path(u) not in seen:
                seen.add(norm_path(u)); links.append(u)
            if len(links) >= TOP_N:
                break
        f = feeds.get(feed_url)
        if f is None or not f["ok"]:
            f = feeds[feed_url] = {"ok": True, "items": []}
        by_path = {norm_path(it["link"]): it for it in f["items"]}
        for other in feeds.values():            # auch in anderen Feeds derselben Seite suchen (z. B. Ressort-Feeds)
            for it in other.get("items", []) if other.get("ok") else []:
                by_path.setdefault(norm_path(it["link"]), it)
        missing = [u for u in links if norm_path(u) not in by_path]
        new_meta = {}
        to_fetch = [u for u in missing if u not in meta_cache]
        errs = []
        def safe_meta(u):
            try:
                m = article_meta(u)
                if not m: errs.append("keine og:-Daten " + u[-60:])
                return m
            except Exception as ex:
                errs.append(str(ex)[:60] + " " + u[-60:])
                return None
        for u in to_fetch:                     # nacheinander, sonst sperrt z. B. sz.de mit 403
            m = safe_meta(u)
            if m:
                new_meta[u] = m
            time.sleep(1.2)
        added = 0
        for rank, u in enumerate(links, 1):
            it = by_path.get(norm_path(u))
            if it is None:
                m = new_meta.get(u) or meta_cache.get(u)
                if not m:
                    continue
                it = dict(m); f["items"].append(it); by_path[norm_path(u)] = it; added += 1
            it["top"] = rank
        info = {"ok": True, "links": len(links), "inFeeds": len(links) - len(missing), "added": added, "metaFailed": len(errs), "errors": errs[:3]}
    except Exception as ex:
        info["error"] = str(ex)[:160]
    homepages[home] = info
    print("Startseite", home, info)

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
with open(out_dir + "/feeds.json", "w", encoding="utf-8") as fh:
    json.dump({"at": int(time.time() * 1000), "feeds": feeds, "homepages": homepages}, fh, ensure_ascii=False, separators=(",", ":"))
