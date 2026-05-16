# Wiring Engram into Claude Code

Two ways to install. Pick whichever you prefer.

## Option A — One-line (recommended)

```bash
pipx install engram-devops
engram init                                  # create ~/.engram/, write default config
engram index --path ~/projects               # index your repos (one-time)
engram mcp install --target claude-code      # adds an MCP server entry to ~/.claude/mcp.json
```

That's it. Restart Claude Code. The `blast_radius`, `infra_context`,
`trace_env_var`, `dependents_of`, `infra_diff`, `service_map`,
`verify_context`, and `remember/recall/forget` tools are now available
to every Claude Code session.

## Option B — Manual

Add this entry to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "engram": {
      "command": "engram",
      "args": ["serve"],
      "env": {}
    }
  }
}
```

If `engram` isn't on your PATH (e.g. you installed in a venv):

```json
{
  "mcpServers": {
    "engram": {
      "command": "/path/to/your/venv/bin/python",
      "args": ["-m", "engram.mcp_server"],
      "env": {}
    }
  }
}
```

## First-call test

Once Claude Code is restarted, ask it:

> Engram says my service "payments" — what does it span?

Claude will call `infra_context("payments")` and respond with a structured
summary: which Dockerfile, which K8s manifests, which Terraform module,
recent changes, environment variables.

## Optional: safety gate via PreToolUse hook

For the strongest safety posture, configure Claude Code to require a
`blast_radius` check before any `Bash` invocation that includes
destructive keywords (`destroy`, `delete`, `uninstall`, `rm -rf`). See
the [Claude Code hooks docs](https://code.claude.com/docs/en/hooks).

A starter hook config that prompts Engram first:

```jsonc
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "engram-hook-precheck",
        "//": "must exit 0 to allow the tool call; non-zero blocks"
      }]
    }]
  }
}
```

The `engram-hook-precheck` script (ships with engram on PyPI) inspects the
proposed command, calls `blast_radius`, and exits non-zero if the action
is `block`. Read [`docs/HOOK_INTEGRATION.md`](../docs/HOOK_INTEGRATION.md)
(coming soon) for the script template.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Claude Code doesn't see the tools after restart | Run `engram diagnose` to confirm the DB exists, then `cat ~/.claude/mcp.json` to confirm the entry is present. |
| `command not found: engram` | Use Option A pipx flow, or use Option B with full path to the venv python. |
| `engram serve` exits immediately when called by Claude | Run `engram serve` manually in a terminal — any error is printed to stderr. Most common: `ENGRAM_DATA_DIR` mismatch. |
