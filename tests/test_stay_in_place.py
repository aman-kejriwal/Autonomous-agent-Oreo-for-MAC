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
