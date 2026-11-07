from __future__ import annotations

import re
import webbrowser
from urllib.parse import quote_plus

import httpx

from . import tool

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def search(query: str, max_results: int = 5, region: str = "wt-wt") -> list[dict]:
    from ddgs import DDGS

    return list(DDGS().text(query, region=region, max_results=max_results) or [])


@tool("Search the internet. Use it for news, facts you are unsure about, weather, prices, etc.", query="search query")
def web_search(query: str) -> str:
    results = search(query, 5)
    if not results:
        return "No results."
    return "\n\n".join(f"[{i+1}] {r.get('title')}\n{r.get('href')}\n{r.get('body')}" for i, r in enumerate(results))


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
    url = site_url(target)
    webbrowser.open(url)
    return url


@tool("Open a website in the user's browser (by URL or name, e.g. netflix, gmail, deadlock wiki), "
      "or a Google search if it's a question.", site="a URL, a website name, or words to search for")
def open_website(site: str) -> str:
    return f"opened {open_site(site)}"


@tool("Open a YouTube search or video in the browser.", query="what to look for")
def open_youtube(query: str) -> str:
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    return f"opened YouTube results for {query}"
