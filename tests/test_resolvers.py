import asyncio

import pytest

from macbrow import resolvers


def test_unknown_resolver_is_a_spoken_error():
    with pytest.raises(resolvers.ResolveError):
        asyncio.run(resolvers.run("nope", "x"))


def test_youtube_parses_first_video_id(monkeypatch):
    html = '... "videoRenderer":{"videoId":"abcdefghijk","thumb":1} ... "videoId":"zzzzzzzzzzz"'
    monkeypatch.setattr(resolvers, "_fetch", lambda url, timeout=8.0: html)
    assert resolvers.youtube_first_result("anything") == "https://www.youtube.com/watch?v=abcdefghijk"


def test_youtube_empty_query_and_no_results(monkeypatch):
    with pytest.raises(resolvers.ResolveError):
        resolvers.youtube_first_result("   ")
    monkeypatch.setattr(resolvers, "_fetch", lambda url, timeout=8.0: "<html>nothing</html>")
    with pytest.raises(resolvers.ResolveError):
        resolvers.youtube_first_result("obscure")


def test_site_search_url_stays_on_the_open_site():
    from macbrow.resolvers import site_search_url

    assert site_search_url("espresso song", "https://www.youtube.com/watch?v=x") == (
        "https://www.youtube.com/results?search_query=espresso+song"
    )
    assert site_search_url("earbuds", "https://www.amazon.in/dp/B0") == "https://www.amazon.in/s?k=earbuds"
    assert site_search_url("x", "https://example.org/a") == "https://www.google.com/search?q=site%3Aexample.org+x"
    assert site_search_url("x", "chrome://newtab/") == "https://www.google.com/search?q=x"
