"""User-level Claude Code hooks: the git guard (H5) and the Stop gate (H6) for every
slopbrake-managed repo a session touches, wherever the session started. Claude Code loads
project hooks only from the session's starting directory, so these run from
~/.claude/settings.json and act only inside repos whose toplevel has .claude/slopbrake.json.

  slopbrake hook pre-tool-use|post-tool-use|stop      hook JSON on stdin
  slopbrake user-hooks install|uninstall|status [--settings PATH]
"""
from __future__ import annotations

import fcntl
import importlib.util
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

KIT_GUARD = Path(__file__).resolve().parent / "kit/common/.claude/hooks/git_guard.py"
PREFIX = "slopbrake hook"  # identifies our entries in the user's settings
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
USER_ENTRIES = {
    "PreToolUse": {"matcher": "Bash", "hooks": [{"type": "command", "command": f"{PREFIX} pre-tool-use"}]},
    "PostToolUse": {"matcher": "|".join((*EDIT_TOOLS, "Bash")),
                    "hooks": [{"type": "command", "command": f"{PREFIX} post-tool-use"}]},
    "Stop": {"hooks": [{"type": "command", "command": f"{PREFIX} stop", "timeout": 600}]},
}
OPERATORS = set("();<>|&")


class SettingsError(ValueError):
    """A Claude Code settings file slopbrake must not rewrite (or must not wire up yet)."""


# ── repos ────────────────────────────────────────────────────────────────────

def toplevel(path) -> Path | None:
    path = Path(path).expanduser()
    while not path.is_dir():  # a file, or one about to be created
        if path.parent == path:
            return None
        path = path.parent
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True,
                            check=False)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def managed_toplevel(path) -> Path | None:
    top = toplevel(path)
    return top if top and (top / ".claude/slopbrake.json").is_file() else None


# ── settings ─────────────────────────────────────────────────────────────────

def read_json(path: Path):
    """The parsed file, or None when it does not exist."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc})") from None


def load_settings(path: Path) -> dict:
    """Settings that are safe to merge into: an object whose hooks map events to lists."""
    data = read_json(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise SettingsError(f"{path}: hooks must be an object")
    for event, entries in (hooks or {}).items():
        if entries is not None and not (isinstance(entries, list) and all(isinstance(e, dict) for e in entries)):
            raise SettingsError(f"{path}: hooks.{event} must be a list of hook entries")
    return data


def hook_entries(settings: dict, event: str) -> list[dict]:
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def commands(entry: dict) -> list[str]:
    hooks = entry.get("hooks")
    return [str(h.get("command", "")) for h in hooks if isinstance(h, dict)] if isinstance(hooks, list) else []


def matches(matcher, tool: str) -> bool:
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(str(matcher), tool) is not None
    except re.error:
        return matcher == tool


def registered(settings, event: str, script: str, tool: str | None = None) -> bool:
    return any(script in command for entry in hook_entries(settings, event)
               if tool is None or matches(entry.get("matcher"), tool) for command in commands(entry))


def settings_gaps(repo: Path, local: Path | None = None) -> list[str]:
    """Why Claude Code would not run the kit's hooks for repo; empty when they are wired.
    settings.local.json is read from `local` (default repo): it is never committed."""
    try:
        settings = read_json(repo / ".claude/settings.json")
        local_settings = read_json((local or repo) / ".claude/settings.local.json")
    except SettingsError as exc:
        return [str(exc)]
    gaps = []
    if settings is None:
        gaps.append("missing .claude/settings.json")
    else:
        if not registered(settings, "PreToolUse", "block-dangerous-git.sh", "Bash"):
            gaps.append(".claude/settings.json does not run block-dangerous-git.sh on PreToolUse for Bash (H5)")
        if not registered(settings, "Stop", "require-green.sh"):
            gaps.append(".claude/settings.json does not run require-green.sh on Stop (H6)")
    for name, data in (("settings.json", settings), ("settings.local.json", local_settings)):
        if isinstance(data, dict) and data.get("disableAllHooks"):
            gaps.append(f".claude/{name} sets disableAllHooks")
    for hook in ("block-dangerous-git.sh", "require-green.sh"):
        path = repo / ".claude/hooks" / hook
        if not path.is_file():
            gaps.append(f"missing .claude/hooks/{hook}")
        elif not os.access(path, os.X_OK):
            gaps.append(f".claude/hooks/{hook} is not executable")
    return gaps


# ── hook events ──────────────────────────────────────────────────────────────

def state_path(session_id) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "slopbrake/sessions" / (re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "unknown")) + ".json")


def touched(session_id) -> list[str]:
    try:
        return json.loads(state_path(session_id).read_text(encoding="utf-8")).get("repos", [])
    except (OSError, ValueError, AttributeError):
        return []


def record(session_id, repos: set[str]) -> None:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # parallel tool calls write the same file
        handle.seek(0)
        try:
            known = set(json.loads(handle.read() or "{}").get("repos", []))
        except (ValueError, AttributeError):
            known = set()
        if not repos <= known:
            handle.seek(0)
            handle.truncate()
            json.dump({"repos": sorted(known | repos)}, handle)


def bash_dirs(command: str, cwd: Path) -> list[Path]:
    """The directories a shell command works in: its cwd, `cd <dir>` and `git -C <dir>` targets."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        words = command.split()
    dirs, in_git = [cwd], False
    for word, following in itertools.pairwise(words):
        if word and set(word) <= OPERATORS:
            in_git = False
            continue
        in_git = in_git or Path(word).name == "git"
        if word == "cd" or (word == "-C" and in_git):
            dirs.append(cwd / Path(following).expanduser())
    return dirs


def post_tool_use(payload: dict) -> int:
    cwd = Path(payload.get("cwd") or os.getcwd())
    args = payload.get("tool_input") or {}
    if payload.get("tool_name") in EDIT_TOOLS:
        target = args.get("file_path") or args.get("notebook_path")
        candidates = [cwd / Path(target).expanduser()] if target else []
    elif payload.get("tool_name") == "Bash":
        candidates = bash_dirs(str(args.get("command", "")), cwd)
    else:
        candidates = []
    repos = {str(top) for top in map(managed_toplevel, candidates) if top}
    if repos:
        record(payload.get("session_id"), repos)
    return 0


def stop(payload: dict, text: str) -> int:
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    project_top = toplevel(project) if project else None
    failures = []
    for repo in map(Path, touched(payload.get("session_id"))):
        hook = repo / ".claude/hooks/require-green.sh"
        if not (repo / ".claude/slopbrake.json").is_file() or not hook.is_file():
            continue
        if repo == project_top and not settings_gaps(repo):
            continue  # the project's own Stop hook already gates it
        result = subprocess.run([str(hook)], input=text, cwd=repo, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(repo)),
                                capture_output=True, text=True, check=False)
        if result.returncode == 2:
            failures.append(f"{repo}:\n{result.stderr.strip()}")
    if failures:
        print("\n\n".join(failures), file=sys.stderr)
        return 2
    return 0


def pre_tool_use(payload: dict, guard: Path = KIT_GUARD) -> int:
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    spec = importlib.util.spec_from_file_location("slopbrake_git_guard", guard)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reason = module.check_command(command, str(payload.get("cwd") or os.getcwd()), "managed")
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def run(event: str, text: str) -> int:
    """Exit code for one hook event. Errors never break sessions outside managed repos."""
    payload = None
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return 0
        if event == "pre-tool-use":
            return pre_tool_use(payload)
        return post_tool_use(payload) if event == "post-tool-use" else stop(payload, text)
    except Exception as exc:  # noqa: BLE001 - reported only where slopbrake is in charge
        try:
            managed = bool(isinstance(payload, dict) and payload.get("cwd") and managed_toplevel(payload["cwd"]))
        except Exception:  # noqa: BLE001
            managed = False
        if not managed:
            return 0
        print(f"slopbrake hook {event}: internal error: {exc!r}", file=sys.stderr)
        return 2 if event == "pre-tool-use" else 1


# ── user settings ────────────────────────────────────────────────────────────

def default_settings() -> Path:
    return Path.home() / ".claude/settings.json"


def ours(command: str) -> bool:
    return command.startswith(PREFIX)


def runnable() -> tuple[str | None, bool]:
    """The `slopbrake` Claude Code would run, and whether it answers a no-op hook event with exit 0.
    An older build has no `hook` command: its argparse exit 2 would block every Bash call and Stop."""
    binary = shutil.which("slopbrake")
    if not binary:
        return None, False
    # Claude Code runs it without the installer's PYTHONPATH: a dev checkout must not prop up an old build.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        result = subprocess.run([binary, "hook", "pre-tool-use"], input="{}", capture_output=True, text=True,
                                timeout=30, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return binary, False
    return binary, result.returncode == 0


def user_status(path: Path) -> dict:
    data = load_settings(path)
    events = {event: any(ours(c) for entry in hook_entries(data, event) for c in commands(entry))
              for event in USER_ENTRIES}
    binary, ready = runnable()
    return {"settings": str(path), "events": events, "installed": all(events.values()),
            "on_path": binary is not None, "binary": binary, "runnable": ready}


def write_settings(path: Path, data: dict) -> None:
    target = path.resolve()  # keep a symlinked settings file (dotfiles) a symlink
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".slopbrake-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def install(path: Path) -> dict:
    data = load_settings(path)
    binary, ready = runnable()
    if not ready:
        raise SettingsError(f"{binary or 'slopbrake'} cannot run `{PREFIX} pre-tool-use`"
                            f"{'' if binary else ' (not on PATH)'}; install this slopbrake version first, "
                            "or every session's Bash calls and Stops would be blocked")
    present = user_status(path)["events"]
    if not all(present.values()):
        hooks = data["hooks"] = data.get("hooks") or {}
        for event, entry in USER_ENTRIES.items():
            if not present[event]:
                hooks[event] = (hooks.get(event) or []) + [json.loads(json.dumps(entry))]
        write_settings(path, data)
    return user_status(path)


def uninstall(path: Path) -> dict:
    data = load_settings(path)
    hooks = data.get("hooks") or {}
    changed = False
    for event in list(hooks):
        kept = []
        for entry in hooks[event] or []:
            mine = [h for h in entry.get("hooks") or [] if isinstance(h, dict) and ours(str(h.get("command", "")))]
            if mine:
                changed = True
                entry = dict(entry, hooks=[h for h in entry["hooks"] if h not in mine])
                if not entry["hooks"]:
                    continue
            kept.append(entry)
        if kept:
            hooks[event] = kept
        elif hooks[event]:
            del hooks[event]
    if changed:
        if not hooks:
            data.pop("hooks", None)
        write_settings(path, data)
    return user_status(path)
