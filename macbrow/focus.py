"""What the user is working on right now: the note, tab, folder or message open in the front app.

The router uses this to resolve implicit requests ("write buy milk" -> the note that is open),
and tools reach the focused object through the built-in placeholders ``{{focus_id}}`` and
``{{focus_name}}``. The frontmost app is probed every second, and an app a tool just drove is
probed right after it runs; probes never launch an app. What was open in apps the user left is
remembered by the ContextPoller.

``app_dictionary`` summarises an app's AppleScript dictionary (its bundled .sdef) so the
codegen tier writes scripts against commands the app really has instead of guessing.
"""

from __future__ import annotations

import functools
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class Focus:
    app: str
    kind: str  # "note" | "tab" | "folder" | "document" | "message" | "window" (any other app)
    name: str
    id: str = ""  # stable handle a script can target: note id, URL, POSIX path...

    def describe(self) -> dict[str, str]:
        d = {"app": self.app, self.kind: self.name}
        if self.kind == "tab" and self.id.startswith("http"):
            d["url"] = self.id[:160]  # results page vs video/product page matters for "play the first one"
        return d


# Each probe returns "name<TAB>id", or "" when nothing is open. Guarded by `is running` so a
# probe racing an app quit never relaunches it. The separator is `sep` (set outside the tell
# block): inside `tell application "Google Chrome"` the word `tab` means a browser tab.
_PROBES: dict[str, tuple[str, str]] = {
    "Notes": (
        "note",
        """tell application "Notes"
  set s to selection
  if (count of s) is 0 then return ""
  set n to item 1 of s
  return (name of n) & sep & (id of n)
end tell""",
    ),
    "Google Chrome": (
        "tab",
        """tell application "Google Chrome"
  if (count of windows) is 0 then return ""
  set t to active tab of front window
  return (title of t) & sep & (URL of t)
end tell""",
    ),
    "Safari": (
        "tab",
        """tell application "Safari"
  if (count of windows) is 0 then return ""
  set t to current tab of front window
  return (name of t) & sep & (URL of t)
end tell""",
    ),
    "Finder": (
        "folder",
        """tell application "Finder"
  if (count of Finder windows) is 0 then return ""
  set f to target of front Finder window
  return (name of f) & sep & (POSIX path of (f as alias))
end tell""",
    ),
    "TextEdit": (
        "document",
        """tell application "TextEdit"
  if (count of documents) is 0 then return ""
  set d to front document
  set p to ""
  try
    set p to path of d
  end try
  return (name of d) & sep & p
end tell""",
    ),
    "Mail": (
        "message",
        """tell application "Mail"
  set s to selection
  if (count of s) is 0 then return ""
  set m to item 1 of s
  return (subject of m) & sep & (message id of m)
end tell""",
    ),
}


def probed_apps(script: str) -> list[str]:
    """Apps a tool script talks to (`tell application "X"`) that have a focus probe."""
    return [a for a in dict.fromkeys(re.findall(r'application\s+"([^"]+)"', script)) if a in _PROBES]


def parse_probe(app: str, kind: str, output: str) -> Focus | None:
    name, _, ident = output.strip("\n").partition("\t")
    name = name.strip()
    if not name:
        return None
    return Focus(app=app, kind=kind, name=name, id=ident.strip())


# Any other app: the title of its front window (a Safari/Chrome web app shows "... - YouTube",
# an editor the file name). Through System Events, so an unknown name never prompts for an app.
_WINDOW_PROBE = """tell application "System Events"
  if not (exists process "{app}") then return ""
  tell process "{app}"
    if (count of windows) is 0 then return ""
    return (name of front window) & sep & ""
  end tell
end tell"""


async def probe_focus(app: str) -> Focus | None:
    """What is open in ``app``: the frontmost app, or one a tool just drove. None if unknown or empty."""
    from .applescript import escape_applescript_string, run_applescript  # applescript imports this module

    probe = _PROBES.get(app)
    if probe is None:
        kind = "window"
        script = "set sep to character id 9\n" + _WINDOW_PROBE.replace("{app}", escape_applescript_string(app))
    else:
        kind, body = probe
        script = f'set sep to character id 9\nif application "{app}" is running then\n{body}\nend if\nreturn ""'
    res = await run_applescript(script, timeout=PROBE_TIMEOUT_S)
    return parse_probe(app, kind, res.output) if res.ok else None


# ------------------------------------------------------------------ scripting dictionaries
_APP_DIRS = (
    Path("/Applications"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path("/System/Library/CoreServices"),
    Path.home() / "Applications",
)
_SKIP_SUITES = {"Standard Suite", "Text Suite", "Type Definitions"}


def _sdef_path(app: str) -> Path | None:
    for d in _APP_DIRS:
        res = d / f"{app}.app" / "Contents" / "Resources"
        if res.is_dir():
            found = sorted(res.glob("*.sdef"))
            if found:
                return found[0]
    return None


def summarize_sdef(xml_text: str, limit: int = 3000) -> str:
    """Commands and class properties from an .sdef, one line each; (r/o) marks read-only."""
    root = ET.fromstring(xml_text)
    lines: list[str] = []
    for suite in root.iter("suite"):
        if suite.get("name") in _SKIP_SUITES:
            continue
        for c in suite.findall("command"):
            desc = (c.get("description") or "").strip()
            lines.append(f"command {c.get('name')}" + (f": {desc}" if desc else ""))
        for c in [*suite.findall("class"), *suite.findall("class-extension")]:
            props = ", ".join(
                p.get("name", "") + ("(r/o)" if p.get("access") == "r" else "") for p in c.findall("property")
            )
            elements = ", ".join(e.get("type", "") for e in c.findall("element"))
            lines.append(f"class {c.get('name') or c.get('extends')}: properties [{props}] elements [{elements}]")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n..."


@functools.lru_cache(maxsize=32)
def app_dictionary(app: str) -> str:
    """Compact scripting dictionary of ``app``, or "" when it has none (then GUI scripting/URL schemes)."""
    path = _sdef_path(app)
    if path is None:
        return ""
    try:
        return summarize_sdef(path.read_text(errors="replace"))
    except (OSError, ET.ParseError):
        return ""
