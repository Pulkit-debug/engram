# Wiring Engram into Cursor

## One-line install

```bash
pipx install engram-mcp
engram init
engram index --path ~/projects
engram mcp install --target cursor
```

This writes the engram entry to `~/.cursor/mcp.json` (or
`~/Library/Application Support/Cursor/...` on macOS) without disturbing
other MCP servers in the file. Restart Cursor.

## Verify

Open Cursor, start a new chat in agent mode, ask:

> Use engram to check the blast radius of `terraform destroy` on `prod-db`

Cursor's agent should call `blast_radius` and surface the response.

## Manual JSON

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

## Notes for Cursor users

- Cursor's MCP support is rate-limited to N parallel tool calls; Engram tools
  are fast (<100ms typical) so this rarely matters.
- Cursor caches MCP server discovery — if you change the config, fully
  restart Cursor, not just reload the window.
- For workspaces with very large indexed graphs (>10k resources), pass
  `--limit` flags to keep response payloads under Cursor's 8 KB tool result
  cap. (`infra_context` defaults to `token_budget=2000` which keeps you well
  under.)
