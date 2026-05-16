"""One-line MCP install for the common agents.

Targets: claude-code, cursor, cline, codex, all.

Each target has a known config file path; we update it idempotently. We never
overwrite values for other servers — only the `engram` entry is touched.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TargetSpec:
    name: str
    config_path: Path
    pointer_path: str   # JSON pointer (slash-separated) where mcpServers live


def _home() -> Path:
    return Path.home()


def _resolve_paths() -> dict[str, TargetSpec]:
    """Return the config paths for each supported target on the current OS."""
    home = _home()
    system = platform.system()

    if system == "Darwin":
        cursor_cfg = home / "Library/Application Support/Cursor/User/globalStorage/cursor.mcp/config.json"
        # Cursor also reads ~/.cursor/mcp.json — we prefer the canonical
        # location but fall back to ~/.cursor/mcp.json if it exists.
    else:
        cursor_cfg = home / ".cursor/mcp.json"

    return {
        "claude-code": TargetSpec(
            name="claude-code",
            config_path=home / ".claude/mcp.json",
            pointer_path="mcpServers",
        ),
        "cursor": TargetSpec(
            name="cursor",
            config_path=cursor_cfg if cursor_cfg.exists() else home / ".cursor/mcp.json",
            pointer_path="mcpServers",
        ),
        "cline": TargetSpec(
            name="cline",
            config_path=home / ".config/cline/mcp.json"
                if system != "Darwin"
                else home / "Library/Application Support/cline/mcp.json",
            pointer_path="mcpServers",
        ),
        "codex": TargetSpec(
            name="codex",
            config_path=home / ".codex/mcp.json",
            pointer_path="mcpServers",
        ),
    }


def _engram_entry() -> dict[str, Any]:
    """The MCP-server entry that points at the locally-installed engram CLI."""
    # Prefer the actual engram entry-point script.
    engram_bin = shutil.which("engram-mcp") or shutil.which("engram")
    if engram_bin:
        return {
            "command": engram_bin,
            "args": ["serve"] if Path(engram_bin).name == "engram" else [],
            "env": {},
        }
    # Fall back to invoking the module via current Python interpreter.
    return {
        "command": sys.executable,
        "args": ["-m", "engram.mcp_server"],
        "env": {},
    }


def install(target: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Idempotently install the engram MCP server into a target client.

    Returns: { 'target': ..., 'config_path': ..., 'action': created|updated|unchanged,
               'dry_run': bool, 'entry': <the entry we'd write> }
    """
    targets = _resolve_paths()
    if target not in targets:
        return {"error": f"unknown target '{target}'. Known: {sorted(targets)}"}

    spec = targets[target]
    entry = _engram_entry()

    # Load existing config (or start fresh).
    config: dict[str, Any] = {}
    if spec.config_path.exists():
        try:
            config = json.loads(spec.config_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {"error": f"existing config at {spec.config_path} is not valid JSON"}

    servers = config.setdefault(spec.pointer_path, {})
    if not isinstance(servers, dict):
        return {"error": f"unexpected shape at {spec.pointer_path}: {type(servers).__name__}"}

    existing = servers.get("engram")
    action = "unchanged"
    if existing != entry:
        servers["engram"] = entry
        action = "updated" if existing else "created"

    if not dry_run and action != "unchanged":
        spec.config_path.parent.mkdir(parents=True, exist_ok=True)
        spec.config_path.write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8",
        )

    return {
        "target": target,
        "config_path": str(spec.config_path),
        "action": action,
        "dry_run": dry_run,
        "entry": entry,
    }


def install_all(*, dry_run: bool = False) -> list[dict[str, Any]]:
    return [install(t, dry_run=dry_run) for t in _resolve_paths().keys()]


def uninstall(target: str) -> dict[str, Any]:
    """Remove the engram entry from a target's config (leaves other entries)."""
    targets = _resolve_paths()
    if target not in targets:
        return {"error": f"unknown target '{target}'"}
    spec = targets[target]
    if not spec.config_path.exists():
        return {"target": target, "action": "noop", "config_path": str(spec.config_path)}
    try:
        config = json.loads(spec.config_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {"error": "config is not valid JSON"}
    servers = config.get(spec.pointer_path, {})
    if "engram" not in servers:
        return {"target": target, "action": "noop", "config_path": str(spec.config_path)}
    del servers["engram"]
    spec.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return {"target": target, "action": "removed", "config_path": str(spec.config_path)}
