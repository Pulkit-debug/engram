# Contributing to Engram

Thanks for considering Engram. This project ships fast and stays small. Read
this first so we stay aligned.

## Scope

Engram is the **local-first DevOps context and blast-radius layer for AI
coding agents**. We focus on:

- Indexing infrastructure-as-code across many repos on one machine
- Surfacing structural relationships an AI agent would otherwise re-derive
- Pre-flight blast-radius checks before destructive operations
- Three output channels: MCP tools, codified context (markdown), infra annotations

**Out of scope (don't send PRs):**

- Generic source-code intelligence — use [code-graph-mcp][cgm] or [CodeGraphContext][cgc]
- Generic agent memory — use [Mem0][m0], [Zep][zep], or [Letta][letta]
- Cloud-hosted or SaaS deployment modes
- LLM-in-the-loop extraction (we stay deterministic)
- Document extractors (PDF / DOCX / XLSX / PPTX)

If your idea doesn't fit, file an issue first — we'd rather discuss before you write code.

[cgm]: https://github.com/sdsrss/code-graph-mcp
[cgc]: https://github.com/CodeGraphContext/CodeGraphContext
[m0]:  https://github.com/mem0ai/mem0
[zep]: https://github.com/getzep/zep
[letta]: https://github.com/letta-ai/letta

## Setup

```bash
git clone https://github.com/pulkit-verma/engram
cd engram
python3 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## Code style

- Python 3.11+. Use type hints. Use `from __future__ import annotations`.
- Run `ruff check engram/` and `ruff format engram/` before pushing.
- One-line comments only. No docstring walls. Names carry meaning.
- No bare `except: pass`. Catch specific exceptions; log the cause.
- Tests live in `tests/`. Use pytest. Don't introduce new test frameworks.

## Pull requests

- Open an issue first for anything larger than a one-file fix.
- Branch from `main`. One change per PR.
- Include a test that fails without your change.
- Update [README.md](README.md) or [docs/](docs/) when behaviour changes.
- Sign your commits if you can (`git commit -s`).

## Reporting bugs

Open an issue with:

1. What you ran.
2. What you expected.
3. What happened.
4. Versions (`engram --version`, `python --version`, OS).
5. The relevant chunk of `engram diagnose` output.

## Security

Found a vulnerability? Don't open a public issue. Email pulkit-verma directly
or use GitHub's "Report a vulnerability" feature on the repo.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
