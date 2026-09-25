"""The front app's on-screen controls, read through macOS accessibility, so Jev can *choose* one.

"Open liked songs" with Spotify in front becomes: snapshot every pressable element of Spotify's
window (and its menu bar) with the path of sections it sits in ("Your Library > Liked Songs"),
then one Jev Choice over those elements, then AXPress on the winner. Nothing is generated and
nothing is guessed: every option is an element that exists on screen right now, so a pick can't
point at a button that isn't there. Menu commands are included because they are stable in every
Mac app.

Reading a window takes ~0.3-1 s, so the agent starts the snapshot in parallel with routing.
Needs the Accessibility permission for the process running macbrow (the same one keystrokes need).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("macbrow.ui")

MAX_NODES = 8000  # walk budget per window
MAX_DEPTH = 45
MAX_OPTIONS = 230  # Jev Choice allows 255; leave room for "none"
PRESSABLE_ROLES = {
    "AXButton",
    "AXLink",
    "AXMenuItem",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRow",
    "AXCell",
    "AXTab",
    "AXRadioButton",
    "AXCheckBox",
    "AXDisclosureTriangle",
    "AXOutlineRow",
}
SELECTABLE_ROLES = {"AXRow", "AXOutlineRow", "AXTab", "AXRadioButton"}  # can be chosen by selecting
ROLE_NAMES = {
    "AXButton": "button",
    "AXLink": "link",
    "AXMenuItem": "menu command",
    "AXMenuButton": "menu button",
    "AXPopUpButton": "pop-up menu",
    "AXRow": "list item",
    "AXCell": "list item",
    "AXOutlineRow": "sidebar item",
    "AXTab": "tab",
    "AXRadioButton": "tab",
    "AXCheckBox": "checkbox",
    "AXDisclosureTriangle": "expander",
}
# Pressing one of these changes something that can't be taken back: always confirm by voice.
DANGEROUS = re.compile(
    r"\b(delete|remove|erase|trash|send|post|publish|share|pay|buy|purchase|order|checkout|subscribe|"
    r"unsubscribe|log ?out|sign ?out|quit|uninstall|reset|clear|block|report|discard|don'?t save|replace|"
    r"format|unfollow|leave|archive|empty)\b",
    re.IGNORECASE,
)


@dataclass
class Element:
    label: str
    role: str
    path: tuple[str, ...]  # labels of the sections it sits in, outermost first
    ref: Any = field(repr=False, compare=False, default=None)  # AXUIElement
    in_menu: bool = False
    actions: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return ROLE_NAMES.get(self.role, "control")

    @property
    def where(self) -> str:
        """The nearest sections it sits in ("Main > Your Library"). The row, cell and group wrapping
        a button repeat its own name ("Liked Songs > Liked Songs"); those say nothing, so skip them."""
        own = self.label.lower()
        parts: list[str] = []
        for part in self.path:
            low = part.lower()
            if not part or low in own or own in low or (parts and parts[-1].lower() == low):
                continue
            parts.append(part)
        return " > ".join(parts[-3:])

    def describe(self) -> str:
        if self.in_menu:
            return f"menu command '{self.label}' in the {self.path[-1]} menu"
        where = f" in {self.where}" if self.where else ""
        return f"{self.kind} '{self.label}'{where}"

    @property
    def dangerous(self) -> bool:
        return bool(DANGEROUS.search(self.label))


@dataclass
class Snapshot:
    app: str
    window: str
    elements: list[Element]
    elapsed_ms: float


# ------------------------------------------------------------------------------ reading
def _ax():
    import ApplicationServices as AS  # pyobjc; imported lazily so tests run without it

    return AS


def _get(el: Any, attr: str) -> Any:
    err, value = _ax().AXUIElementCopyAttributeValue(el, attr, None)
    return value if err == 0 else None


def _clean(text: Any) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).replace("\n", " ").strip()
    return re.sub(r"\s+", " ", s)[:90]


def _label(el: Any, role: str) -> str:
    for attr in ("AXTitle", "AXDescription", "AXHelp"):
        v = _clean(_get(el, attr))
        if v:
            return v
    if role in ("AXRow", "AXCell", "AXOutlineRow", "AXLink"):  # rows are labelled by their text
        texts: list[str] = []
        for kid in (_get(el, "AXChildren") or [])[:6]:
            for attr in ("AXValue", "AXTitle", "AXDescription"):
                v = _clean(_get(kid, attr))
                if v:
                    texts.append(v)
                    break
        return _clean(" ".join(texts))
    return ""


_pids: dict[str, int] = {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid(app_name: str) -> int | None:
    """Process id of a running app, by the name System Events reports (the same names the context
    poller uses). Not NSWorkspace: its list is filled once and only refreshed by a Cocoa run loop,
    which this process doesn't run, so an app launched after the assistant started was never found."""
    cached = _pids.get(app_name)
    if cached is not None and _alive(cached):
        return cached
    name = app_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "System Events" to get unix id of first application process whose name is "{name}"'
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip().isdigit():
        _pids.pop(app_name, None)
        return None
    _pids[app_name] = int(out.stdout.strip())
    return _pids[app_name]


def _app_element(app_name: str) -> Any | None:
    pid = _pid(app_name)
    if pid is None:
        return None
    app = _ax().AXUIElementCreateApplication(pid)
    # Electron apps build their web tree when an assistive client sets this; harmless elsewhere.
    _ax().AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    return app


def _wake(app: Any) -> None:
    """Chromium/CEF apps (Spotify) drop their accessibility tree after a while without a screen
    reader and ignore AXManualAccessibility; the VoiceOver signal brings it back (~1 s). Sent only
    when a window looks empty: some apps animate windows oddly while it is set."""
    _ax().AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)


def _looks_empty(win: Any) -> bool:
    """A window whose content is only unlabeled groups: the tree hasn't been built."""
    labelled = 0
    stack, seen = [win], 0
    while stack and seen < 400:
        el = stack.pop()
        seen += 1
        role = _get(el, "AXRole") or ""
        if role not in ("AXGroup", "AXWindow", "AXScrollArea", "AXSplitGroup") and _label(el, role):
            labelled += 1
            if labelled >= 8:
                return False
        stack.extend(_get(el, "AXChildren") or [])
    return True


# Chromium browsers expose their own controls (tabs, bookmarks, toolbar) but build the page's tree
# only for a screen reader, so their window never "looks empty" while the page is missing.
CHROMIUM_BROWSERS = {
    "google chrome",
    "microsoft edge",
    "brave browser",
    "arc",
    "opera",
    "vivaldi",
    "chromium",
    "comet",
    "dia",
}


def web_areas(win: Any, max_depth: int = 30) -> list[Any]:
    """The web pages shown in a window (outermost first); nested frames are not descended into."""
    found: list[Any] = []

    def walk(el: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if _get(el, "AXRole") == "AXWebArea":
            found.append(el)
            return
        for kid in _get(el, "AXChildren") or []:
            walk(kid, depth + 1)

    walk(win, 0)
    return found


def _page_missing(win: Any) -> bool:
    return not any(len(_get(w, "AXChildren") or []) > 0 for w in web_areas(win))


def _front_window(app: Any, wake: bool = True, app_name: str = "") -> Any | None:
    win = _get(app, "AXFocusedWindow") or next(iter(_get(app, "AXWindows") or []), None)
    if win is None or not wake:
        return win
    browser = app_name.lower() in CHROMIUM_BROWSERS
    asleep = (lambda w: _page_missing(w)) if browser else _looks_empty
    if not asleep(win):
        return win
    _wake(app)
    deadline = time.monotonic() + (3.0 if browser else 1.5)
    while time.monotonic() < deadline:
        time.sleep(0.25)
        win = _get(app, "AXFocusedWindow") or win
        if not asleep(win):
            log.info("woke the accessibility tree of %s", app_name or "the app")
            break
    return win


def snapshot(app_name: str, include_menus: bool = True, max_nodes: int = MAX_NODES) -> Snapshot | None:
    """Pressable elements of ``app_name``'s focused window (plus its menu commands)."""
    AS = _ax()
    t0 = time.perf_counter()
    app = _app_element(app_name)
    if app is None:
        return None
    win = _front_window(app, app_name=app_name)
    elements: list[Element] = []
    budget = [max_nodes]
    noise: set[str] = set()

    def walk(el: Any, path: tuple[str, ...], depth: int) -> None:
        if budget[0] <= 0 or depth > MAX_DEPTH:
            return
        budget[0] -= 1
        role = _get(el, "AXRole") or ""
        label = _label(el, role)
        err, actions = AS.AXUIElementCopyActionNames(el, None)
        actions = tuple(actions or ())
        # Cells without a press action are text holders: pressing them silently does nothing.
        if label and ("AXPress" in actions or role in SELECTABLE_ROLES):
            elements.append(Element(label=label, role=role, path=path, ref=el, actions=actions))
        keep = (
            label and role not in ("AXStaticText", "AXImage", "AXWebArea") and label.lower() not in noise and depth > 1
        )
        child_path = path + (label[:40],) if keep else path
        for kid in _get(el, "AXChildren") or []:
            walk(kid, child_path, depth + 1)

    window_title = _clean(_get(win, "AXTitle")) if win is not None else ""
    # Wrapper labels that every element shares (the window, the app, a web view named after the
    # page) say nothing about where an element is; keep them out of paths.
    noise.update({window_title.lower(), app_name.lower()})
    if win is not None:
        walk(win, (), 0)
    if include_menus:
        bar = _get(app, "AXMenuBar")
        for top in (_get(bar, "AXChildren") or [])[1:]:  # skip the Apple menu
            top_title = _clean(_get(top, "AXTitle"))
            for menu in _get(top, "AXChildren") or []:
                for item in _get(menu, "AXChildren") or []:
                    title = _clean(_get(item, "AXTitle"))
                    if title and _get(item, "AXEnabled") is not False:
                        elements.append(
                            Element(label=title, role="AXMenuItem", path=("menu", top_title), ref=item, in_menu=True)
                        )
    return Snapshot(app_name, window_title, elements, (time.perf_counter() - t0) * 1e3)


def fingerprint(app_name: str) -> frozenset[str]:
    """What the window shows right now (labels of its controls), to tell whether a press did anything."""
    snap = snapshot(app_name, include_menus=False)
    return window_fingerprint(snap) if snap else frozenset()


def window_fingerprint(snap: Snapshot) -> frozenset[str]:
    return frozenset([f"window:{snap.window}", *(f"{e.role}:{e.label}" for e in snap.elements if not e.in_menu)])


def _pressable_inside(el: Any, depth: int = 0) -> list[Any]:
    """Buttons/links inside a row: in web-based apps (Spotify) the row ignores AXPress, its button acts."""
    found: list[Any] = []
    if depth > 4:
        return found
    for kid in _get(el, "AXChildren") or []:
        role = _get(kid, "AXRole") or ""
        err, actions = _ax().AXUIElementCopyActionNames(kid, None)
        if role in ("AXButton", "AXLink") and "AXPress" in (actions or ()):
            found.append(kid)
        found.extend(_pressable_inside(kid, depth + 1))
    return found


def _system_events(body: str) -> bool:
    """Input goes through System Events: synthetic events posted from this process (Quartz) are
    not delivered to other apps without a separate permission, while System Events' are."""
    try:
        out = subprocess.run(
            ["osascript", "-e", f'tell application "System Events"\n{body}\nend tell'],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def _as_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _click_center(ref: Any) -> bool:
    """A real mouse click at the element's centre (the element must be on screen, app in front)."""
    pos, size = _get(ref, "AXPosition"), _get(ref, "AXSize")
    if pos is None or size is None:
        return False
    ok_p, point = _ax().AXValueGetValue(pos, _ax().kAXValueCGPointType, None)
    ok_s, dims = _ax().AXValueGetValue(size, _ax().kAXValueCGSizeType, None)
    if not (ok_p and ok_s) or dims.width <= 0 or dims.height <= 0:
        return False
    x, y = int(point.x + dims.width / 2), int(point.y + dims.height / 2)
    return _system_events(f"click at {{{x}, {y}}}")


def press_verified(
    el: Element, app_name: str, before: frozenset[str], allow_click: bool, settle_s: float = 0.7
) -> str | None:
    """Press ``el`` and confirm the window changed; try the next way when it didn't. Returns how it
    worked ("inner button", "press", "select", "click", "menu") or None when nothing visibly happened."""
    AS = _ax()
    if el.in_menu:  # menu commands act reliably and often open another window; no page to compare
        return "menu" if AS.AXUIElementPerformAction(el.ref, "AXPress") == 0 else None

    def changed() -> bool:
        time.sleep(settle_s)
        return fingerprint(app_name) != before

    attempts: list[tuple[str, Any]] = []
    if el.role in ("AXRow", "AXCell", "AXOutlineRow"):
        attempts += [("inner button", ref) for ref in _pressable_inside(el.ref)[:2]]
    if "AXPress" in el.actions:
        attempts.append(("press", el.ref))
    if el.role in SELECTABLE_ROLES:
        attempts.append(("select", el.ref))
    if allow_click:
        attempts.append(("click", el.ref))
    for how, ref in attempts:
        if how == "click":
            done = _click_center(ref)
        elif how == "select":
            done = AS.AXUIElementSetAttributeValue(ref, "AXSelected", True) == 0
        else:
            done = AS.AXUIElementPerformAction(ref, "AXPress") == 0
        if done and changed():
            return how
    return None


# ------------------------------------------------------------------------------ choosing
_WORD = re.compile(r"[a-z0-9]+")
_ACTS = {"AXButton": 3, "AXLink": 3, "AXMenuItem": 3, "AXTab": 2, "AXRadioButton": 2, "AXRow": 1, "AXOutlineRow": 1}


def _words(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


def candidates(snap: Snapshot, utterance: str, limit: int = MAX_OPTIONS) -> list[Element]:
    """De-duplicated elements, the ones sharing words with the utterance first, capped for a Choice."""
    seen: dict[tuple[str, bool], Element] = {}
    for el in snap.elements:
        key = (el.label.lower(), el.in_menu)
        # a row and the button inside it usually carry the same label: keep the button, it acts
        if key not in seen or _ACTS.get(el.role, 0) > _ACTS.get(seen[key].role, 0):
            seen[key] = el
    unique = list(seen.values())
    if len(unique) <= limit:
        return unique
    said = _words(utterance)
    order = {id(e): i for i, e in enumerate(unique)}
    unique.sort(key=lambda e: (-len(said & _words(e.label + " " + e.where)), e.in_menu, order[id(e)]))
    return unique[:limit]


def base_key(key: str) -> str:
    """Option key without the " (2)" suffix that told identical labels apart."""
    return re.sub(r" \(\d+\)$", "", key).lower()


def _same_target(a: str, b: str) -> bool:
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def pick_confidence(choice: str, probabilities: dict[str, float]) -> float:
    """Probability of the chosen target, counting every option that names the same thing: "Arijit
    Singh" in the library and "Arijit Singh Artist" on the home page are one intent, not two."""
    base = base_key(choice)
    return sum(p for k, p in probabilities.items() if k != "__none__" and _same_target(base_key(k), base))


def choice_criteria(elements: list[Element]) -> tuple[dict[str, str], dict[str, Element]]:
    """Choice options keyed by the element's own label (unique-ified), described with its place."""
    crit: dict[str, str] = {}
    by_key: dict[str, Element] = {}
    for el in elements:
        key = el.label[:70]
        n = 2
        while key in by_key:
            key = f"{el.label[:64]} ({n})"
            n += 1
        by_key[key] = el
        crit[key] = el.describe()
    return crit, by_key


def press_args(el: Element) -> dict[str, str]:
    """What a staged (confirm-first) press remembers; the element itself may be rebuilt by then."""
    return {"label": el.label, "where": el.where, "menu": "1" if el.in_menu else ""}


def find(snap: Snapshot, args: dict[str, str]) -> Element | None:
    """The element a staged press meant, in a fresh snapshot."""
    menu = bool(args.get("menu"))
    same = [e for e in snap.elements if e.label == args["label"] and e.in_menu == menu]
    return next((e for e in same if e.where == args.get("where", "")), same[0] if same else None)


def dump_tree(app_name: str, max_nodes: int = MAX_NODES) -> list[str]:
    """The front window's raw accessibility tree, indented by depth: role, label, actions. For looking."""
    AS = _ax()
    app = _app_element(app_name)
    if app is None:
        return [f"{app_name} is not running"]
    win = _front_window(app, app_name=app_name)
    lines: list[str] = []
    budget = [max_nodes]

    def walk(el: Any, depth: int) -> None:
        if budget[0] <= 0 or depth > MAX_DEPTH:
            return
        budget[0] -= 1
        role = _get(el, "AXRole") or "?"
        label = _label(el, role)
        err, actions = AS.AXUIElementCopyActionNames(el, None)
        acts = [a.removeprefix("AX") for a in (actions or ()) if a in ("AXPress", "AXShowMenu", "AXPick", "AXConfirm")]
        line = "  " * depth + role.removeprefix("AX")
        if label:
            line += f"  '{label}'"
        if acts:
            line += f"  [{', '.join(acts)}]"
        lines.append(line)
        for kid in _get(el, "AXChildren") or []:
            walk(kid, depth + 1)

    if win is not None:
        walk(win, 0)
    if budget[0] <= 0:
        lines.append(f"... stopped after {max_nodes} elements")
    return lines


def screen_text(app_name: str, max_chars: int = 9000, max_nodes: int = MAX_NODES) -> str:
    """The text the front window shows, in reading order, one line per labelled element:
    for answering "what songs can you see" or "is X on this page". Menus are left out.

    A row's pieces ("Pinned", "Playlist", "•") repeat its own label and a list shown twice repeats
    itself; both are dropped so the page itself fits, not just the sidebar."""
    app = _app_element(app_name)
    if app is None:
        return ""
    win = _front_window(app, app_name=app_name)
    if win is None:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    budget = [max_nodes]
    size = [0]

    def keep(text: str) -> bool:
        low = text.lower()
        if len(text.strip(" •·|-–—:")) < 2 or low in seen:
            return False
        return not any(low in prev.lower() for prev in lines[-4:])  # a piece of a label just shown

    def walk(el: Any, depth: int) -> None:
        if budget[0] <= 0 or depth > MAX_DEPTH or size[0] > max_chars:
            return
        budget[0] -= 1
        role = _get(el, "AXRole") or ""
        text = _clean(_get(el, "AXValue")) if role in ("AXStaticText", "AXHeading", "AXTextField") else ""
        text = text or _clean(_get(el, "AXTitle")) or _clean(_get(el, "AXDescription"))
        if text and role not in ("AXGroup", "AXWindow", "AXWebArea") and keep(text):
            seen.add(text.lower())
            lines.append(("heading: " if role == "AXHeading" else "") + text)
            size[0] += len(text) + 1
        for kid in _get(el, "AXChildren") or []:
            walk(kid, depth + 1)

    pages = web_areas(win)
    if pages:
        # A browser: the page is what "what can you see" is about, not tabs and bookmarks.
        page = max(pages, key=lambda w: len(_get(w, "AXChildren") or []))
        title = _clean(_get(page, "AXTitle")) or _clean(_get(page, "AXDescription"))
        if title:
            lines.append(f"page: {title}")
        walk(page, 0)
    else:
        walk(win, 0)
    return "\n".join(lines)[:max_chars]


# ------------------------------------------------------------------------------ searching in an app
# "Search for X" in any app: the app's own search field, found in its accessibility tree (not a
# per-app keyboard shortcut), focused and checked before typing, results checked after.
FIELD_ROLES = {"AXTextField", "AXSearchField", "AXComboBox"}
_SEARCHY = re.compile(r"\b(search|find|filter|look ?up|look for)\b|^what do you want to", re.IGNORECASE)
# Text boxes that are for writing, not searching: typing a query there would put it in a message,
# a document or a form.
_NOT_SEARCH = re.compile(
    r"\b(message|reply|comment|compose|write|password|e-?mail|subject|caption|note|chat|type a|address)\b",
    re.IGNORECASE,
)
_LIST_ROLES = {"AXRow", "AXCell", "AXOutline", "AXTable", "AXList"}  # editable names in lists (Notes folders)


@dataclass
class Field:
    ref: Any = field(repr=False, compare=False)
    role: str
    subrole: str
    label: str  # description, placeholder or title
    in_list: bool
    in_toolbar: bool

    @property
    def score(self) -> int:
        """How clearly this is the app's search box; 0 = never type a search here."""
        if self.in_list or _NOT_SEARCH.search(self.label) or self.subrole == "AXSecureTextField":
            return 0
        points = 0
        if self.role == "AXSearchField" or self.subrole == "AXSearchField":
            points += 5
        if _SEARCHY.search(self.label):
            points += 3
        if self.role == "AXComboBox":
            points += 2
        if self.in_toolbar:
            points += 1
        return points


def search_fields(app_name: str, max_nodes: int = MAX_NODES) -> list[Field]:
    """Text fields of the front window that could be its search box, best first."""
    app = _app_element(app_name)
    win = _front_window(app, app_name=app_name) if app is not None else None
    if win is None:
        return []
    found: list[Field] = []
    budget = [max_nodes]

    def walk(el: Any, ancestors: tuple[str, ...], depth: int) -> None:
        if budget[0] <= 0 or depth > MAX_DEPTH:
            return
        budget[0] -= 1
        role = _get(el, "AXRole") or ""
        if role in FIELD_ROLES:
            label = next(
                (
                    v
                    for v in (_clean(_get(el, a)) for a in ("AXPlaceholderValue", "AXDescription", "AXTitle", "AXHelp"))
                    if v
                ),
                "",
            )
            found.append(
                Field(
                    ref=el,
                    role=role,
                    subrole=_get(el, "AXSubrole") or "",
                    label=label,
                    in_list=any(a in _LIST_ROLES for a in ancestors),
                    in_toolbar="AXToolbar" in ancestors,
                )
            )
        for kid in _get(el, "AXChildren") or []:
            walk(kid, ancestors + (role,), depth + 1)

    walk(win, (), 0)
    ranked = [f for f in found if f.score >= 2]
    ranked.sort(key=lambda f: -f.score)  # stable: equal scores keep reading order
    return ranked


def _search_button(snap: Snapshot) -> Element | None:
    """A button that reveals a hidden search box (Spotify's and Slack's "Search")."""
    for el in snap.elements:
        low = el.label.lower()
        if el.in_menu or el.role not in ("AXButton", "AXLink", "AXRadioButton"):
            continue
        if low == "search" or (low.startswith("search") and "result" not in low and len(low) < 30):
            return el
    return None


def _same_element(a: Any, b: Any) -> bool:
    from CoreFoundation import CFEqual

    return a is not None and b is not None and bool(CFEqual(a, b))


def _has_focus(app: Any, ref: Any) -> bool:
    """The field, or the text box inside or around it (combo boxes focus a child), has the focus."""
    focused = _get(app, "AXFocusedUIElement")
    if focused is None:
        return False
    node = focused
    for _ in range(4):  # focused element is the field or sits inside it
        if _same_element(node, ref):
            return True
        node = _get(node, "AXParent")
        if node is None:
            break
    return any(_same_element(focused, kid) for kid in (_get(ref, "AXChildren") or []))


def _replace_text_and_enter(text: str) -> bool:
    """Select what the focused box holds, type ``text`` over it, press Return."""
    return _system_events(
        f'keystroke "a" using {{command down}}\ndelay 0.05\nkeystroke {_as_string(text)}\ndelay 0.2\nkey code 36'
    )


def search_in_app(app_name: str, query: str, settle_s: float = 1.0) -> str:
    """Search ``query`` with the front app's own search box. Returns "searched" (results changed),
    "no change" (typed, but nothing appeared), "no field" or "no focus" (nothing was typed: the
    caller may fall back to a keyboard shortcut)."""
    snap = snapshot(app_name, include_menus=False)
    if snap is None:
        return "no field"
    fields = search_fields(app_name)
    if not fields:
        button = _search_button(snap)
        if button is None or _ax().AXUIElementPerformAction(button.ref, "AXPress") != 0:
            return "no field"
        time.sleep(0.7)  # the box slides in
        fields = search_fields(app_name)
        if not fields:
            return "no field"
    target = fields[0]
    app = _app_element(app_name)
    _ax().AXUIElementSetAttributeValue(target.ref, "AXFocused", True)
    time.sleep(0.15)
    if not _has_focus(app, target.ref):
        _click_center(target.ref)
        time.sleep(0.25)
        if not _has_focus(app, target.ref):
            return "no focus"  # never type into something that isn't the search box
    before = window_fingerprint(snapshot(app_name, include_menus=False) or snap)
    if not _replace_text_and_enter(query):
        return "no focus"
    for _ in range(3):  # results can take a moment
        time.sleep(settle_s)
        if fingerprint(app_name) != before:
            return "searched"
    return "no change"
