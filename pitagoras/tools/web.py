from __future__ import annotations

import logging
import re
import time
import webbrowser
from urllib.parse import quote_plus

import httpx

from . import tool

log_web = logging.getLogger("web")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


# the latest search results, so "open that page" / "otwórz drugi" can open one of them
LAST_RESULTS: list[dict] = []


def search(query: str, max_results: int = 5, region: str = "wt-wt", remember: bool = False) -> list[dict]:
    from ddgs import DDGS

    results = list(DDGS().text(query, region=region, max_results=max_results) or [])
    if remember and results:
        LAST_RESULTS[:] = [{"title": r.get("title", ""), "url": r.get("href", "")} for r in results if r.get("href")]
    return results


@tool("Search the internet. Use it for news, facts you are unsure about, weather, prices, etc.", query="search query")
def web_search(query: str) -> str:
    results = []
    for attempt in range(3):  # the search engine is flaky (timeouts, DNS hiccups): retry quietly
        try:
            results = search(query, 5, remember=True)
        except Exception:  # noqa: BLE001
            results = []
        if results:
            break
        time.sleep(0.6 * (attempt + 1))
    if not results:
        return "No results (the search didn't work right now)."
    return ("\n\n".join(f"[{i+1}] {r.get('title')}\n{r.get('href')}\n{r.get('body')}" for i, r in enumerate(results))
            + "\n\n(To open one of these pages for the user, call open_website with its number or URL, "
              "e.g. open_website site=\"1\". Never click_on a search result: it isn't on the screen.)")


ORDINALS = {"pierwsz": 1, "first": 1, "drug": 2, "second": 2, "trzec": 3, "third": 3, "czwart": 4, "fourth": 4,
            "piąt": 5, "fifth": 5, "ostatni": -1, "last": -1}


def result_url(target: str) -> str | None:
    """'2', 'the first one', 'drugi', or (part of) a result's title -> that search result's URL."""
    if not LAST_RESULTS:
        return None
    t = target.strip().strip("'\".#[]").lower()
    n = None
    if t.isdigit():
        n = int(t)
    else:
        for stem, i in ORDINALS.items():
            if re.search(rf"\b{stem}\w*\b", t) and len(t.split()) <= 4:
                n = i
                break
    if n is not None:
        if n == -1:
            return LAST_RESULTS[-1]["url"]
        return LAST_RESULTS[n - 1]["url"] if 1 <= n <= len(LAST_RESULTS) else None
    words = [w for w in re.findall(r"\w+", t) if len(w) > 2]
    if len(words) >= 2:
        for r in LAST_RESULTS:
            title = r["title"].lower()
            if t in title or sum(w in title for w in words) >= max(2, int(len(words) * 0.7)):
                return r["url"]
    return None


@tool("Read the text of a web page (use after web_search to get details).", url="the page URL")
def read_webpage(url: str) -> str:
    import trafilatura

    r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=15)
    r.raise_for_status()
    text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
    if not text:
        return "Could not extract readable text from that page."
    return text[:4000] + ("\n…(truncated)" if len(text) > 4000 else "")


SITES = {
    "youtube": "https://www.youtube.com", "yt": "https://www.youtube.com", "netflix": "https://www.netflix.com",
    "twitch": "https://www.twitch.tv", "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "google maps": "https://maps.google.com", "maps": "https://maps.google.com", "reddit": "https://www.reddit.com",
    "twitter": "https://x.com", "x": "https://x.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "tiktok": "https://www.tiktok.com", "github": "https://github.com",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "wikipedia": "https://www.wikipedia.org",
    "amazon": "https://www.amazon.com", "allegro": "https://allegro.pl", "spotify web": "https://open.spotify.com",
    "discord web": "https://discord.com/app", "outlook": "https://outlook.live.com", "translate": "https://translate.google.com",
    "google translate": "https://translate.google.com", "kick": "https://kick.com", "disney plus": "https://www.disneyplus.com",
    "prime video": "https://www.primevideo.com", "hbo max": "https://www.max.com", "max": "https://www.max.com",
}
URL_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", re.I)


def site_url(target: str) -> str:
    """A URL for a site name ('netflix'), a domain ('bbc.co.uk'), a URL, or a search."""
    t = target.strip().strip("'\"")
    key = t.lower().removeprefix("the ").removesuffix(" website").removesuffix(" site").strip()
    if key in SITES:
        return SITES[key]
    if URL_RE.match(t):
        return t if t.lower().startswith(("http://", "https://")) else "https://" + t
    if len(t.split()) <= 4:  # a name: find its official site
        for q in (f"{t} official site", t, f"{t} website"):
            try:
                hits = [r["href"] for r in search(q, 3) if r.get("href", "").startswith("http")]
            except Exception:  # noqa: BLE001  (search engines are flaky; try the next phrasing)
                continue
            if hits:
                return hits[0]
        word = re.sub(r"[^a-z0-9-]", "", key)
        for guess in (f"https://www.{word}.com", f"https://{word}.com"):  # last resort: guess the domain
            try:
                if word and httpx.head(guess, headers={"User-Agent": UA}, follow_redirects=True, timeout=4).status_code < 400:
                    return guess
            except httpx.HTTPError:
                pass
    return f"https://www.google.com/search?q={quote_plus(t)}"


def open_site(target: str) -> str:
    url = result_url(target) or site_url(target)
    webbrowser.open(url)
    return url


@tool("Open a website in the user's browser: a URL, a site name (netflix, gmail, deadlock wiki), the number of "
      "one of your last web_search results (\"1\"), or a Google search if it's a question.",
      site="a URL, a website name, a search result number, or words to search for")
def open_website(site: str) -> str:
    return f"opened {open_site(site)}"


@tool("YouTube videos on a topic, from anyone. play=true starts the best matching video right away (\"puść/odpal "
      "film o X\"); play=random starts one of the top ones (\"jakiś film z minecrafta\"; you pick the topic yourself "
      "if they don't say); play=false just shows the search. A named YouTuber -> open_youtube_channel instead.",
      query="what to look for", play="true = the top video (default) | random = one of the top videos | false = just the search")
def open_youtube(query: str, play: str = "true") -> str:
    import random

    mode = str(play).strip().lower()
    if mode in ("random", "losowy", "jakiś", "any"):
        vids = top_videos(query, 10)
        if vids:
            title, url = random.choice(vids)
            webbrowser.open(url)
            return f"playing \"{title}\" on YouTube"
    if mode not in ("false", "0", "no", "nie"):
        hit = first_video(query)
        if hit:
            title, url = hit
            webbrowser.open(url)
            return f"playing \"{title}\" on YouTube"
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    return f"opened YouTube results for {query}"


# ── YouTube channels ─────────────────────────────────────────
YT_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
YT_COOKIES = {"SOCS": "CAI", "CONSENT": "YES+1"}  # skip the cookie-consent page


def _yt_data(query: str, sp: str = "") -> dict | None:
    import json

    params = {"search_query": query}
    if sp:
        params["sp"] = sp
    return _yt_page("https://www.youtube.com/results", params)


def _yt_page(url: str, params: dict | None = None) -> dict | None:
    """A YouTube page's data (ytInitialData); one retry, since YouTube sometimes answers with an empty page."""
    import json

    for attempt in range(2):
        try:
            r = httpx.get(url, params=params, headers=YT_HEADERS, cookies=YT_COOKIES, follow_redirects=True, timeout=10)
            m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text)
            if m:
                return json.loads(m.group(1))
        except (httpx.HTTPError, ValueError) as e:
            if attempt:
                raise
            log_web.debug("youtube page failed once: %s", e)
        time.sleep(0.5)
    return None


def _walk(o, key: str):
    if isinstance(o, dict):
        if key in o:
            yield o[key]
        for v in o.values():
            yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)


def _text(t: dict | None) -> str:
    t = t or {}
    return t.get("simpleText") or "".join(x.get("text", "") for x in t.get("runs", []))


def top_videos(query: str, n: int = 10) -> list[tuple[str, str]]:
    """The top real videos for a search (no Shorts, no live streams): [(title, watch URL)]."""
    try:
        data = _yt_data(query, "EgIQAQ==")  # type: video
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for v in _walk(data or {}, "videoRenderer"):
        if v.get("videoId") and v.get("lengthText"):
            out.append((_text(v.get("title")), f"https://www.youtube.com/watch?v={v['videoId']}"))
            if len(out) >= n:
                break
    return out


def first_video(query: str) -> tuple[str, str] | None:
    vids = top_videos(query, 1)
    return vids[0] if vids else None


def _norm_name(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.lower().replace("ł", "l"))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _name_score(asked: str, title: str, handle: str = "") -> float:
    """How well a channel's title/handle matches the name asked for (Polish forms too: 'Elevena', 'Blowka')."""
    import difflib

    a = _norm_name(asked)
    best = 0.0
    for t in (_norm_name(title), _norm_name(handle.lstrip("/@"))):
        if not t or not a:
            continue
        if t == a:
            return 1.0
        stem = re.sub(r"(owi|em|ie|a|u|y|i|ka|kiem)$", "", a)  # Polish endings: Elevena -> Eleven
        if t == stem or (len(stem) >= 4 and t.startswith(stem) and len(t) - len(stem) <= 2):
            best = max(best, 0.95)
        elif a in t or t in a:
            best = max(best, 0.6 + 0.3 * min(len(a), len(t)) / max(len(a), len(t)))
        else:
            best = max(best, difflib.SequenceMatcher(None, a, t).ratio() * 0.8)
    return best


def _subs(text: str) -> float:
    m = re.search(r"([\d.,]+)\s*([KMB])?", text or "", re.I)
    if not m:
        return 0.0
    n = float(m.group(1).replace(",", "")) if m.group(2) else float(re.sub(r"[.,]", "", m.group(1)) or 0)
    return n * {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)


def _owner(v: dict) -> tuple[str, str, str]:
    """A video's channel: (name, url path, channel id)."""
    o = ((v.get("ownerText") or v.get("longBylineText") or {}).get("runs") or [{}])[0]
    be = o.get("navigationEndpoint", {}).get("browseEndpoint", {})
    cid = be.get("browseId", "")
    return o.get("text", ""), be.get("canonicalBaseUrl") or (f"/channel/{cid}" if cid else ""), cid


def find_channel(name: str, about: str = "") -> tuple[str, str] | None:
    """YouTube's best channel match for a name: (title, url).
    Picks by how well the name matches, how big the channel is, and (with a topic) who actually makes videos
    about it: "Eleven" + "fnaf" is the Polish YouTuber, not "Eleven Sports"."""
    import math

    try:
        data = _yt_data(name, "EgIQAg==")  # type: channel
    except (httpx.HTTPError, ValueError):
        data = None
    cands: dict[str, dict] = {}
    for i, c in enumerate(list(_walk(data or {}, "channelRenderer"))[:6]):
        path = (c.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("canonicalBaseUrl")
                or f"/channel/{c.get('channelId')}")
        title = _text(c.get("title"))
        # (the subscriber count sometimes sits in videoCountText in the new layout)
        subs = max(_subs(_text(c.get("subscriberCountText"))) if "subscriber" in _text(c.get("subscriberCountText")) else 0,
                   _subs(_text(c.get("videoCountText"))) if "subscriber" in _text(c.get("videoCountText")) else 0)
        cands[c.get("channelId") or path] = {"title": title, "path": path, "name": _name_score(name, title, path),
                                             "subs": subs, "rank": i, "votes": 0}
    if about:  # who owns the videos for "<name> <topic>"?
        try:
            vdata = _yt_data(f"{name} {about}", "EgIQAQ==")
        except (httpx.HTTPError, ValueError):
            vdata = None
        for v in list(_walk(vdata or {}, "videoRenderer"))[:10]:
            owner, path, cid = _owner(v)
            if not path or _name_score(name, owner, path) < 0.6:
                continue
            c = cands.setdefault(cid or path, {"title": owner, "path": path, "name": _name_score(name, owner, path),
                                               "subs": 0, "rank": 6, "votes": 0})
            c["votes"] += 1
    if cands:
        def score(c):
            return (c["name"] * 3 + math.log10(c["subs"] + 10) * 0.25 + min(c["votes"], 5) * 0.6
                    - c["rank"] * 0.1)
        best = max(cands.values(), key=score)
        log_web.info("youtube channel for %r%s: %s (%s)", name, f" + {about!r}" if about else "", best["title"], best["path"])
        return best["title"], "https://www.youtube.com" + best["path"]
    if data:  # YouTube sometimes serves another layout: take the first channel handle on the page
        import json

        m = re.search(r'"canonicalBaseUrl":"(/@[^"]+)"', json.dumps(data))
        if m:
            return m.group(1)[2:], "https://www.youtube.com" + m.group(1)
    return None


def channel_search(channel_url: str, about: str) -> list[dict]:
    """A channel's own videos about something (its search tab): [{id, title, views}], best match first."""
    data = _yt_page(channel_url.rstrip("/") + "/search", {"query": about})
    out = []
    for v in _walk(data or {}, "videoRenderer"):
        if v.get("videoId") and v.get("lengthText"):  # (no Shorts / live streams)
            out.append({"id": v["videoId"], "title": _text(v.get("title")),
                        "views": _views(_text(v.get("viewCountText"))),
                        "age": _age(_text(v.get("publishedTimeText")))})
    return list({v["id"]: v for v in out}.values())


def _age(text: str) -> float:
    """'3 weeks ago' / '2 lata temu' -> days (for 'the newest'); unknown = very old."""
    m = re.search(r"(\d+)\s*(\w+)", text or "")
    if not m:
        return 1e9
    unit = m.group(2).lower()
    days = next((d for k, d in (("min", 0.001), ("godz", 0.04), ("hour", 0.04), ("sec", 0.0001), ("sek", 0.0001),
                                ("dni", 1), ("dzie", 1), ("day", 1), ("tyg", 7), ("week", 7), ("mies", 30),
                                ("month", 30), ("rok", 365), ("lat", 365), ("year", 365)) if unit.startswith(k)), 1e6)
    return int(m.group(1)) * days


TOPIC_FILLER = {"the", "a", "of", "and", "z", "ze", "o", "w", "na", "i", "od", "by", "gry", "gra", "game", "film",
                "filmy", "filmik", "video", "odcinek", "part", "gdzie", "jak", "ktory", "ktora", "ktore", "ktorym",
                "kiedy", "where", "how", "when", "which", "that", "ten", "ta", "to", "tym", "jego", "his", "her"}


# game nicknames -> the words in real titles ("fnaf 3" must not match a fan game "Five Nights at Frickbear's 3")
ALIASES = {"fnaf": "five nights at freddy", "fnf": "friday night funkin", "gta": "grand theft auto",
           "cs": "counter strike", "csgo": "counter strike", "cs2": "counter strike", "lol": "league of legends",
           "mc": "minecraft", "cod": "call of duty", "rdr": "red dead redemption", "tlou": "the last of us",
           "botw": "breath of the wild", "totk": "tears of the kingdom", "dbd": "dead by daylight",
           "r6": "rainbow six", "pubg": "battlegrounds", "wow": "world of warcraft", "ow": "overwatch",
           "re": "resident evil", "dmc": "devil may cry", "sh": "silent hill", "ac": "assassin"}


def _tokens(about: str) -> list[str]:
    return [w for w in (_norm_name(x) for x in re.findall(r"[\w']+", about)) if w and w not in TOPIC_FILLER]


def _title_score(title: str, about: str) -> float:
    """Share of the asked-for words a title really has: 'teorie fnaf 3' -> 'teoria', 'fnaf' and the game
    number 3 (not an episode '#3'). Polish forms count ('teorię' = 'teoria', 'fnafa' = 'fnaf')."""
    toks = _tokens(about)
    if not toks:
        return 1.0
    words = [_norm_name(x) for x in re.findall(r"[\w']+", title)]
    words = [w for w in words if w]
    plain = re.sub(r"#\s*\d+|\d+\s*/\s*\d+|\(\s*\d+[^)]*\)|\d+:\d+|odc\w*\.?\s*\d+|ep\w*\.?\s*\d+|part\s*\d+|"
                   r"cz(?:ęść|esc|\.)\s*\d+|noc\w*\s*\d+|night\s*\d+|sezon\s*\d+", " ", title.lower())
    numbers = set(re.findall(r"(?<![\d#])\d+(?!\d)", plain))  # game/part numbers, not episode numbers
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", title.lower().replace("'s", "")) if w)
    flat = _norm_name(title)
    hits = num_hits = 0
    for t in toks:
        base = t[:-1] if len(t) >= 5 and t not in ALIASES and t[:-1] in ALIASES else t  # "fnafa" -> "fnaf"
        if t.isdigit():
            num_hits += t in numbers
        elif base in ALIASES:
            hits += base in words or _norm_name(ALIASES[base]) in flat
        elif any(w == t or (len(t) >= 4 and w.startswith(t[:max(4, len(t) - 2)])) or (len(w) >= 4 and t.startswith(w))
                 for w in words):
            hits += 1
        elif len(t) >= 3 and t in initials:  # an unknown short name made of a title's initials
            hits += 1
    if hits == 0 and len(toks) > num_hits:  # "Jolly 3" for "fnaf 3": a number alone means nothing
        num_hits = 0
    return (hits + num_hits) / len(toks)


ORDINAL_WORDS = {
    "pierwsz": 1, "drug": 2, "trzec": 3, "czwart": 4, "piąt": 5, "piat": 5, "szóst": 6, "szost": 6, "siódm": 7,
    "siodm": 7, "ósm": 8, "osm": 8, "dziewiąt": 9, "dziewiat": 9, "dziesiąt": 10, "dziesiat": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10}
NEWEST_RE = re.compile(r"najnowsz\w*|\bostatni[mae]?\b|przedostatni\w*|newest|latest|most recent|"
                       r"\bnew\b|\bnowy\b|wrzucił\w*|uploaded", re.I)
POPULAR_RE = re.compile(r"najpopularniejsz\w*|najbardziej popularn\w*|najczęściej oglądan\w*|najwięcej wyświetl\w*|"
                        r"popular\w*|most viewed|most watched|\btop\b", re.I)


def video_rank(text: str) -> tuple[str, int] | None:
    """'drugi najnowszy film' -> ('latest', 2); 'trzeci najpopularniejszy' -> ('popular', 3);
    'przedostatni film' -> ('latest', 2); 'the 2nd newest' -> ('latest', 2). None if no newest/popular wording."""
    t = text.lower()
    mode = "popular" if POPULAR_RE.search(t) else "latest" if NEWEST_RE.search(t) else ""
    if not mode:
        return None
    nth = 1
    if re.search(r"przedostatni|second[- ]to[- ]last|one before last", t):
        nth = 2
    else:
        m = re.search(r"\b(\d{1,2})\s*(?:\.|-?(?:st|nd|rd|th|gi|ci|ty|my|wszy))?\s+(?:naj|most|new|last|popul|ostat)", t)
        if m:
            nth = int(m.group(1))
        else:
            for w in re.findall(r"[^\W\d_]+", t):
                hit = next((n for stem, n in ORDINAL_WORDS.items() if w.startswith(stem) and len(w) - len(stem) <= 4), None)
                if hit:
                    nth = hit
                    break
    return mode, max(1, nth)


def _ordinal(n: int) -> str:
    return "" if n <= 1 else {2: "2nd ", 3: "3rd "}.get(n, f"{n}th ")


def _pick(vids: list[dict], about: str, mode: str, nth: int = 1) -> tuple[dict, float]:
    """The video to play: for a specific request ('teorie fnaf 3') the best match (several equally good:
    random only if they said 'jakiś'); for a plain topic ('fnaf') random / newest / most viewed."""
    import random

    scored = [(v, _title_score(v["title"], about)) for v in vids]
    top = max(s for _, s in scored)
    pool = [v for v, s in scored if s == top] if top > 0 else vids
    if mode == "latest":
        pool = sorted(pool, key=lambda x: x.get("age", 1e9))
        return pool[min(nth, len(pool)) - 1], top
    if mode == "popular":
        pool = sorted(pool, key=lambda x: -x["views"])
        return pool[min(nth, len(pool)) - 1], top
    if mode == "best" or (len(_tokens(about)) >= 2 and mode != "random"):
        return pool[0], top  # YouTube's own relevance order among the best matches
    return random.choice(pool[:20]), top


def _about_match(title: str, about: str) -> bool:
    """Is a video title about the topic? (fnaf = Five Nights at Freddy's too)"""
    t, a = _norm_name(title), _norm_name(about)
    if not a or a in t:
        return True
    words = [w for w in re.findall(r"\w{3,}", about.lower())]
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9']+", title.lower().replace("'s", "")))
    return a in initials or (bool(words) and all(_norm_name(w) in t for w in words))


def _views(text: str) -> float:
    """'1.2M views' / '35K views' / '1,234 views' -> a number (for 'the most popular')."""
    m = re.search(r"([\d.,]+)\s*(K|M|B|thousand|million|billion)?\s*(?:views|wyświetle)", text or "", re.I)
    if not m:
        return 0.0
    unit = (m.group(2) or "").lower()
    n = float(m.group(1).replace(",", "")) if unit else float(re.sub(r"[.,]", "", m.group(1)))
    return n * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}.get(unit, 1)


def channel_videos(channel_url: str) -> list[dict]:
    """The videos on a channel's Videos tab, newest first: [{id, title, views}]. (No Shorts, no streams.)"""
    data = _yt_page(channel_url.rstrip("/") + "/videos")
    if not data:
        return []
    out = []
    for lv in _walk(data, "lockupViewModel"):  # the current page layout
        vid = lv.get("contentId")
        meta = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = (meta.get("title") or {}).get("content", "")
        texts = [p.get("accessibilityLabel") or p.get("text", {}).get("content", "") for row in
                 meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                 for p in row.get("metadataParts", [])]
        if vid and title and lv.get("contentType", "LOCKUP_CONTENT_TYPE_VIDEO") == "LOCKUP_CONTENT_TYPE_VIDEO":
            out.append({"id": vid, "title": title, "views": max((_views(t) for t in texts), default=0.0)})
    if not out:  # the older layout
        for v in _walk(data, "videoRenderer"):
            if v.get("videoId"):
                out.append({"id": v["videoId"], "title": _text(v.get("title")),
                            "views": _views(_text(v.get("viewCountText")))})
    return list({v["id"]: v for v in out}.values())


@tool("A YouTuber's CHANNEL or THEIR videos: \"włącz kanał X\" (the page), \"włącz kanał X i odpal jakiś film\" "
      "(play=random), \"włącz film z gry Y od X\" / \"puść odcinek X-a o Y\" (about=Y, play=random): one of X's "
      "own videos about Y. Use this whenever a specific YouTuber is named (\"od X\", \"X-a\", \"by X\").",
      name="the channel / YouTuber name in its basic form (\"od Elevena\" -> Eleven, \"Blowka\" -> Blowek)",
      play="optional: random (any of its videos) | latest (newest) | popular (most viewed) | best (the one that "
           "matches 'about' best: a specific video); empty = just the channel page",
      about="optional: the game/topic, or the specific video in the user's words (e.g. fnaf, \"teoria fnaf 3\", "
            "\"minecraft hardcore 100 dni\"); keep every word they said about it",
      nth="optional, with latest/popular: which one (2 = the second newest / second most viewed; default 1)",
      tab="optional when not playing: videos | shorts | streams")
def open_youtube_channel(name: str, play: str = "", about: str = "", tab: str = "", nth: int = 1) -> str:
    import random

    try:
        nth = max(1, int(nth or 1))
    except (TypeError, ValueError):
        nth = 1

    name = re.sub(r"(?i)\b(kanał\w*|channel|na (?:yt|youtube)|on youtube|youtube|yt|od|by)\b", "", name).strip(" ,.") or name
    about = re.sub(r"(?i)^\s*(?:z gry|z|o|about|gra|gry|game|filmy?|videos?)\s+", "", str(about or "")).strip(" ,.\"'")
    hit = find_channel(name, about)
    if not hit:
        webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus((name + ' ' + about).strip())}")
        return f"couldn't find the channel, opened YouTube results for {name} {about}".strip()
    title, url = hit
    mode = str(play).strip().lower()
    ranked = video_rank(mode) if mode not in ("latest", "popular") else None
    if ranked:  # play="drugi najnowszy" / "second newest"
        mode, nth = ranked[0], max(nth, ranked[1])
    mode = {"true": "random", "yes": "random", "1": "random", "losowy": "random", "jakiś": "random",
            "najnowszy": "latest", "newest": "latest", "ostatni": "latest", "new": "latest",
            "najpopularniejszy": "popular", "most viewed": "popular", "top": "popular"}.get(mode, mode)
    if about and not mode:
        mode = "best" if len(_tokens(about)) >= 2 else "random"  # a topic means they want a video
    if about:
        try:
            vids = channel_search(url, about)
        except (httpx.HTTPError, ValueError):
            vids = []
        try:  # YouTube's general search finds some of the channel's videos its own search misses
            path = url.split("youtube.com", 1)[1]
            seen = {v["id"] for v in vids}
            for v in _walk(_yt_data(f"{name} {about}", "EgIQAQ==") or {}, "videoRenderer"):
                o_name, o_path, o_id = _owner(v)
                if v.get("videoId") not in seen and v.get("lengthText") and (o_path == path or o_id in path):
                    vids.append({"id": v["videoId"], "title": _text(v.get("title")),
                                 "views": _views(_text(v.get("viewCountText"))),
                                 "age": _age(_text(v.get("publishedTimeText")))})
        except (httpx.HTTPError, ValueError):
            pass
        # nothing has every word yet: search again with the game's full name ("fnaf 3" -> "five nights at freddy 3")
        full = " ".join(ALIASES.get(w, w) for w in re.findall(r"[\w']+", about.lower()))
        if full != about.lower() and vids and max(_title_score(v["title"], about) for v in vids) < 1:
            try:
                seen = {v["id"] for v in vids}
                vids += [v for v in channel_search(url, full) if v["id"] not in seen]
            except (httpx.HTTPError, ValueError):
                pass
        on_topic =[v for v in vids if _about_match(v["title"], about) or _title_score(v["title"], about) >= 0.5]
        vids = on_topic or vids
        if vids and mode == "latest":
            # search results only say "2 years ago": for "the (2nd) newest about X" use the Videos tab's exact order
            try:
                recent = [r for r in channel_videos(url) if _title_score(r["title"], about) >= 1]
            except (httpx.HTTPError, ValueError):
                recent = []
            if len(recent) >= nth:
                v = recent[nth - 1]
                webbrowser.open(f"https://www.youtube.com/watch?v={v['id']}")
                return f"playing \"{v['title']}\" from the channel {title} (its {_ordinal(nth)}newest video about {about})"
        if vids:
            v, score = _pick(vids, about, mode, nth)
            webbrowser.open(f"https://www.youtube.com/watch?v={v['id']}")
            note = "" if score >= 1 else (f" (the closest match: {title} has no video with everything in "
                                          f"'{about}'; tell the user it's the closest one)")
            return f"playing \"{v['title']}\" from the channel {title}{note}"
        webbrowser.open(f"{url}/search?query={quote_plus(about)}")
        return f"the channel {title} has no videos about {about}; opened its search for it"
    if mode in ("random", "latest", "popular"):
        try:
            vids = channel_videos(url)
        except (httpx.HTTPError, ValueError):
            vids = []
        if vids:
            if mode == "latest":  # (the Videos tab is newest first)
                v = vids[min(nth, len(vids)) - 1]
            elif mode == "popular":
                v = sorted(vids, key=lambda x: -x["views"])[min(nth, len(vids)) - 1]
            else:
                v = random.choice(vids[:30])
            webbrowser.open(f"https://www.youtube.com/watch?v={v['id']}")
            which = {"latest": "newest", "popular": "most viewed"}.get(mode)
            rank = f" (its {_ordinal(nth)}{which} video)" if which else ""
            return f"playing \"{v['title']}\" from the channel {title}{rank}"
    tab = tab.strip().lower()
    if tab in ("videos", "shorts", "streams"):
        url += "/" + tab
    webbrowser.open(url)
    return f"opened the channel {title} ({url})" + (" (couldn't pick a video from it)" if mode else "")


# ── streaming channels elsewhere ─────────────────────────────
@tool("Open a streamer's channel on Twitch or Kick (\"włącz streama X na twitchu\"). A live stream starts "
      "playing by itself. For YouTube channels use open_youtube_channel.",
      platform="twitch | kick", name="the streamer's name / login")
def open_stream_channel(platform: str, name: str) -> str:
    login = re.sub(r"(?i)\b(stream\w*|kanał\w*|channel|na|on|twitch\w*|kick\w*)\b", " ", name)
    login = re.sub(r"[^\w]", "", login.strip()).lower() or re.sub(r"[^\w]", "", name).lower()
    p = platform.lower()
    url = f"https://kick.com/{login}" if "kick" in p else f"https://www.twitch.tv/{login}"
    webbrowser.open(url)
    return f"opened {url}"


# ── shops / classifieds ──────────────────────────────────────
def _slug(q: str) -> str:
    return re.sub(r"[^\w]+", "-", q.strip().lower()).strip("-")


SHOPS = {  # name -> (search url, url for cheapest first)
    "olx": ("https://www.olx.pl/oferty/q-{slug}/", "https://www.olx.pl/oferty/q-{slug}/?search%5Border%5D=filter_float_price%3Aasc"),
    "allegro": ("https://allegro.pl/listing?string={q}", "https://allegro.pl/listing?string={q}&order=p"),
    "allegro lokalnie": ("https://allegrolokalnie.pl/oferty/q/{slug}", "https://allegrolokalnie.pl/oferty/q/{slug}?sort=price"),
    "vinted": ("https://www.vinted.pl/catalog?search_text={q}", "https://www.vinted.pl/catalog?search_text={q}&order=price_low_to_high"),
    "amazon": ("https://www.amazon.pl/s?k={q}", "https://www.amazon.pl/s?k={q}&s=price-asc-rank"),
    "ebay": ("https://www.ebay.com/sch/i.html?_nkw={q}", "https://www.ebay.com/sch/i.html?_nkw={q}&_sop=15"),
    "ceneo": ("https://www.ceneo.pl/;szukaj-{q}", "https://www.ceneo.pl/;szukaj-{q};0112-0.htm"),
    "otomoto": ("https://www.otomoto.pl/osobowe/q-{slug}", "https://www.otomoto.pl/osobowe/q-{slug}?search%5Border%5D=filter_float_price%3Aasc"),
    "steam": ("https://store.steampowered.com/search/?term={q}", "https://store.steampowered.com/search/?term={q}&sort_by=Price_ASC"),
}


@tool("Search a shop or classifieds site and show the offers in the browser, optionally cheapest first "
      "(\"znajdź najtańsze ogłoszenie iPhone na OLX\"). Shops: olx, allegro, allegro lokalnie, vinted, amazon, "
      "ebay, ceneo, otomoto, steam. For other shops use open_website.",
      shop="olx / allegro / vinted / ...", query="what to look for", cheapest="true = sort by lowest price")
def open_shop_search(shop: str, query: str, cheapest: bool = False) -> str:
    key = shop.lower().replace(".pl", "").replace(".com", "").strip()
    if key not in SHOPS:
        key = next((k for k in SHOPS if k in key or key in k), "")
    if not key:
        return open_website(f"{shop} {query}")
    cheap = cheapest if isinstance(cheapest, bool) else str(cheapest).lower() in ("true", "1", "yes", "tak")
    url = SHOPS[key][1 if cheap else 0].format(q=quote_plus(query), slug=_slug(query))
    webbrowser.open(url)
    return f"opened {key} offers for {query}" + (" (cheapest first)" if cheap else "")
