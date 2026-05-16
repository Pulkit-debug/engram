# Engram

[![CI](https://github.com/pulkit-verma/engram/actions/workflows/ci.yml/badge.svg)](https://github.com/pulkit-verma/engram/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/engram-mcp)](https://pypi.org/project/engram-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Local-first DevOps context and blast-radius layer for AI coding agents.**

> In February 2026, an AI coding agent ran `terraform destroy` on the
> production infrastructure of a learning platform serving 100,000 students.
> It wasn't malice. It wasn't a model bug. It was missing context: the
> agent had no concept of "this is production."
>
> Engram is the layer that would have stopped it.

Engram indexes your DevOps codebases — Kubernetes manifests, Terraform, Helm charts, Dockerfiles, Jenkinsfiles, docker-compose, env files — across every repo on your machine, and gives any MCP-compatible AI agent three things at once:

1. **Cross-repo structural context.** Stop watching the agent re-read 12 files across 5 repos every session to figure out what depends on what.
2. **Blast-radius awareness.** Before any destructive operation, the agent learns what production resources it's about to touch, what depends on them, and what the worst-case impact is.
3. **Trustworthy context.** Engram signs every codified-context artifact it emits, detects documentation drift, and refuses to load unverified context into MCP responses.

100% local. Zero cloud. Zero API keys. Plain SQLite file you can read with `sqlite3`.

---

## See it in 30 seconds

```text
$ engram assess "terraform destroy" "datatalks-prod-db"

STOP  Action: BLOCK  Tier: red
  Operation:    terraform destroy  (destructive)
  Target:       datatalks-prod-db
  Environment:  production
  Resources:    1 resolved
  Dependents:   3
  Incidents:    1
Reasons:
  - Operation 'terraform destroy' is destructive (deletes/destroys state).
  - Target is in PRODUCTION.
  - 3 dependent resource(s) found.
  - 1 prior incident/runbook memory(s) mention this target.
```

Wire this into Claude Code or Cursor as an MCP server with one command. Now every destructive infrastructure operation gets a pre-flight check.

---

## Works for clicked-into-existence infrastructure too

Most production resources weren't created from Terraform — they were clicked into
existence in the AWS console, or `kubectl apply`-d once and never tracked. Engram
covers this without breaking the local-first promise: it doesn't talk to your
cloud, but it parses the output of *your* `aws` and `kubectl` CLIs (using *your*
existing credentials).

```bash
engram import-cloud --provider aws --kinds rds,s3,eks
engram import-cluster --context prod
engram assess "terraform destroy" "users-prod"        # finds it whether or not it's in IaC
```

Discovered resources are tagged `discovered_from = "aws-cli"` / `"kubectl"` so
you can tell them apart from IaC-defined ones. Same `blast_radius` primitive
applies — production tags trigger `red` regardless of how the resource was
created.

See [docs/HOW_IT_UNDERSTANDS_TYPES.md](docs/HOW_IT_UNDERSTANDS_TYPES.md) for
the honest answer to "wait, is this an AI?" (no — it's a type-aware indexer
that trusts the type tags already in your YAML/HCL/JSON).

---

## Why this exists

In February 2026, an AI coding agent ran `terraform destroy` on the production infrastructure of a learning platform serving 100,000 students. It wasn't malice. It wasn't a model bug. It was missing context: the Terraform state file hadn't moved when the engineer switched laptops, and the agent had no concept of "this is production."

In April 2026, an AI agent deleted a production data volume in 9 seconds because it found an API token with excessive permissions and decided to act.

Cursor and Claude Code are great at single-repo app code. They are **bad at understanding how a deployment actually works across many repos**, and they have **no concept of blast radius** when reasoning about infrastructure. Sourcegraph and Augment solve this for enterprises with cloud SaaS — but a solo DevOps engineer or a small platform team can't put their production infra into someone else's cloud.

That's the gap Engram fills.

---

## What's in the box

Three independent output channels for the same DevOps graph. Use one or all three.

### 1. MCP server (the standard channel)

`engram serve` starts an MCP stdio server. Wire it into Claude Code, Cursor, Cline, or any MCP-compatible agent.

| Tool | What it does |
|---|---|
| `infra_context(service, token_budget)` | Returns the structured infrastructure context for a service across all repos, compressed to fit a token budget. |
| `blast_radius(operation, target)` | **Before** any destructive operation, returns the risk tier, dependents, last-incident references, and a proceed/gate/block recommendation. |
| `trace_env_var(name)` | Shows where an env var is defined and consumed across every repo. |
| `dependents_of(target)` | Reverse-search: "what services use Postgres?" |
| `infra_diff(since)` | Infrastructure files changed in the last N days, grouped by service. |
| `service_map(scope)` | Compact dependency graph between services. |
| `verify_context(file_path)` | Anti-poisoning check on Engram-emitted artifacts. |
| `remember / recall / forget` | Persistent memory for DevOps decisions ("we use Helm not Kustomize because X"). |

### 2. Codified Context emission (the markdown channel)

`engram emit-agents-md` writes a human-reviewable summary your agent reads pre-session — works without MCP, works air-gapped, works on locked-down corporate laptops.

`engram check-drift` compares your existing AGENTS.md / CLAUDE.md against the current graph and emits a PR-ready diff. Engram never auto-overwrites human-curated content; it only updates sections marked with `<!-- engram:auto-generated -->`.

### 3. Infrastructure annotation (the non-obvious channel)

`engram label --target k8s --apply` writes structured `engram.io/*` labels onto your Kubernetes resources. `engram label --target terraform --apply` writes `engram.io/*` tags onto Terraform resources.

Once labeled, **any** AI agent doing `kubectl describe pod` or `terraform show` sees Engram's context inline. The agent doesn't need Engram installed. The context lives in the metadata namespace that K8s/Terraform/Docker/Helm have supported since 2014 and that nobody else uses as an agent-context channel.

All labels carry the `engram.io/` prefix and are fully reversible with `engram unlabel`.

---

## Install

```bash
pipx install engram
engram init                                  # creates ~/.engram/, scans for repos
engram index --path ~/projects               # build the graph
engram mcp install --target claude-code      # one-line Claude Code install
engram mcp install --target cursor           # one-line Cursor install
```

Air-gapped? Use the `--offline` flag at install time. Engram bundles tree-sitter grammars and an optional tiny embedding model — no first-run download required.

---

## Architecture

```
Your filesystem
       │
       ▼
   Crawler ── tree-sitter ──┐
                            │
   13 DevOps extractors ────┼──► SQLite + sqlite-vec graph
       │                    │       (File, Resource, Entity,
       │                    │        Project, Service, Memory)
       ▼                    │
   Blast-radius classifier ─┘
                            │
   ┌────────────────────────┼──────────────────────┐
   ▼                        ▼                      ▼
MCP server          Codified-context        Infrastructure
(8 tools)           emitter (AGENTS.md)     annotator (labels)
```

**Storage:** A single `~/.engram/engram.db` SQLite file with [sqlite-vec](https://github.com/asg017/sqlite-vec) for HNSW vector search. Plain-text-readable. Backup with `cp`.

**Extraction:** Tree-sitter for Python, JS, TS, YAML, HCL, Dockerfile. Hand-rolled deterministic parsers for Jenkinsfile, Helm template YAML, Makefile, docker-compose, shell, .env. No LLM in the extraction path.

**Embeddings:** Optional. Lexical (FTS5) covers most DevOps queries well; embeddings are a `pip install engram[embed]` add-on for fuzzy semantic search.

---

## What Engram is not

- **Not a code-search tool.** Use [code-graph-mcp](https://github.com/sdsrss/code-graph-mcp) or [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) for source-code analysis. Engram covers the *infrastructure* surface those tools intentionally skip.
- **Not a general-purpose memory layer.** Use [Mem0](https://github.com/mem0ai/mem0), [Zep](https://github.com/getzep/zep), or [Letta](https://github.com/letta-ai/letta) for conversational memory. Engram is DevOps-structural.
- **Not an action server.** Use [Terraform MCP](https://github.com/hashicorp/terraform-mcp-server), [Kubernetes MCP](https://github.com/containers/kubernetes-mcp-server), or [Azure DevOps MCP](https://github.com/microsoft/azure-devops-mcp) to *do* things. Engram tells those servers what they're about to touch.

Engram is the **context and safety gate** that sits between the agent and the action servers.

---

## Status

**v0.2.0 — production-hardened alpha.**

- ✅ 64 unit tests + 17 adversarial scenarios + 1 live MCP wire test, all green on Linux + Windows
- ✅ Real-world validated against [cert-manager](https://github.com/cert-manager/cert-manager) (1,217 files, 6.4 s) and [vault-helm](https://github.com/hashicorp/vault-helm) — see [docs/REAL_WORLD_VALIDATION.md](docs/REAL_WORLD_VALIDATION.md)
- ✅ Terraform tag allowlist prevents `terraform plan` breakage on untaggable resource types
- ✅ HMAC-signed AGENTS.md emissions with drift detection
- ✅ Three install channels (MCP / AGENTS.md / metadata labels) so it works in MDM-locked and air-gapped environments

See [docs/ROADMAP.md](docs/ROADMAP.md) for the build journey and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design walkthrough.

## Reproduce everything in 60 seconds

```bash
git clone https://github.com/pulkit-verma/engram
cd engram
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
bash tests/full_demo.sh
```

The demo writes everything to `/tmp/engram_full_demo/` and prints `RESULT: all 9 checks passed` at the end. All output is verbatim from real runs — no scripted demos.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design + mermaid diagram + why three channels
- [docs/REAL_WORLD_VALIDATION.md](docs/REAL_WORLD_VALIDATION.md) — numbers from cert-manager + vault-helm
- [docs/CROSS_PLATFORM_AUDIT.md](docs/CROSS_PLATFORM_AUDIT.md) — Linux + Windows verified, macOS audited
- [docs/ROADMAP.md](docs/ROADMAP.md) — build status + next milestones
- [examples/claude_code_mcp_setup.md](examples/claude_code_mcp_setup.md) — wiring into Claude Code
- [examples/cursor_mcp_setup.md](examples/cursor_mcp_setup.md) — wiring into Cursor
- [examples/before_after_token_savings.md](examples/before_after_token_savings.md) — measured token comparison
- [docs/HOW_IT_UNDERSTANDS_TYPES.md](docs/HOW_IT_UNDERSTANDS_TYPES.md) — what extraction actually does (no AI)
- [docs/CLICK_OPS.md](docs/CLICK_OPS.md) — bringing clicked-into-existence resources into the graph

## License

MIT
