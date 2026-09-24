from macbrow.agent import DynamicMacAgent, _has_place, _on_web_page
from macbrow.applescript import MacContext
from macbrow.focus import Focus
from macbrow.registry import Tool, ToolRegistry

YT = Focus("Google Chrome", "tab", "YouTube", "https://www.youtube.com/")
AMAZON = Focus("Google Chrome", "tab", "Amazon", "https://www.amazon.in/")
NOTE = Focus("Notes", "note", "Shopping", "x-coredata://n1")


def _ctx(front: str, focus: Focus | None) -> MacContext:
    return MacContext(front, ["Finder", "Google Chrome", "Notes"], [], focus=focus)


def test_moves_detected_from_learned_scripts():
    spotify = 'tell application "Spotify" to activate\ntell application "System Events" to keystroke "l"'
    assert Tool.from_dict({"name": "a", "description": "d", "script": spotify}, "learned").moves
    assert not Tool.from_dict(
        {"name": "a", "description": "d", "script": 'tell application "Spotify" to playpause'}, "learned"
    ).moves


def test_leaves():
    a, reg = DynamicMacAgent, ToolRegistry()
    on_yt, on_amazon = _ctx("Google Chrome", YT), _ctx("Google Chrome", AMAZON)
    assert a._leaves(reg.get("chrome_open"), {"site": "github"}, on_yt)
    assert not a._leaves(reg.get("search_here"), {"query": "x"}, on_yt)
    assert not a._leaves(reg.get("set_volume"), {"level": "mute"}, on_yt)  # system tools never move you
    # youtube_play plays in the YouTube tab that is open, but opens one from anywhere else
    assert not a._leaves(reg.get("youtube_play"), {"query": "x"}, on_yt)
    assert a._leaves(reg.get("youtube_play"), {"query": "x"}, on_amazon)
    # opening the app that is already in front goes nowhere
    assert not a._leaves(reg.get("open_app"), {"app": "Google Chrome"}, on_yt)
    assert a._leaves(reg.get("open_app"), {"app": "Notes"}, on_yt)
    # web tasks stay when they run in the current tab
    wt = reg.get("web_task")
    assert a._leaves(wt, {"site": "amazon"}, on_yt)
    assert not a._leaves(wt, {"site": "current_tab"}, on_yt)
    assert not a._leaves(wt, None, on_yt)  # candidate check: it can run on the open page
    assert a._leaves(wt, None, _ctx("Notes", NOTE))


def test_places():
    assert _on_web_page(_ctx("Google Chrome", YT))
    assert not _on_web_page(_ctx("Notes", NOTE))
    assert not _on_web_page(_ctx("Google Chrome", Focus("Google Chrome", "tab", "New Tab", "chrome://newtab/")))
    assert _has_place(_ctx("Notes", NOTE))
    assert not _has_place(_ctx("Finder", None))  # the bare desktop is nothing to stay in


def test_tab_actions_move_only_for_tab_changes():
    a, reg = DynamicMacAgent, ToolRegistry()
    on_yt, act = _ctx("Google Chrome", YT), reg.get("app_action")
    for action in ("new_tab", "next_tab", "previous_tab", "new_item"):
        assert a._leaves(act, {"action": action}, on_yt), action
    for action in ("back", "scroll_down", "play_pause", "reload", "close_tab"):
        assert not a._leaves(act, {"action": action}, on_yt), action
    assert not a._leaves(act, None, on_yt)  # as a candidate it can still stay (other actions)


def test_play_pause_knows_it_is_on_youtube():
    act = ToolRegistry().get("app_action")
    on_yt = act.render({"action": "play_pause"}, YT)
    assert '("YouTubehttps://www.youtube.com/" contains "youtube")' in on_yt
    elsewhere = act.render({"action": "play_pause"}, NOTE)
    assert '("Shoppingx-coredata://n1" contains "youtube")' in elsewhere


def test_background_app_tools_act_elsewhere():
    reg, on_yt = ToolRegistry(), _ctx("Google Chrome", YT)
    assert DynamicMacAgent._elsewhere(reg.get("spotify_play_pause"), on_yt)  # Spotify is not in front
    assert DynamicMacAgent._elsewhere(reg.get("notes_write"), on_yt)
    assert not DynamicMacAgent._elsewhere(reg.get("chrome_reload"), on_yt)  # Chrome is in front
    assert not DynamicMacAgent._elsewhere(reg.get("app_action"), on_yt)  # works on whatever is in front
    assert not DynamicMacAgent._elsewhere(reg.get("set_volume"), on_yt)  # system-wide


def test_numbered_picks_use_the_last_results_page():
    from macbrow.agent import _is_listing

    assert _is_listing("https://www.youtube.com/results?search_query=espresso")
    assert _is_listing("https://www.amazon.in/s?k=earbuds")
    assert not _is_listing("https://www.youtube.com/watch?v=eVli-tstM5E")
    assert not _is_listing("https://www.amazon.in/dp/B0X")

    agent = DynamicMacAgent.__new__(DynamicMacAgent)  # no network client needed
    agent.listings = {}
    results = Focus(
        "Google Chrome", "tab", "espresso - YouTube", "https://www.youtube.com/results?search_query=espresso"
    )
    video = Focus("Google Chrome", "tab", "Espresso - YouTube", "https://www.youtube.com/watch?v=eVli-tstM5E")
    other = Focus("Google Chrome", "tab", "GitHub", "https://github.com/a/b")
    agent._remember_listing(_ctx("Google Chrome", results))
    assert agent._listing_for(results) == results.id
    assert agent._listing_for(video) == results.id  # "the second one" after the first video opened
    assert agent._listing_for(other) == other.id  # another site: its own page


def test_youtube_results_are_read_without_the_browser_only_on_youtube():
    from macbrow.resolvers import page_result_url

    assert page_result_url("1", "https://www.amazon.in/s?k=x") == ""
    assert page_result_url("1", "https://www.youtube.com/watch?v=x") == ""
