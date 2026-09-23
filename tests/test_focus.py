from macbrow.applescript import ContextPoller, MacContext
from macbrow.focus import Focus, parse_probe, probed_apps, summarize_sdef
from macbrow.registry import ToolRegistry
from macbrow.router import _state

MY_NOTE = Focus(app="Notes", kind="note", name="My_Note", id="x-coredata://X/ICNote/p1")
TAB = Focus(app="Google Chrome", kind="tab", name="GitHub", id="https://github.com")


def test_parse_probe():
    assert parse_probe("Notes", "note", "My_Note\tx-coredata://X/ICNote/p1\n") == MY_NOTE
    assert parse_probe("Finder", "folder", "Desktop\t") == Focus("Finder", "folder", "Desktop", "")
    assert parse_probe("Notes", "note", "") is None
    assert parse_probe("Notes", "note", "\t") is None


def test_focus_in_prefers_live_then_remembered():
    ctx = MacContext("Google Chrome", ["Google Chrome", "Notes"], [], focus=TAB, recent=(MY_NOTE,))
    assert ctx.focus_in("notes") == MY_NOTE  # the note stays reachable while Chrome is in front
    assert ctx.focus_in("Google Chrome") == TAB
    assert ctx.focus_in("Finder") is None


def test_poller_remembers_other_apps_newest_first():
    p = ContextPoller(memory_s=60)
    p.remember(MY_NOTE, now=100)
    p.remember(TAB, now=110)
    assert p.recent_focus("Finder", now=120) == (TAB, MY_NOTE)
    assert p.recent_focus("Google Chrome", now=120) == (MY_NOTE,)  # the live one is not repeated
    assert p.recent_focus("", now=120) == (TAB, MY_NOTE)
    assert p.recent_focus("Finder", now=165) == (TAB,)  # the note expired after 60 s


def test_router_state_carries_focus():
    ctx = MacContext("Notes", ["Notes"], [], focus=MY_NOTE, recent=(TAB,))
    st = _state("write buy milk", ctx)
    assert st["open_in_frontmost_app"] == {"app": "Notes", "note": "My_Note"}
    assert st["recently_worked_on"] == [{"app": "Google Chrome", "tab": "GitHub"}]
    assert "open_in_frontmost_app" not in _state("hi", MacContext("Finder", [], []))


def test_notes_write_targets_the_focused_note():
    tool = ToolRegistry().get("notes_write")
    script = tool.render({"text": 'buy "milk"'}, MY_NOTE)
    assert 'set nid to "x-coredata://X/ICNote/p1"' in script
    assert 'set t to "buy \\"milk\\""' in script
    assert 'set nid to ""' in tool.render({"text": "x"})  # nothing open: the script falls back to the selection
    assert not tool.blocked and not tool.risky


def test_notes_create_args_are_optional():
    tool = ToolRegistry().get("notes_create")
    assert all(a.optional for a in tool.args)


def test_summarize_sdef():
    xml = """<dictionary><suite name="Standard Suite"><command name="close"/></suite>
    <suite name="Notes Suite"><command name="show" description="Show an object"/>
    <class name="note"><property name="name"/><property name="id" access="r"/>
    <element type="attachment"/></class></suite></dictionary>"""
    assert summarize_sdef(xml) == (
        "command show: Show an object\nclass note: properties [name, id(r/o)] elements [attachment]"
    )


def test_probed_apps_from_tool_script():
    script = ToolRegistry().get("notes_create").render({"title": "x"})
    assert probed_apps(script) == ["Notes"]
    assert probed_apps('tell application "Spotify" to playpause') == []  # no probe for Spotify
