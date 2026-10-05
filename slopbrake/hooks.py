"""User-level Claude Code hooks: the git guard (H5) and the Stop gate (H6) for every
slopbrake-managed repo a session changes, wherever the session started. Claude Code loads
project hooks only from the session's starting directory, so these run from
~/.claude/settings.json and act only inside repos whose toplevel has .claude/slopbrake.json.
The Stop gate runs a repo's own scripts only when the operator trusts the repo
(${XDG_CONFIG_HOME:-~/.config}/slopbrake/trusted.json; `slopbrake init` adds its repo).

  slopbrake-hook pre-tool-use|post-tool-use|stop      hook JSON on stdin (alias: slopbrake hook)
  slopbrake user-hooks install|uninstall|status [--settings PATH]
  slopbrake user-hooks trust|untrust REPO
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KIT_GUARD = Path(__file__).resolve().parent / "kit/common/.claude/hooks/git_guard.py"
# A separate console script: an install that predates it exits 127 (not a block) instead of argparse's 2.
PROGRAM = "slopbrake-hook"
LEGACY = "slopbrake hook"  # 0.2.0 entries; install replaces them
EVENTS = ("pre-tool-use", "post-tool-use", "stop")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
USER_ENTRIES = {
    "PreToolUse": {"matcher": "Bash", "hooks": [{"type": "command", "command": f"{PROGRAM} pre-tool-use"}]},
    "PostToolUse": {"matcher": "|".join((*EDIT_TOOLS, "Bash")),
                    "hooks": [{"type": "command", "command": f"{PROGRAM} post-tool-use"}]},
    # A failed command can still have changed the tree, and its snapshot must not linger.
    "PostToolUseFailure": {"matcher": "Bash", "hooks": [{"type": "command", "command": f"{PROGRAM} post-tool-use"}]},
    "Stop": {"hooks": [{"type": "command", "command": f"{PROGRAM} stop", "timeout": 600}]},
}
OPERATORS = set("();<>|&")
STOP_BUDGET = 540  # seconds for every touched repo together, inside the 600 s hook timeout
STOP_GRACE = 10  # on top of a gate's share of the budget before it is killed
PENDING_MAX = 64  # snapshots of Bash calls still running (or whose PostToolUse never came)


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


def managed_root(path) -> Path | None:
    """managed_toplevel by walking the filesystem (no git process): cheap enough for every path in a command."""
    path = Path(path).expanduser().resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate if (candidate / ".claude/slopbrake.json").is_file() else None
    return None


def update_json(path: Path, change):
    """Read-modify-write a JSON object file under an exclusive lock (parallel hooks write the same file).
    change(data) mutates the dict and returns what update_json returns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        try:
            data = json.loads(handle.read() or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        before = json.dumps(data, sort_keys=True)
        result = change(data)
        if json.dumps(data, sort_keys=True) != before:
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle)
        return result


def string_list(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


# ── trust ────────────────────────────────────────────────────────────────────

def trust_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "slopbrake/trusted.json"


def trusted() -> list[str]:
    try:
        data = json.loads(trust_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return string_list(data.get("repos")) if isinstance(data, dict) else []


def is_trusted(repo: Path) -> bool:
    """Whether the Stop gate may run repo's own scripts: the operator listed its toplevel."""
    return str(Path(repo).expanduser().resolve()) in trusted()


def trust(repo: Path) -> Path:
    """Trust the git repo holding repo; returns its toplevel."""
    path = Path(repo).expanduser()
    top = toplevel(path) if path.exists() else None
    if top is None:
        raise SettingsError(f"{repo} is not inside a git repository")
    update_json(trust_path(), lambda data: data.update(repos=sorted({*string_list(data.get("repos")), str(top)})))
    return top


def untrust(repo: Path) -> bool:
    """Forget repo (or the toplevel holding it); False when it was not trusted."""
    path = Path(repo).expanduser().resolve()
    names = {str(path)}
    if path.is_dir():
        names.add(str(toplevel(path) or path))

    def change(data):
        known = string_list(data.get("repos"))
        data["repos"] = [r for r in known if r not in names]
        return len(data["repos"]) != len(known)
    return update_json(trust_path(), change)


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

def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "slopbrake/sessions"


def state_path(session_id) -> Path:
    return state_dir() / (re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "unknown")) + ".json")


def touched(session_id) -> list[str]:
    try:
        return string_list(json.loads(state_path(session_id).read_text(encoding="utf-8")).get("repos"))
    except (OSError, ValueError, AttributeError):
        return []


def record(session_id, repos: set[str]) -> None:
    update_json(state_path(session_id), lambda data: data.update(repos=sorted({*string_list(data.get("repos")), *repos})))


def expand(base: Path, word: str) -> Path | None:
    path = os.path.expandvars(os.path.expanduser(word))
    return None if "$" in path or "`" in path else Path(os.path.normpath(base / path))


def bash_dirs(command: str, cwd: Path) -> list[Path]:
    """Directories a shell command may change: its cwd, `cd`/`pushd` targets (followed through a chain of
    cds), `git -C` targets, and absolute or ~ paths among its words (also after an `=`)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        words = command.split()
    dirs, here, in_git = [cwd], cwd, False
    for i, word in enumerate(words):
        if not word or set(word) <= OPERATORS:
            in_git = False
            continue
        following = words[i + 1] if i + 1 < len(words) and not set(words[i + 1]) <= OPERATORS else None
        in_git = in_git or Path(word).name == "git"
        target = expand(here, following) if following and (word in ("cd", "pushd") or (word == "-C" and in_git)) \
            else None
        if target:
            dirs.append(target)
            here = here if word == "-C" else target
        for part in {word, word.partition("=")[2]}:
            if part.startswith(("/", "~")) and (path := expand(here, part)):
                dirs.append(path)
    return list(dict.fromkeys(dirs))


def snapshot(repo: Path) -> str | None:
    """A digest of HEAD and every changed path with its size and mtime; None when git fails.
    Equal digests before and after a tool call mean the call left the repo alone."""
    try:
        head = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=repo, capture_output=True,
                              timeout=30, check=False).stdout
        status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repo,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status.returncode:
        return None
    digest = hashlib.sha256(head)
    for item in status.stdout.split(b"\0"):
        try:
            info = os.lstat(repo / os.fsdecode(item[3:]))
            stamp = f"{info.st_size}:{info.st_mtime_ns}"
        except (OSError, ValueError):
            stamp = "-"
        digest.update(item + b"\0" + stamp.encode() + b"\0")
    return digest.hexdigest()


def snapshots(command: str, cwd: Path) -> dict[str, str | None]:
    roots = {managed_root(d) for d in bash_dirs(command, cwd)} - {None}
    return {str(root): snapshot(root) for root in sorted(roots)}


def pending_key(payload: dict, command: str) -> str:
    key = payload.get("tool_use_id")
    return str(key) if key else hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()  # cwd may move


def remember(payload: dict, command: str) -> None:
    """PreToolUse: snapshot the managed repos a Bash command may change, for post_tool_use to compare."""
    taken = snapshots(command, Path(payload.get("cwd") or os.getcwd()))
    if not taken:
        return

    def change(data):
        pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
        pending[pending_key(payload, command)] = taken
        data["pending"] = dict(list(pending.items())[-PENDING_MAX:])
    update_json(state_path(payload.get("session_id")), change)


def recall(payload: dict, command: str) -> dict:
    def change(data):
        pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
        taken = pending.pop(pending_key(payload, command), None)
        if "pending" in data:
            data["pending"] = pending
        return taken if isinstance(taken, dict) else {}
    return update_json(state_path(payload.get("session_id")), change)


def post_tool_use(payload: dict) -> int:
    """Record the managed repos this tool call changed: edit targets directly, and for Bash every repo
    whose snapshot differs from PreToolUse's (or that had none, such as a fresh clone)."""
    cwd = Path(payload.get("cwd") or os.getcwd())
    args = payload.get("tool_input") or {}
    repos = set()
    if payload.get("tool_name") in EDIT_TOOLS:
        target = args.get("file_path") or args.get("notebook_path")
        root = managed_root(cwd / Path(target).expanduser()) if target else None
        repos = {str(root)} if root else set()
    elif payload.get("tool_name") == "Bash":
        command = str(args.get("command", ""))
        before = recall(payload, command) if state_path(payload.get("session_id")).is_file() else {}
        missing = object()
        repos = {repo for repo, digest in snapshots(command, cwd).items()
                 if digest is None or before.get(repo, missing) != digest}
    if repos:
        record(payload.get("session_id"), repos)
    return 0


def run_gate(hook: Path, repo: Path, text: str, left: float) -> tuple[int, str]:
    """Run one repo's require-green.sh in its own process group; kill the group at left + STOP_GRACE."""
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(repo), SLOPBRAKE_STOP_TIMEOUT=str(max(1, int(left))))
    proc = subprocess.Popen([str(hook)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=repo, env=env, text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        _, err = proc.communicate(text, timeout=left + STOP_GRACE)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)  # the gate's children too: they hold its pipes open
        proc.communicate()
        raise TimeoutError(f"the gate timed out after {int(left + STOP_GRACE)}s and was killed") from None
    return proc.returncode, err


def stop(payload: dict, text: str) -> int:
    """Block (exit 2) when a trusted, changed repo's gate is red. A gate that cannot run, times out or
    misses the budget is reported without blocking (exit 1), so it never traps the session."""
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    project_root = Path(project).expanduser().resolve() if project else None
    failures, problems, deadline = [], [], time.monotonic() + STOP_BUDGET  # Claude Code kills the hook at 600 s
    for repo in map(Path, touched(payload.get("session_id"))):
        hook = repo / ".claude/hooks/require-green.sh"
        if not (repo / ".claude/slopbrake.json").is_file() or not hook.is_file() or not is_trusted(repo):
            continue
        if repo == project_root and not settings_gaps(repo):
            continue  # the project's own Stop hook already gates it ("$CLAUDE_PROJECT_DIR"/.claude/hooks resolves)
        left = deadline - time.monotonic()
        if left <= 0:
            problems.append(f"{repo}:\nrequire-green: the {STOP_BUDGET}s Stop budget was used up before this repo's "
                            "gate ran")
            continue
        try:
            code, err = run_gate(hook, repo, text, left)
        except TimeoutError as exc:
            problems.append(f"{repo}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one repo's broken gate must not hide the others
            problems.append(f"{repo}: the gate could not run ({exc})")
            continue
        if code == 2:
            failures.append(f"{repo}:\n{err.strip()}")
        elif code:
            problems.append(f"{repo}: the gate could not run (exit {code}): {err.strip()}")
    if failures or problems:
        print("\n\n".join(failures + problems), file=sys.stderr)
    return 2 if failures else 1 if problems else 0


def pre_tool_use(payload: dict, guard: Path = KIT_GUARD) -> int:
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    spec = importlib.util.spec_from_file_location("slopbrake_git_guard", guard)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reason = module.check_command(command, str(payload.get("cwd") or os.getcwd()), "managed")
    if reason:
        print(f"BLOCKED by the destructive-git guard (H5): {reason}. The operator has not granted this in agent "
              "sessions; ask them to run it.", file=sys.stderr)
        return 2
    with contextlib.suppress(Exception):  # bookkeeping never blocks a command
        remember(payload, command)
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
        print(f"{PROGRAM} {event}: internal error: {exc!r}", file=sys.stderr)
        return 2 if event == "pre-tool-use" else 1


def main(argv: list[str] | None = None) -> int:
    """The `slopbrake-hook <event>` console script (and `slopbrake hook`). Never exits 2 on bad usage."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in EVENTS:
        print(f"usage: {PROGRAM} {'|'.join(EVENTS)}  (Claude Code hook JSON on stdin)", file=sys.stderr)
        return 1
    return run(argv[0], sys.stdin.buffer.read().decode("utf-8", errors="replace"))


# ── user settings ────────────────────────────────────────────────────────────

def default_settings() -> Path:
    return Path.home() / ".claude/settings.json"


def ours(command: str) -> bool:
    return command.startswith((f"{PROGRAM} ", f"{LEGACY} "))


def runnable() -> tuple[str | None, bool]:
    """The `slopbrake-hook` Claude Code would run, and whether it answers a no-op hook event with exit 0."""
    binary = shutil.which(PROGRAM)
    if not binary:
        return None, False
    # Claude Code runs it without the installer's PYTHONPATH: a dev checkout must not prop up an old build.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        result = subprocess.run([binary, "pre-tool-use"], input="{}", capture_output=True, text=True,
                                timeout=30, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return binary, False
    return binary, result.returncode == 0


def untrusted() -> list[str]:
    """Managed repos that sessions changed but the operator has not trusted: the Stop gate skips them."""
    seen = set()
    for path in state_dir().glob("*.json"):
        try:
            seen.update(string_list(json.loads(path.read_text(encoding="utf-8")).get("repos")))
        except (OSError, ValueError, AttributeError):
            continue
    known = set(trusted())
    return sorted(r for r in seen if r not in known and (Path(r) / ".claude/slopbrake.json").is_file())


def user_status(path: Path) -> dict:
    data = load_settings(path)
    events = {event: any(c == entry["hooks"][0]["command"] for e in hook_entries(data, event) for c in commands(e))
              for event, entry in USER_ENTRIES.items()}
    legacy = any(c.startswith(f"{LEGACY} ") for event in (data.get("hooks") or {})
                 for e in hook_entries(data, event) for c in commands(e))
    binary, ready = runnable()
    installed, disabled = all(events.values()), bool(data.get("disableAllHooks"))
    # Not installed is informational (the hooks are opt-in); installed but unable to fire is not.
    ok = not (any(events.values()) or legacy) or (installed and not legacy and ready and not disabled)
    return {"settings": str(path), "events": events, "installed": installed, "legacy": legacy,
            "on_path": binary is not None, "binary": binary, "runnable": ready, "disabled": disabled,
            "trusted": trusted(), "untrusted": untrusted(), "ok": ok}


def user_lines(result: dict) -> list[str]:
    """Human output for `slopbrake user-hooks`."""
    if "events" not in result:
        word = "trusted" if result["trusted"] else "untrusted" if result.get("removed", True) else "was not trusted:"
        return [f"{word} {result['repo']}"]
    any_ours = any(result["events"].values()) or result["legacy"]
    on = " ".join(event for event, present in result["events"].items() if present) or "none"
    lines = [f"slopbrake user hooks in {result['settings']}: {on}"]
    if result["legacy"]:
        lines.append(f"note: older `{LEGACY}` entries are installed; slopbrake user-hooks install switches them "
                     f"to `{PROGRAM}`")
    if not any_ours:
        if not result["runnable"]:
            found = f"{result['binary']} cannot run it" if result["binary"] else "none on PATH"
            lines.append(f"note: install refuses until a current `{PROGRAM}` is on PATH ({found})")
    elif not result["on_path"]:
        lines.append(f"note: `{PROGRAM}` is not on PATH, so Claude Code cannot run these hooks")
    elif not result["runnable"]:
        lines.append(f"WARNING: {result['binary']} cannot run `{PROGRAM} pre-tool-use`: installed hooks block every "
                     "session; install this slopbrake version or run slopbrake user-hooks uninstall")
    if result["disabled"]:
        lines.append(f"WARNING: {result['settings']} sets disableAllHooks, so Claude Code runs none of these hooks")
    if result["untrusted"]:
        lines.append("untrusted managed repos (the Stop gate skips them; slopbrake user-hooks trust REPO):")
        lines += [f"      - {repo}" for repo in result["untrusted"]]
    return lines


def write_settings(path: Path, data: dict) -> None:
    """Replace the file atomically, keeping its mode (settings can hold secrets) and a symlink a symlink."""
    target = path.resolve()  # keep a symlinked settings file (dotfiles) a symlink
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".slopbrake-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def strip_ours(data: dict) -> bool:
    """Remove every slopbrake hook (current and legacy) from data; True when something was removed."""
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
    if changed and not hooks:
        data.pop("hooks", None)
    return changed


def install(path: Path) -> dict:
    binary, ready = runnable()
    data = load_settings(path)
    if not ready:
        raise SettingsError(f"{binary or PROGRAM} cannot run `{PROGRAM} pre-tool-use`"
                            f"{'' if binary else ' (not on PATH)'}; install this slopbrake version first, "
                            "or every session's Bash calls and Stops would be blocked")
    present = user_status(path)
    if not present["installed"] or present["legacy"]:
        strip_ours(data)
        hooks = data["hooks"] = data.get("hooks") or {}
        for event, entry in USER_ENTRIES.items():
            hooks[event] = (hooks.get(event) or []) + [copy.deepcopy(entry)]
        write_settings(path, data)
    return dict(user_status(path), ok=True)


def uninstall(path: Path) -> dict:
    data = load_settings(path)
    if strip_ours(data):
        write_settings(path, data)
    return dict(user_status(path), ok=True)


def user_command(action: str, settings: Path | None = None, repo: Path | None = None) -> dict:
    """`slopbrake user-hooks <action>`; the result's "ok" is the command's success."""
    if action in ("trust", "untrust"):
        if repo is None:
            raise SettingsError(f"user-hooks {action} needs a repo path")
        if action == "trust":
            return {"repo": str(trust(repo)), "trusted": True, "ok": True}
        return {"repo": str(Path(repo).expanduser().resolve()), "trusted": False, "removed": untrust(repo), "ok": True}
    return {"install": install, "uninstall": uninstall, "status": user_status}[action](settings or default_settings())


if __name__ == "__main__":
    sys.exit(main())
