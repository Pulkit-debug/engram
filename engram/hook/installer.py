"""Install the Engram PreToolUse hook into an agent harness.

Currently supports: Claude Code (~/.claude/settings.json).

Claude Code's hook config shape (Anthropic docs):

  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Bash",
          "hooks": [
            {"type": "command", "command": "/path/to/engram hook"}
          ]
        }
      ]
    }
  }

We mutate ONLY:
  1. settings.hooks.PreToolUse — append our matcher entry if it isn't
     already present;
  2. inside it, the engram command line — overwrite if it exists, leave
     other hooks alone.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


def _claude_settings_path() -> Path:
    """Where Claude Code reads PreToolUse hook config from."""
    return Path.home() / ".claude" / "settings.json"


def _engram_command() -> str:
    """The shell command Claude Code will invoke."""
    engram_bin = shutil.which("engram")
    if engram_bin:
        return f"{engram_bin} hook"
    # Fallback: invoke via current Python interpreter.
    return f"{sys.executable} -m engram.cli hook"


def install_hook(target: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Idempotently install the hook. Returns a dict with action + path."""
    if target != "claude-code":
        return {"error": f"target '{target}' not supported (only 'claude-code' for now)"}

    cfg_path = _claude_settings_path()
    config: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            config = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {"error": f"existing config at {cfg_path} is not valid JSON"}

    if not isinstance(config, dict):
        return {"error": f"expected object at root of {cfg_path}, got {type(config).__name__}"}

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return {"error": f"unexpected shape at 'hooks' in {cfg_path}"}

    pre_tool_use = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre_tool_use, list):
        return {"error": f"unexpected shape at 'hooks.PreToolUse' in {cfg_path}"}

    engram_cmd = _engram_command()
    desired_hook = {"type": "command", "command": engram_cmd}

    # Find an existing Bash matcher (whether or not it has our hook).
    bash_matcher_idx = -1
    for i, matcher in enumerate(pre_tool_use):
        if isinstance(matcher, dict) and matcher.get("matcher") == "Bash":
            bash_matcher_idx = i
            break

    action = "unchanged"

    if bash_matcher_idx == -1:
        # No Bash matcher yet — add one.
        pre_tool_use.append({"matcher": "Bash", "hooks": [desired_hook]})
        action = "created"
    else:
        matcher = pre_tool_use[bash_matcher_idx]
        hook_list = matcher.setdefault("hooks", [])
        if not isinstance(hook_list, list):
            return {"error": "hooks.PreToolUse[*].hooks must be a list"}

        # Look for an existing engram hook entry.
        engram_idx = -1
        for j, h in enumerate(hook_list):
            if (isinstance(h, dict)
                    and h.get("type") == "command"
                    and isinstance(h.get("command"), str)
                    and "engram" in h["command"] and "hook" in h["command"]):
                engram_idx = j
                break

        if engram_idx == -1:
            hook_list.append(desired_hook)
            action = "created"
        elif hook_list[engram_idx] != desired_hook:
            hook_list[engram_idx] = desired_hook
            action = "updated"

    if not dry_run and action != "unchanged":
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return {
        "target": target,
        "config_path": str(cfg_path),
        "action": action,
        "dry_run": dry_run,
        "command": engram_cmd,
        "matcher_summary": f"hooks.PreToolUse[matcher=Bash] → '{engram_cmd}'",
    }


def uninstall_hook(target: str) -> dict[str, Any]:
    """Remove the engram hook from a target. Idempotent."""
    if target != "claude-code":
        return {"error": f"target '{target}' not supported"}

    cfg_path = _claude_settings_path()
    if not cfg_path.exists():
        return {"target": target, "action": "noop", "config_path": str(cfg_path)}

    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {"error": "config is not valid JSON"}

    pre = (config.get("hooks") or {}).get("PreToolUse", [])
    if not isinstance(pre, list):
        return {"target": target, "action": "noop", "config_path": str(cfg_path)}

    changed = False
    for matcher in pre:
        if not isinstance(matcher, dict):
            continue
        if matcher.get("matcher") != "Bash":
            continue
        hook_list = matcher.get("hooks") or []
        if not isinstance(hook_list, list):
            continue
        before = len(hook_list)
        matcher["hooks"] = [
            h for h in hook_list
            if not (isinstance(h, dict)
                    and h.get("type") == "command"
                    and isinstance(h.get("command"), str)
                    and "engram" in h["command"] and "hook" in h["command"])
        ]
        if len(matcher["hooks"]) != before:
            changed = True

    if changed:
        cfg_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return {"target": target, "action": "removed", "config_path": str(cfg_path)}
    return {"target": target, "action": "noop", "config_path": str(cfg_path)}
