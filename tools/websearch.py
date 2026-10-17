"""
Web intelligence tools: search (Tavily → SearXNG → DuckDuckGo HTML fallback),
page scraping (httpx + BeautifulSoup4), OpenWeatherMap forecast, RSS digests.
These degrade gracefully offline (cached/simulated snippets) so the agent
never dead-ends during routines.
"""
from __future__ import annotations

import math
import random
import re
import time
import xml.etree.ElementTree as ET

import httpx

from config import settings
from eventbus import bus
from .registry import register

_UA = {"User-Agent": "Mozilla/5.0 (SmartHomeAgent/1.0; RaspberryPi5)"}
_weather_cache: dict = {"data": None, "ts": 0}


@register(
    name="web_search",
    domain="web",
    description="Search the web for current information (news, facts, prices, how-tos). "
                "Returns ranked titles, URLs and snippets.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
    examples=["search for latest iPhone news"],
)
async def web_search(query: str, max_results: int = 5) -> dict:
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
        # 1) Tavily (best quality, needs key)
        if settings.tavily_api_key:
            try:
                r = await c.post("https://api.tavily.com/search",
                                 json={"api_key": settings.tavily_api_key, "query": query,
                                       "max_results": max_results})
                if r.status_code == 200:
                    results = [{"title": x["title"], "url": x["url"],
                                "snippet": x["content"][:220]}
                               for x in r.json().get("results", [])][:max_results]
            except Exception:
                pass
        # 2) Self-hosted SearXNG
        if not results and settings.searxng_url:
            try:
                r = await c.get(f"{settings.searxng_url}/search",
                                params={"q": query, "format": "json"})
                if r.status_code == 200:
                    results = [{"title": x.get("title"), "url": x.get("url"),
                                "snippet": x.get("content", "")[:220]}
                               for x in r.json().get("results", [])][:max_results]
            except Exception:
                pass
        # 3) DuckDuckGo html endpoint (no key required)
        if not results:
            try:
                r = await c.get("https://html.duckduckgo.com/html/", params={"q": query})
                if r.status_code == 200:
                    for m in re.finditer(
                            r'<a rel="nofollow" class="result__a" href="([^"]+)">([^<]+)</a>'
                            r'.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
                        url, title, snip = m.groups()
                        results.append({"title": re.sub("<.*?>", "", title).strip(),
                                        "url": url,
                                        "snippet": re.sub("<.*?>", "", snip).strip()[:220]})
                        if len(results) >= max_results:
                            break
            except Exception:
                pass
    status = "live" if results else "offline-fallback"
    if not results:
        results = [{"title": f"(cached) Result for '{query}'", "url": "n/a",
                    "snippet": "Network unavailable — answer from general knowledge instead."}]
    await bus.emit("web", "search", {"query": query, "hits": len(results)},
                   log_line=f"[WEB] search '{query}' → {len(results)} results ({status})")
    return {"ok": True, "query": query, "results": results, "source": status}


@register(
    name="fetch_page",
    domain="web",
    description="Fetch a web page by URL and extract readable text content "
                "(headlines, article body) for summarisation.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string"},
                       "max_chars": {"type": "integer", "default": 3000}},
        "required": ["url"],
    },
)
async def fetch_page(url: str, max_chars: int = 3000) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        return {"ok": False, "error": f"fetch failed: {e}"}
    title = url
    try:
        from bs4 import BeautifulSoup  # optional dependency
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:max_chars]
    except ImportError:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))[:max_chars]
    await bus.emit("web", "fetch", {"url": url, "chars": len(text)},
                   log_line=f"[WEB] fetched {url} ({len(text)} chars)")
    return {"ok": True, "url": url, "title": title, "text": text}


@register(
    name="weather_forecast",
    domain="web",
    description="Get current conditions and optionally the forecast from OpenWeatherMap "
                "for the configured home location (or given lat/lon).",
    parameters={
        "type": "object",
        "properties": {
            "lat": {"type": "number"}, "lon": {"type": "number"},
            "forecast": {"type": "boolean", "default": False},
        },
    },
    examples=["what's the weather today", "will it rain tomorrow"],
)
async def weather_forecast(lat: float | None = None, lon: float | None = None,
                           forecast: bool = False) -> dict:
    lat = lat or settings.weather_lat
    lon = lon or settings.weather_lon
    api = "forecast" if forecast else "weather"
    path = f"https://api.openweathermap.org/data/2.5/{api}"
    if settings.openweathermap_key:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(path, params={"lat": lat, "lon": lon,
                                              "units": settings.weather_units,
                                              "appid": settings.openweathermap_key})
                if r.status_code == 200:
                    data = r.json()
                    _weather_cache.update(data=data, ts=time.time())
                    cur = data.get("current") or data
                    main = cur.get("main", {})
                    wx = cur.get("weather", [{}])[0].get("description", "clear")
                    out = {"ok": True, "temp_c": main.get("temp"),
                           "feels_like": main.get("feels_like"),
                           "humidity": main.get("humidity"), "condition": wx,
                           "wind_ms": cur.get("wind", {}).get("speed"),
                           "raw": data if forecast else None, "source": "openweathermap"}
                    await bus.emit("weather", "update", out,
                                   log_line=f"[WEATHER] {wx}, {out['temp_c']}°C "
                                            f"(humidity {out['humidity']}%)")
                    return out
        except Exception:
            pass
    # ---- graceful simulated fallback (10-min cache) ----
    if _weather_cache["data"] and time.time() - _weather_cache["ts"] < 600:
        return {"ok": True, **_weather_cache["data"], "source": "cache"}
    hour = time.localtime().tm_hour
    temp = round(9 + 9 * math.sin(max(0, hour - 5) / 14 * math.pi) + random.uniform(0, 2.5), 1)
    conds = ["clear sky", "few clouds", "scattered clouds", "light rain", "partly cloudy"]
    cond = conds[int(hour / 5) % len(conds)]
    sim = {"ok": True, "temp_c": temp, "feels_like": round(temp - 1.2, 1),
           "humidity": 40 + int(temp) % 30, "condition": cond,
           "wind_ms": round(random.uniform(1, 6), 1),
           "location": {"lat": lat, "lon": lon}, "source": "simulated"}
    _weather_cache.update(data=sim, ts=time.time())
    await bus.emit("weather", "update", sim,
                   log_line=f"[WEATHER] {cond}, {temp}°C (simulated)")
    return sim


@register(
    name="rss_digest",
    domain="web",
    description="Read configured RSS news feeds and return top headlines for a daily digest.",
    parameters={
        "type": "object",
        "properties": {"limit_per_feed": {"type": "integer", "default": 4}},
    },
    examples=["give me the news digest", "read the rss feeds"],
)
async def rss_digest(limit_per_feed: int = 4) -> dict:
    feeds: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
        for url in settings.rss_feeds:
            items: list[dict] = []
            try:
                r = await c.get(url)
                root = ET.fromstring(r.content)
                for it in root.iter():
                    if it.tag.endswith("item") or it.tag.endswith("entry"):
                        title = (it.findtext("title") or "").strip()
                        link = (it.findtext("link") or "").strip()
                        if title:
                            items.append({"title": title[:160], "link": link})
                        if len(items) >= limit_per_feed:
                            break
            except Exception as e:
                items = [{"title": f"(feed unreachable: {type(e).__name__})", "link": url}]
            feeds[url] = items or [{"title": "(no items)", "link": url}]
    total = sum(len(v) for v in feeds.values())
    await bus.emit("web", "rss", {"feeds": len(feeds), "headlines": total},
                   log_line=f"[NEWS] RSS digest: {total} headlines from {len(feeds)} feeds")
    return {"ok": True, "feeds": feeds}
