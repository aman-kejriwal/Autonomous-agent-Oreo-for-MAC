"""Thin async wrappers around osascript and macOS environment probing."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .focus import Focus, probe_focus

OSASCRIPT_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    output: str
    error: str = ""

    @property
    def text(self) -> str:
        return self.output if self.ok else self.error


async def run_applescript(script: str, timeout: float = OSASCRIPT_TIMEOUT_S) -> ScriptResult:
    """Run an AppleScript source string with osascript and capture its result."""
    proc = await asyncio.create_subprocess_exec(
        "osascript",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(script.encode("utf-8")), timeout)
    except TimeoutError:
        proc.kill()
        return ScriptResult(False, "", f"AppleScript timed out after {timeout:.0f}s")
    output = out.decode("utf-8", "replace").strip()
    error = err.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        # osascript prefixes errors like "123:145: execution error: ... (-1728)"
        error = re.sub(r"^\d+:\d+:\s*(execution|syntax) error:\s*", "", error)
        return ScriptResult(False, output, error or f"osascript exited {proc.returncode}")
    return ScriptResult(True, output, error)


def escape_applescript_string(value: str) -> str:
    """Escape a Python string for interpolation inside an AppleScript "..." literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


_FRONTMOST = 'tell application "System Events" to get name of first application process whose frontmost is true'
_RUNNING = 'tell application "System Events" to get name of every application process whose background only is false'


async def get_active_app(fallback: str = "Finder") -> str:
    res = await run_applescript(_FRONTMOST, timeout=5)
    return res.output if res.ok and res.output else fallback


async def get_running_apps(fallback: list[str] | None = None) -> list[str]:
    """``fallback`` (default ["Finder"]) when System Events fails, which it briefly does while
    apps are being activated; the poller passes its last good list so app tools don't vanish."""
    res = await run_applescript(_RUNNING, timeout=5)
    if not res.ok or not res.output:
        return fallback or ["Finder"]
    apps = [a.strip() for a in res.output.split(",") if a.strip()]
    return sorted(set(apps), key=str.lower)


_APP_DIRS = (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")


def get_installed_apps(limit: int = 200) -> list[str]:
    """Names of installed .app bundles (top level only). Cheap filesystem scan."""
    names: set[str] = set()
    for d in _APP_DIRS:
        if not d.is_dir():
            continue
        for p in d.glob("*.app"):
            names.add(p.stem)
    return sorted(names, key=str.lower)[:limit]


@dataclass(frozen=True)
class MacContext:
    active_app: str
    running_apps: list[str]
    installed_apps: list[str]
    focus: Focus | None = None  # what is open in active_app (the note, tab, folder...)
    recent: tuple[Focus, ...] = ()  # what was open in other apps the user worked in lately, newest first

    def focus_in(self, app: str) -> Focus | None:
        """The focused object of ``app``: live if it is in front, else remembered."""
        for f in (self.focus, *self.recent):
            if f is not None and f.app.lower() == app.lower():
                return f
        return None


async def snapshot_context() -> MacContext:
    active, running = await asyncio.gather(get_active_app(), get_running_apps())
    return MacContext(
        active_app=active, running_apps=running, installed_apps=get_installed_apps(), focus=await probe_focus(active)
    )


class ContextPoller:
    """Keeps a fresh MacContext in the background so a voice turn never waits on osascript.

    Frontmost/running apps and the focused object of the frontmost app refresh every
    ``interval`` seconds; the installed-app scan (filesystem) every ``installed_interval``.
    The last focus seen in each app is remembered for ``memory_s`` seconds so "write that in
    my note" still finds the note after the user switched to Chrome.
    """

    MAX_RECENT = 4

    def __init__(self, interval: float = 1.0, installed_interval: float = 60.0, memory_s: float = 30 * 60):
        self.interval = interval
        self.installed_interval = installed_interval
        self.memory_s = memory_s
        self._ctx: MacContext | None = None
        self._task: asyncio.Task | None = None
        self._installed: list[str] = []
        self._installed_at = 0.0
        self._seen: dict[str, tuple[Focus, float]] = {}  # app (lowercase) -> (last focus, monotonic time)
        self._ctx_started = 0.0  # when the stored snapshot started reading: an older read never overwrites it

    async def start(self) -> MacContext:
        if self._task is None:
            self._ctx = await self._refresh()
            self._task = asyncio.create_task(self._loop(), name="macbrow-context-poller")
        assert self._ctx is not None
        return self._ctx

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def latest(self) -> MacContext:
        """Cached snapshot; starts the poller on first use."""
        if self._ctx is None:
            return await self.start()
        return self._ctx

    async def refresh(self, touched: list[str] | tuple[str, ...] = ()) -> MacContext:
        """Re-read now (after a tool ran) so the next turn sees the note/tab it just opened.

        ``touched`` are apps the tool drove: they are probed even if not (yet) in front, because
        right after `activate` macOS can still report the previous app as frontmost.
        """
        for app in touched:
            focus = await probe_focus(app)
            if focus is not None:
                self.remember(focus)
        try:
            await self._store()
        except Exception:
            pass
        return await self.latest()

    async def _store(self) -> None:
        """Read and keep the result, unless a read that started later was already kept: the
        background loop and an on-demand refresh overlap, and the slower, older read must not
        put an app that was just replaced back in front."""
        started = time.monotonic()
        ctx = await self._refresh()
        if started >= self._ctx_started:
            self._ctx, self._ctx_started = ctx, started

    def remember(self, focus: Focus, now: float | None = None) -> None:
        self._seen[focus.app.lower()] = (focus, time.monotonic() if now is None else now)

    def recent_focus(self, active_app: str, now: float | None = None) -> tuple[Focus, ...]:
        now = time.monotonic() if now is None else now
        live = [(f, t) for f, t in self._seen.values() if now - t <= self.memory_s]
        live.sort(key=lambda ft: -ft[1])
        return tuple(f for f, _ in live if f.app.lower() != active_app.lower())[: self.MAX_RECENT]

    async def _refresh(self) -> MacContext:
        now = time.monotonic()
        if now - self._installed_at > self.installed_interval:
            self._installed = get_installed_apps()
            self._installed_at = now
        prev = self._ctx
        active, running = await asyncio.gather(
            get_active_app(prev.active_app if prev else "Finder"),
            get_running_apps(prev.running_apps if prev else None),
        )
        focus = await probe_focus(active)
        if focus is not None:
            self.remember(focus)
        running_lower = {a.lower() for a in running}
        for app in [a for a in self._seen if a not in running_lower]:  # forget apps that were quit
            del self._seen[app]
        return MacContext(
            active_app=active,
            running_apps=running,
            installed_apps=self._installed,
            focus=focus,
            # nothing open in front right now: keep what was last open there available too
            recent=self.recent_focus(active if focus else ""),
        )

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self._store()
            except Exception:  # keep polling on transient osascript failures
                pass
