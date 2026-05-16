# Cross-platform audit

## Test matrix

| Platform | Python | Status | Notes |
|---|---|---|---|
| **Linux (WSL Ubuntu 24.04)** | 3.12.3 | ✅ 64/64 + MCP wire test | Primary dev platform |
| **Windows 11 (native)** | 3.12.1 | ✅ 62 passed, 3 skipped, MCP wire test green | 3 skipped tests are explicit `skipif(win32)` for chmod/symlink-loop semantics |
| **macOS** | 3.12 (unverified) | ⚠️ Audited statically, see below | No hardware available for live run |

## Methodology

I ran the full pytest suite on both Linux (WSL) and Windows (native cpython
3.12.1) from clean venvs. I also ran the live MCP wire test (`test_mcp_wire.py`)
on both — which launches a real `engram serve` subprocess, opens an MCP stdio
client, calls four tools, and asserts the responses. Both platforms green.

For macOS I cannot run the code, so I audited every place the code touches
the filesystem, processes, OS-specific paths, or platform-conditional logic.
Findings below.

## Platform-conditional code in the source

A repository-wide grep for `platform.system`, `sys.platform`, hardcoded
`/tmp`, `/home`, `posix`, `darwin` yielded exactly **one** match in `engram/`:

```
engram/install.py:35:    system = platform.system()
```

That branch handles the Cursor MCP config path:
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/cursor.mcp/config.json`
- Linux/Windows: `~/.cursor/mcp.json`

Both fall back to `~/.cursor/mcp.json` if the first path doesn't exist.

## Filesystem path handling

Every place the code constructs a path uses `pathlib.Path`. There is **no**
hardcoded `/` separator in path construction anywhere in `engram/`.

The 17 grep hits for `"/"` literals are all inside *string content parsing*
(not paths):
- Docker image refs (`split(":")[0].split("/")[-1]` for image name)
- K8s `apiVersion` strings (`"apps/v1"`)
- escape sequences in HCL/YAML state machines
- env-path normalization (`path.replace("\\", "/")` before scanning segments
  — this is correct on Windows: it normalizes both back/forward slashes
  before checking for `prod`/`staging` segments)

Verdict: no cross-platform path bugs likely.

## Process spawning

`engram.mcp_server.run_server()` uses `mcp.run(transport="stdio")` which is
pure-Python stdin/stdout — works on every platform the `mcp` package supports
(officially: Linux, macOS, Windows).

There is no `os.fork()`, no `subprocess.Popen` with `shell=True` in package
code, and no platform-specific signal handling.

## SQLite

- SQLite is bundled with Python on all three platforms.
- `sqlite-vec` ships wheels for `linux-x86_64`, `macosx-*`, `win-amd64` — all
  three platforms supported. Pip will pick the right wheel automatically.
- WAL mode is enabled in `schema.sql` and works identically across platforms.

## Tree-sitter

- `tree-sitter-language-pack` ships pre-built grammars as wheels for Linux,
  macOS, and Windows. No source build required.
- The JS/TS extractor falls back to a regex parser if tree-sitter can't load
  for any reason, so it degrades gracefully on a platform where the wheel is
  missing.

## Specific macOS unknowns

These are the things I'd want to verify on a real Mac before claiming
"works on Mac":

1. **`engram mcp install --target cursor`** — the Cursor config path is the
   guess that might be wrong. Easy to fix once a Mac user reports back; the
   `install.py` module is 130 lines.
2. **fastembed (optional dep)** — pulls ONNX Runtime; ships macOS-arm64 +
   macOS-x86_64 wheels but the first-run download on Apple Silicon can hit
   ~400 MB. Not a correctness issue, just a UX heads-up. Default disabled.
3. **`os.chmod(..., 0o555)` in adversarial tests** — already gated by
   `skipif(win32)`; on macOS this works the same as Linux.

## How to extend this audit

If a user reports a macOS bug, the first diagnostic is:

```bash
engram diagnose
```

This prints the resolved config dir, DB path, vector-support state, and watch
paths. Most macOS-specific bugs will be visible from that output alone (wrong
data dir, missing perms, fastembed model download stuck).

## Recommendation for first public release

- **Promise:** Linux + Windows fully supported (verified on the test bench).
- **Beta:** macOS — code is portable, every path uses `pathlib`, no
  POSIX-only system calls; the one path-inference guess (Cursor) is easy
  to correct. We will accept macOS bug reports as P0 and patch within 24h.
