"""User-level hooks for harnesses other than Claude Code: the same git guard (H5) and Stop gate (H6), wired
in each harness's own format. hooks.py holds the shared runner and the Claude Code installer.

  slopbrake user-hooks install|uninstall|status --harness codex     ~/.codex/hooks.json (three entries)
  slopbrake user-hooks install|uninstall|status --harness opencode  ~/.config/opencode/plugins/slopbrake.js
"""
from __future__ import annotations

import copy
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from slopbrake import hooks

HARNESSES = ("claude", "codex", "opencode")
CODEX_COMMAND = f"{hooks.PROGRAM} --harness codex"
CODEX_ENTRIES = {
    "PreToolUse": {"matcher": "^Bash$",
                   "hooks": [{"type": "command", "command": f"{CODEX_COMMAND} pre-tool-use", "timeout": 30}]},
    "PostToolUse": {"matcher": "^(Bash|apply_patch)$",
                    "hooks": [{"type": "command", "command": f"{CODEX_COMMAND} post-tool-use"}]},
    "Stop": {"hooks": [{"type": "command", "command": f"{CODEX_COMMAND} stop", "timeout": 600}]},
}
CODEX_TRUST = ("Codex runs a new or changed hook only after the operator trusts it in Codex's /hooks screen "
               "(it records the trust under [hooks.state] in config.toml, keyed by each entry's position: removing "
               "or replacing slopbrake's entries can renumber later entries in the same event, which then need "
               "trusting again)")
PLUGIN = Path(__file__).resolve().parent / "adapters/opencode.js"
PLUGIN_MARKER = "// installed by slopbrake"


def snake(event: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in event).lstrip("_")


def runnable(harness: str) -> tuple[str | None, bool]:
    """The `slopbrake-hook` the harness would run, and whether it answers `--harness <harness> pre-tool-use`
    with exit 0: an older build on PATH answers `pre-tool-use` but rejects `--harness`, so every hook would fail."""
    binary = shutil.which(hooks.PROGRAM)
    if not binary:
        return None, False
    # The harness runs it without the installer's PYTHONPATH: a dev checkout must not prop up an old build.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        result = subprocess.run([binary, "--harness", harness, "pre-tool-use"], input="{}", capture_output=True,
                                text=True, timeout=30, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return binary, False
    return binary, result.returncode == 0


def not_ready(harness: str) -> str | None:
    """Why the harness could not run slopbrake-hook, or None when it can."""
    binary, ready = runnable(harness)
    return None if ready else (f"{binary or hooks.PROGRAM} cannot run `{hooks.PROGRAM} --harness {harness} "
                               f"pre-tool-use`{'' if binary else ' (not on PATH)'}; install this slopbrake version first")


# ── Codex ────────────────────────────────────────────────────────────────────

def codex_default() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def codex_config(path: Path) -> tuple[dict | None, str | None]:
    """config.toml beside hooks.json, parsed read-only: (data, why it could not be read)."""
    config = path.parent / "config.toml"
    try:
        return tomllib.loads(config.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{config} does not exist"
    except (OSError, ValueError) as exc:
        return None, f"{config} could not be read ({exc})"


def hooks_feature(config: dict | None) -> bool:
    features = (config or {}).get("features")
    return isinstance(features, dict) and features.get("hooks") is True


def codex_position(data: dict, event: str) -> tuple[int, int] | None:
    """(group, handler) of our exact entry (matcher and timeout too) in hooks.json, as Codex numbers them in
    [hooks.state] keys; None when it is missing or differs, so install rewrites it."""
    for group, entry in enumerate(hooks.hook_entries(data, event)):
        if entry == CODEX_ENTRIES[event]:
            return group, 0
    return None


def codex_status(path: Path) -> dict:
    data = hooks.load_settings(path)
    config, problem = codex_config(path)
    feature_on = hooks_feature(config)
    table = (config or {}).get("hooks")
    state = table.get("state") if isinstance(table, dict) else None
    state = state if isinstance(state, dict) else {}
    spellings = {str(path.absolute()), str(path.resolve())}
    positions = {event: codex_position(data, event) for event in CODEX_ENTRIES}
    trusted = {}
    for event, where in positions.items():
        keys = [f"{p}:{snake(event)}:{where[0]}:{where[1]}" for p in spellings] if where else []
        entry = next((state[key] for key in keys if isinstance(state.get(key), dict)), None)
        trusted[event] = entry is not None and entry.get("enabled", True) is not False
    binary, ready = runnable("codex")
    events = {event: where is not None for event, where in positions.items()}
    installed = all(events.values())
    return {"harness": "codex", "settings": str(path), "config": str(path.parent / "config.toml"),
            "events": events, "installed": installed, "features_hooks": feature_on, "config_problem": problem,
            "trusted": trusted, "on_path": binary is not None, "binary": binary, "runnable": ready,
            "note": CODEX_TRUST, "ok": installed and feature_on and ready}


def codex_install(path: Path) -> dict:
    data = hooks.load_settings(path)
    config, problem = codex_config(path)
    if not hooks_feature(config):
        raise hooks.SettingsError(f"Codex runs hooks only with '[features] hooks = true' in {path.parent / 'config.toml'}"
                                  f"{f' ({problem})' if problem else ''}; add it yourself (slopbrake does not edit "
                                  "config.toml), then install again")
    if why := not_ready("codex"):
        raise hooks.SettingsError(why)
    if not codex_status(path)["installed"]:
        hooks.strip_ours(data)
        entries = data["hooks"] = data.get("hooks") or {}
        for event, entry in CODEX_ENTRIES.items():
            entries[event] = (entries.get(event) or []) + [copy.deepcopy(entry)]
        hooks.write_settings(path, data)
    return dict(codex_status(path), ok=True)


def codex_uninstall(path: Path) -> dict:
    data = hooks.load_settings(path)
    if hooks.strip_ours(data):
        hooks.write_settings(path, data)
    return dict(codex_status(path), ok=True)


# ── OpenCode ─────────────────────────────────────────────────────────────────

def opencode_default() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode/plugins/slopbrake.js"


def plugin_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def opencode_status(path: Path) -> dict:
    text = plugin_text(path)
    installed = text is not None and text.startswith(PLUGIN_MARKER)
    current = installed and text == PLUGIN.read_text(encoding="utf-8")
    binary, ready = runnable("opencode")
    return {"harness": "opencode", "settings": str(path), "installed": installed, "current": current,
            "foreign": text is not None and not installed, "on_path": binary is not None, "binary": binary,
            "runnable": ready, "ok": current and ready}


def opencode_install(path: Path) -> dict:
    status = opencode_status(path)
    if status["foreign"]:
        raise hooks.SettingsError(f"{path} is not slopbrake's plugin (no '{PLUGIN_MARKER}' first line); "
                                  "move it aside, then install again")
    if why := not_ready("opencode"):
        raise hooks.SettingsError(why)
    if not status["current"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".slopbrake-tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(PLUGIN.read_text(encoding="utf-8"))
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return dict(opencode_status(path), ok=True)


def opencode_uninstall(path: Path) -> dict:
    if opencode_status(path)["installed"]:
        path.unlink()
    return dict(opencode_status(path), ok=True)


# ── dispatch and output ──────────────────────────────────────────────────────

ACTIONS = {
    "codex": (codex_default, {"install": codex_install, "uninstall": codex_uninstall, "status": codex_status}),
    "opencode": (opencode_default, {"install": opencode_install, "uninstall": opencode_uninstall,
                                    "status": opencode_status}),
}


def user_command(harness: str, action: str, settings: Path | None = None) -> dict:
    """`slopbrake user-hooks install|uninstall|status --harness codex|opencode`."""
    default, actions = ACTIONS[harness]
    return actions[action](settings or default())


def runner_lines(result: dict, harness: str) -> list[str]:
    if result["runnable"]:
        return []
    found = f"{result['binary']} cannot run it" if result["binary"] else "none on PATH"
    command = f"{hooks.PROGRAM} --harness {result['harness']}"
    return [f"note: {harness} cannot run `{command}` ({found}); install refuses until it can"]


def lines(result: dict) -> list[str]:
    """Human output for `slopbrake user-hooks --harness codex|opencode`."""
    if result["harness"] == "opencode":
        state = ("installed" if result["current"] else "installed (older version: run slopbrake user-hooks install "
                 "--harness opencode)") if result["installed"] else "not installed"
        out = [f"slopbrake OpenCode plugin {result['settings']}: {state}"]
        if result["foreign"]:
            out.append(f"note: {result['settings']} is another plugin, not slopbrake's: install refuses to overwrite it")
        return out + runner_lines(result, "OpenCode")
    on = " ".join(event for event, present in result["events"].items() if present) or "none"
    out = [f"slopbrake Codex hooks in {result['settings']}: {on}"]
    if not result["features_hooks"]:
        out.append(f"WARNING: Codex runs no hooks without '[features] hooks = true' in {result['config']}"
                   + (f" ({result['config_problem']})" if result["config_problem"] else ""))
    if result["installed"]:
        waiting = [event for event, ok in result["trusted"].items() if not ok]
        out.append(f"not trusted yet: {' '.join(waiting)}" if waiting else
                   "trusted in [hooks.state] (slopbrake cannot check the hash matches the current entries)")
    out.append(f"note: {result['note']}")
    return out + runner_lines(result, "Codex")
