# Engram v0.2 — Build status

## Days 1-25: SHIPPED ✓

End-to-end working in one session. Every phase's exit-criteria was verified
on a real fixture before moving to the next.

### Day 1 — Foundation (done)

- [x] Repositioning README (3-pillar value prop, named DataTalks anchor)
- [x] `pyproject.toml` with sqlite-vec, tree-sitter, mcp, click, rich
- [x] `schema.sql` — 12 tables, explicit indexes, `risk_tier` first-class
- [x] `db.py` — SQLite + sqlite-vec, WAL mode, FK cascade
- [x] `graph.py` — typed dataclass ops layer
- [x] `extractors/base.py` — `ExtractedResource` / `ExtractedEntity` / `ExtractedEdge`
- [x] `extractors/dockerfile.py` — first extractor
- [x] **`safety/blast_radius.py`** — DataTalks-Club-prevention primitive
- [x] `mcp_server.py` — 10 MCP tools
- [x] `cli.py` — `init`, `status`, `serve`, `assess`, `diagnose`

### Days 2-5 — Extractors (done)

- [x] YAMLExtractor (with K8s detection)
- [x] TerraformExtractor (pure-Python HCL parser)
- [x] DockerComposeExtractor
- [x] HelmChartExtractor + HelmValuesExtractor
- [x] JenkinsfileExtractor
- [x] MakefileExtractor
- [x] ShellExtractor
- [x] EnvFileExtractor
- [x] PythonExtractor (stdlib `ast`)
- [x] JsTsExtractor (tree-sitter + regex fallback)
- [x] Registry wires all 21 file types to extractors

### Days 6-10 — Crawler (done)

- [x] `crawler.py` walks paths, runs extractors, populates graph
- [x] Content-hash skip on re-run
- [x] Project detection (15 markers including Chart.yaml, main.tf, kustomization.yaml)
- [x] Environment classifier writes `risk_tier` on every Resource
- [x] Edge post-pass: env_var ↔ env_ref linking across files
- [x] `engram index` CLI command

### Days 11-15 — Codified Context output (done)

- [x] `output/codified.py` — AGENTS.md / CLAUDE.md emitter
- [x] HMAC signing of every emitted section
- [x] `engram emit-agents-md` CLI
- [x] `engram check-drift` CLI
- [x] Tamper detection — refuses overwrite without `--force`
- [x] Preserves human-curated content outside engram markers
- [x] `verify_context` MCP tool wires to real HMAC check

### Days 16-22 — Infrastructure annotation (done)

- [x] `output/annotate.py` — K8s labels + Terraform tags
- [x] Dry-run default; `--apply` writes
- [x] Idempotent (re-apply doesn't duplicate keys)
- [x] `engram label` / `engram unlabel` CLI
- [x] `annotation` table tracks every applied label for safe rollback

### Days 23-25 — Polish & packaging (done)

- [x] LICENSE (MIT)
- [x] CONTRIBUTING.md — explicit scope & out-of-scope
- [x] CODE_OF_CONDUCT.md — Contributor Covenant v2.1
- [x] GitHub Actions CI matrix (Ubuntu/macOS/Windows × Python 3.11/3.12)
- [x] CI runs ruff + pytest + end-to-end smoke
- [x] 34/34 pytest passing covering extractors, crawler, blast-radius, codified, annotate
- [x] `engram-mcp` PyPI entry point declared
- [x] `engram mcp install --target {claude-code,cursor,cline,codex,all}` helper

## Final state inventory

```
v2/
├── engram/                           # ~3,200 LOC (target was <3k; close enough)
│   ├── __init__.py
│   ├── cli.py                        # 9 commands
│   ├── config.py
│   ├── crawler.py
│   ├── db.py
│   ├── graph.py
│   ├── install.py
│   ├── mcp_server.py                 # 10 MCP tools
│   ├── schema.sql                    # 12 tables, all indexed
│   ├── extractors/                   # 11 concrete extractors covering 21 file types
│   │   ├── base.py
│   │   ├── compose_ext.py
│   │   ├── devops_ext.py             # Jenkinsfile, Makefile, shell, .env
│   │   ├── dockerfile.py
│   │   ├── helm_ext.py
│   │   ├── js_ts_ext.py
│   │   ├── python_ext.py
│   │   ├── terraform_ext.py
│   │   └── yaml_ext.py
│   ├── output/                       # Mode B + Mode C
│   │   ├── annotate.py
│   │   └── codified.py
│   └── safety/                       # The killer primitive
│       └── blast_radius.py
├── tests/                            # 34 passing tests
│   ├── build_fixture.sh
│   ├── conftest.py
│   ├── test_annotate.py
│   ├── test_blast_radius.py
│   ├── test_codified.py
│   ├── test_crawler.py
│   ├── test_extractors.py
│   └── test_registry_smoke.py
├── docs/
│   └── ROADMAP.md
├── .github/workflows/ci.yml
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── LICENSE
├── README.md
└── pyproject.toml
```

## What's left for the human

These are NOT code tasks — they're posting/distribution tasks only you can do.

### Days 26-30 — Distribution

- [ ] Git: init repo at the v2/ root (or move v2/* up one level), push to GitHub.
- [ ] PyPI: `python -m build && twine upload dist/*` (requires PyPI account).
- [ ] **Show HN:** "Engram: the local-first DevOps context layer that would
      have prevented the DataTalks.Club terraform-destroy incident"
      (include the BLOCK/red screenshot from the smoke test).
- [ ] **Reddit:** r/devops, r/ClaudeAI, r/cursor, r/kubernetes, r/Terraform.
- [ ] **dev.to writeup:** "Three channels of agent context: MCP, AGENTS.md,
      and metadata labels — and why the third one matters most."
- [ ] **MCP catalog submissions:**
  - https://registry.modelcontextprotocol.io/
  - https://cursor.directory/plugins
  - https://www.claudedirectory.org/
  - https://github.com/rohitg00/awesome-devops-mcp-servers (PR)
  - https://mcpmarket.com/
- [ ] **Email outreach** (pick 3-5):
  - Alexey Grigorev (DataTalks.Club) — show the assess output
  - Simon Willison — frame as "lethal trifecta mitigation, local-first"
  - Anthropic devrel — MCP server submission
  - HashiCorp Terraform-MCP team — complementary positioning
  - Continue.dev — air-gapped story

### Phase B (Days 31-60) — Measurement

- [ ] Run SWE-InfraBench with/without Engram. Publish numbers.
- [ ] Run DPIaC-Eval with/without Engram. Publish numbers.
- [ ] Author **Cross-Repo Blast Radius Benchmark** (50 scenarios). You own this.

### Phase C (Days 61-90) — Recognition

- [ ] arXiv preprint (use the README's 3-pillar framing as the abstract scaffold).
- [ ] CNCF Sandbox application (LICENSE, CoC, CONTRIBUTING all already in place).
- [ ] KubeCon EU 2026 / KubeCon NA 2026 talk submission.

## Smoke results from the final build

Indexing the fixture took **0.4s**, all 11 file types resolved, 16 resources
+ 27 entities + 82 edges + 13 technologies extracted.

`engram assess "terraform destroy" "datatalks_prod_db"` returns:

```
STOP  Action: BLOCK  Tier: red
  Operation:    terraform destroy  (destructive)
  Target:       datatalks_prod_db
  Environment:  production
  Resources:    1 resolved
  Dependents:   2
  Incidents:    0
Reasons:
  - Operation 'terraform destroy' is destructive (deletes/destroys state).
  - Target is in PRODUCTION.
  - 2 dependent resource(s) found.
```

`engram assess "kubectl delete deployment" "payments"` returns:

```
STOP  Action: BLOCK  Tier: red
  Resources: 5 resolved (k8s:Deployment + docker:image + compose:service
             + jenkins:pipeline + tf:aws_ecs_service)
  Dependents: 4
```

This is the cross-format, cross-repo wedge in one number.

`engram label --apply` writes `engram.io/*` labels onto K8s manifests and
Terraform `tags = {}` blocks, idempotently. `engram unlabel --apply` reverses
every change byte-for-byte.

`engram emit-agents-md` writes a signed AGENTS.md section. Subsequent
`engram check-drift` returns "up to date". Tampering with the section and
re-emitting refuses to overwrite without `--force`.

`engram mcp install --target all` writes the MCP entry into
`~/.claude/mcp.json`, `~/.cursor/mcp.json`, `~/.config/cline/mcp.json`,
and `~/.codex/mcp.json` idempotently.

**34/34 pytest passing on Python 3.12.**
