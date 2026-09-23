"""Computed arguments: Python-side lookups a tool can request before its script runs.

A tool declares  "computed": {"video_url": {"fn": "youtube_first_result", "from": "query"}}
and the agent fills {{video_url}} by calling the named function with the value of the
`query` argument. Keeps AppleScript free of JavaScript, curl, and API keys.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
import urllib.request

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class ResolveError(RuntimeError):
    """Spoken-friendly message in str(e)."""


def _fetch(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "en-US,en;q=0.9",
            # Skip the EU consent interstitial that would otherwise replace the results page.
            "Cookie": "SOCS=CAI; CONSENT=YES+cb",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def youtube_first_result(query: str) -> str:
    """Watch URL of the first video for a YouTube search."""
    q = query.strip()
    if not q:
        raise ResolveError("I didn't catch what to play.")
    html = _fetch("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q))
    m = re.search(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"', html) or re.search(
        r'"videoId":"([A-Za-z0-9_-]{11})"', html
    )
    if not m:
        raise ResolveError(f"I couldn't find a YouTube video for {q}.")
    return f"https://www.youtube.com/watch?v={m.group(1)}"


# Search results URL per site, so "search for X" on the page that is open runs that site's own
# search in the same tab. {q} is the URL-encoded query; {host} the page's host.
_SITE_SEARCH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|\.)music\.youtube\.com$"), "https://music.youtube.com/search?q={q}"),
    (re.compile(r"(^|\.)youtube\.com$"), "https://www.youtube.com/results?search_query={q}"),
    (re.compile(r"(^|\.)google\.[a-z.]+$"), "https://{host}/search?q={q}"),
    (re.compile(r"(^|\.)amazon\.[a-z.]+$"), "https://{host}/s?k={q}"),
    (re.compile(r"(^|\.)flipkart\.com$"), "https://www.flipkart.com/search?q={q}"),
    (re.compile(r"(^|\.)github\.com$"), "https://github.com/search?q={q}"),
    (re.compile(r"(^|\.)wikipedia\.org$"), "https://{host}/w/index.php?search={q}"),
    (re.compile(r"(^|\.)reddit\.com$"), "https://www.reddit.com/search/?q={q}"),
    (re.compile(r"(^|\.)(x|twitter)\.com$"), "https://x.com/search?q={q}"),
    (re.compile(r"(^|\.)linkedin\.com$"), "https://www.linkedin.com/search/results/all/?keywords={q}"),
    (re.compile(r"(^|\.)stackoverflow\.com$"), "https://stackoverflow.com/search?q={q}"),
    (re.compile(r"(^|\.)duckduckgo\.com$"), "https://duckduckgo.com/?q={q}"),
    (re.compile(r"(^|\.)bing\.com$"), "https://www.bing.com/search?q={q}"),
    (re.compile(r"(^|\.)open\.spotify\.com$"), "https://open.spotify.com/search/{q}"),
    (re.compile(r"(^|\.)netflix\.com$"), "https://www.netflix.com/search?q={q}"),
]


def site_search_url(query: str, page_url: str = "") -> str:
    """Search URL for ``query`` on the site ``page_url`` belongs to (a web search when there is none)."""
    q = query.strip()
    if not q:
        raise ResolveError("I didn't catch what to search for.")
    host = (urllib.parse.urlsplit(page_url).hostname or "").lower() if page_url.startswith("http") else ""
    enc = urllib.parse.quote_plus(q)
    if not host:  # new-tab page, about:blank... (non-browser apps don't use the URL at all)
        return "https://www.google.com/search?q=" + enc
    for rx, template in _SITE_SEARCH:
        if rx.search(host):
            return template.format(q=enc, host=host)
    # Any other site: a web search limited to it, in the same tab.
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(f"site:{host} {q}")


RESOLVERS = {"youtube_first_result": youtube_first_result, "site_search_url": site_search_url}


async def run(fn: str, value: str, focus_id: str | None = None) -> str:
    """``focus_id`` (the open page's URL, note id...) is passed on to resolvers that declare
    ``"with_focus": true`` in the tool's computed spec."""
    f = RESOLVERS.get(fn)
    if f is None:
        raise ResolveError(f"unknown resolver {fn}")
    call_args = (value,) if focus_id is None else (value, focus_id)
    try:
        return await asyncio.wait_for(asyncio.to_thread(f, *call_args), 12)
    except ResolveError:
        raise
    except TimeoutError as e:
        raise ResolveError("The lookup took too long.") from e
    except Exception as e:  # network errors etc.
        raise ResolveError("The lookup failed.") from e
