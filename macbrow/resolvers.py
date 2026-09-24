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


def _youtube_video_ids(query: str) -> list[str]:
    """Video ids of a YouTube search, in the order the results page lists them."""
    html = _fetch("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    ids = re.findall(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"', html) or re.findall(
        r'"videoId":"([A-Za-z0-9_-]{11})"', html
    )
    return list(dict.fromkeys(ids))


def youtube_first_result(query: str) -> str:
    """Watch URL of the first video for a YouTube search."""
    q = query.strip()
    if not q:
        raise ResolveError("I didn't catch what to play.")
    ids = _youtube_video_ids(q)
    if not ids:
        raise ResolveError(f"I couldn't find a YouTube video for {q}.")
    return f"https://www.youtube.com/watch?v={ids[0]}"


def page_result_url(position: str, page_url: str = "") -> str:
    """URL of result number ``position`` on the results page ``page_url``, for pages that can be read
    without the browser (YouTube search results). "" otherwise: the tool then reads the page itself."""
    n = int(position) if position.isdigit() else 1
    parts = urllib.parse.urlsplit(page_url)
    host = (parts.hostname or "").lower()
    if (host == "youtube.com" or host.endswith(".youtube.com")) and parts.path == "/results":
        query = urllib.parse.parse_qs(parts.query).get("search_query", [""])[0]
        if query:
            ids = _youtube_video_ids(query)
            if len(ids) < n:
                raise ResolveError(f"There are only {len(ids)} videos on this page.")
            return f"https://www.youtube.com/watch?v={ids[n - 1]}"
    return ""


# Finds the n-th result link on the page in front (run by the browser through AppleScript).
# Site-specific selectors first, then any sizeable visible link; duplicates removed.
_RESULT_JS = """(() => {
  const n = %d, host = location.hostname;
  const sites = [
    [/youtube\\.com$/, 'ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title-link, a#video-title'],
    [/(^|\\.)google\\./, '#search a:has(h3)'],
    [/amazon\\./, 'div[data-component-type="s-search-result"] h2 a, div[data-component-type="s-search-result"] a.s-no-outline'],
    [/flipkart\\.com$/, 'a[href*="/p/"]'],
    [/github\\.com$/, '[data-testid="results-list"] a, .search-title a'],
    [/reddit\\.com$/, 'a[slot="full-post-link"], a[data-testid="post-title"]'],
    [/bing\\.com$/, '#b_results h2 a'],
    [/duckduckgo\\.com$/, 'a[data-testid="result-title-a"]'],
    [/wikipedia\\.org$/, '.mw-search-result-heading a'],
  ];
  let sel = null;
  for (const [re, s] of sites) if (re.test(host)) { sel = s; break; }
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let links = sel ? [...document.querySelectorAll(sel)]
    : [...document.querySelectorAll('main a[href], article a[href], a[href]')].filter(a => a.textContent.trim().length > 15);
  links = links.filter(a => a.href && a.href.startsWith('http') && visible(a));
  const seen = new Set();
  links = links.filter(a => !seen.has(a.href) && seen.add(a.href));
  return links.length >= n ? links[n - 1].href : '';
})()"""


def results_page(_position: str, page_url: str = "") -> str:
    """The results page a numbered pick refers to (the agent resolves it; see DynamicMacAgent._listing_for)."""
    return page_url


def result_link_js(position: str) -> str:
    return _RESULT_JS % (int(position) if position.isdigit() else 1)


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


RESOLVERS = {
    "youtube_first_result": youtube_first_result,
    "site_search_url": site_search_url,
    "page_result_url": page_result_url,
    "result_link_js": result_link_js,
    "results_page": results_page,
}


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
