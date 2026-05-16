"""Hook entrypoint — called by Claude Code's PreToolUse mechanism.

Reads a JSON payload from stdin, classifies any Bash command in it, calls
blast_radius.assess(), and exits with the harness-honored code:

  0  = green / proceed
  1  = orange / confirm (Claude Code surfaces this to the user)
  2  = red / block (Claude Code refuses the tool call)

Stderr is the explainer that Claude Code shows to the user. Stdout is
empty by convention.

Failure mode: anything we cannot parse or query → exit 0 (allow). We do
not turn Engram into a single point of failure for every Bash call.
The user can opt into stricter behavior with ENGRAM_HOOK_STRICT=1.

Override: ENGRAM_HOOK_ALLOW=red lets the user force red operations
through. We log the override to the memory table for audit.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from engram.hook.classifier import classify


EXIT_ALLOW = 0
EXIT_CONFIRM = 1
EXIT_BLOCK = 2


def run() -> int:
    """Read stdin, classify, call assess, return the exit code."""
    raw = sys.stdin.read()
    if not raw.strip():
        # No input → nothing to assess → allow.
        return EXIT_ALLOW

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _log_failure(f"hook: invalid JSON on stdin: {raw[:200]!r}")
        return _failure_exit()

    command = _extract_command(payload)
    if not command:
        # Not a Bash tool call, or no command in the payload — allow.
        return EXIT_ALLOW

    intent = classify(command)
    if not intent.is_infra_command:
        return EXIT_ALLOW

    # Now run blast_radius.
    try:
        from engram.config import load_config
        from engram.db import open_db
        from engram.safety.blast_radius import assess
    except Exception as exc:
        _log_failure(f"hook: import error: {exc}")
        return _failure_exit()

    try:
        cfg = load_config()
        if not cfg.db_path.exists():
            # No DB → no graph → can't reason about blast radius. Allow but
            # warn so the user knows Engram isn't actually guarding them.
            print(
                "engram: DB not initialised (run `engram init` to enable "
                "blast-radius checks). Allowing this call.",
                file=sys.stderr,
            )
            return EXIT_ALLOW
        conn = open_db(cfg)
        result = assess(conn, intent.operation, intent.target or "")
    except Exception as exc:
        _log_failure(f"hook: assess failed: {exc}")
        return _failure_exit()

    # Render the explainer to stderr.
    _render(intent, result)

    # Map tier → exit code, honoring the override env var.
    override = os.environ.get("ENGRAM_HOOK_ALLOW", "").lower().strip()
    if result.action == "block":
        if override in ("red", "all"):
            _audit_override(intent, result)
            return EXIT_ALLOW
        return EXIT_BLOCK
    if result.action == "confirm":
        if override in ("orange", "red", "all"):
            return EXIT_ALLOW
        return EXIT_CONFIRM
    return EXIT_ALLOW


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_command(payload: dict[str, Any]) -> str:
    """Extract the bash command string from Claude Code's PreToolUse JSON.

    Claude Code's documented shape:

        {
          "tool_name": "Bash",
          "tool_input": {"command": "...", "description": "..."}
        }

    Older / experimental forms also appear:

        {"tool": "Bash", "tool_input": {"command": "..."}}
        {"name": "Bash", "input": {"command": "..."}}
    """
    if not isinstance(payload, dict):
        return ""

    tool_name = (
        payload.get("tool_name")
        or payload.get("tool")
        or payload.get("name", "")
    )
    if str(tool_name).lower() not in ("bash", "shell", "execute_bash"):
        return ""

    tool_input = (
        payload.get("tool_input")
        or payload.get("input")
        or {}
    )
    if not isinstance(tool_input, dict):
        return ""

    return str(tool_input.get("command", "") or tool_input.get("cmd", ""))


def _render(intent, result) -> None:
    """Write the human-readable assessment to stderr."""
    tag = {"block": "❌  BLOCK", "confirm": "⚠️  CONFIRM",
           "proceed": "✓  PROCEED"}.get(result.action, "?")
    color = {"red": "\033[31m", "orange": "\033[33m",
             "green": "\033[32m"}.get(result.risk_tier, "")
    reset = "\033[0m" if color else ""

    print(file=sys.stderr)
    print(f"{color}{tag}  Engram blast-radius: tier={result.risk_tier}{reset}",
          file=sys.stderr)
    print(f"  operation:   {intent.operation}", file=sys.stderr)
    print(f"  target:      {intent.target or '(none extracted)'}", file=sys.stderr)
    print(f"  environment: {result.environment or '(unknown)'}", file=sys.stderr)
    print(f"  resources:   {len(result.resolved_resources)}", file=sys.stderr)
    print(f"  dependents:  {len(result.dependents)}", file=sys.stderr)
    if result.reasons:
        print(f"  reasons:", file=sys.stderr)
        for r in result.reasons[:6]:
            print(f"    - {r}", file=sys.stderr)
    if result.action == "block":
        print(file=sys.stderr)
        print("  To override this block, set ENGRAM_HOOK_ALLOW=red and re-run.",
              file=sys.stderr)
        print("  This override is logged in the memory table for audit.",
              file=sys.stderr)


def _audit_override(intent, result) -> None:
    """Log the override to the memory table so post-hoc review is possible."""
    try:
        import hashlib
        from datetime import datetime, timezone
        from engram.config import load_config
        from engram.db import open_db

        cfg = load_config()
        if not cfg.db_path.exists():
            return
        conn = open_db(cfg)
        now = datetime.now(timezone.utc).isoformat()
        content = (
            f"OVERRIDE: user set ENGRAM_HOOK_ALLOW for a {result.risk_tier} "
            f"operation: '{intent.operation}' on '{intent.target}'. "
            f"Resolved {len(result.resolved_resources)} resources, "
            f"{len(result.dependents)} dependents, environment={result.environment}."
        )
        mid = hashlib.sha256(
            f"override|{now}|{intent.operation}|{intent.target}".encode()
        ).hexdigest()[:16]
        conn.execute(
            """
            INSERT INTO memory(id, content, memory_type, source, project_path,
                               file_path, service_name, created_at, accessed_at,
                               access_count, importance)
            VALUES (?, ?, 'error', 'hook-override', '', '', '', ?, ?, 0, 1.0)
            ON CONFLICT(id) DO NOTHING
            """,
            (mid, content, now, now),
        )
    except Exception:
        # Audit failure must not break the override path.
        pass


def _failure_exit() -> int:
    """Default-allow on classifier/DB failure unless STRICT is set."""
    if os.environ.get("ENGRAM_HOOK_STRICT", "").strip() in ("1", "true", "yes"):
        return EXIT_BLOCK
    return EXIT_ALLOW


def _log_failure(msg: str) -> None:
    """Best-effort log to stderr so the user sees something went wrong."""
    print(f"engram-hook: {msg}", file=sys.stderr)
