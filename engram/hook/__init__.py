"""Engram PreToolUse hook.

The enforcement primitive. Reads a Claude Code (or any MCP-compatible
agent) PreToolUse JSON payload from stdin, extracts the destructive
intent, calls blast_radius.assess(), and exits with a code the harness
honors:

  0  = allow the tool call to proceed (tier = green)
  1  = surface a warning but allow (tier = orange/confirm)
  2  = block the tool call (tier = red/block)

Claude Code's PreToolUse hook protocol: any non-zero exit code blocks
the tool. Stderr text is shown to the user.

Override: the user can set ENGRAM_HOOK_ALLOW=red to force red operations
through. This is an explicit, audited override — we still log it to the
memory table for later review.

Failure mode: if Engram can't parse the input or the DB is missing, we
exit 0 (allow). The default is to *not* block work just because Engram
is broken. The safety property is "best-effort enforcement" not "fail
closed" — fail-closed at the harness layer makes Engram a single point
of failure for every dev's Bash tool calls.
"""
