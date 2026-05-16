# Engram — Build status

## v0.3 — Click-ops, benchmarks, sales material (SHIPPED ✓)

The strategic phase. Three things the v0.2 release deferred:

### Director's Brief (`docs/DIRECTORS_BRIEF.md`)

The conversion document for tech-fluent buyers (CTO, VP-of-Eng, recruiter,
non-engineer decider). Every smart objection — "why not just prompt?", "why
not AWS Config?", "doesn't Cursor already do this?" — answered before they
raise it. 2,500 words, anti-defensive tone, ready to paste into a LinkedIn
article.

### Real-repo benchmarks (`docs/BENCHMARKS.md`)

Indexed 6 deliberately-different OSS DevOps repositories:

- hashicorp/vault-helm — pure Helm
- cert-manager/cert-manager — Go + Helm + K8s + Make
- terraform-aws-modules/terraform-aws-eks — pure Terraform
- GoogleCloudPlatform/microservices-demo — multi-service realistic app
- docker/awesome-compose — 80 docker-compose examples
- prometheus-community/helm-charts — Helm monorepo, 48 projects

Headline numbers: **1,392 files indexed in 20.5 seconds** on a budget laptop;
**3,190 edges built**; **76 cross-format services detected**;
**28 / 28 hand-crafted assessment scenarios pass** (5–6 per repo, asserting
exact `(risk_tier, action)` outputs).

The benchmark is reproducible via `bash tests/benchmark_real_repos.sh` and
the scenario assertions live in `tests/test_scenario_replay.py` (28
parametrized pytest cases).

### Click-ops coverage (the IaC-coverage gap)

The v0.2 release covered 10 AWS services (RDS, EC2, S3, EKS, Lambda, ELB,
ECS, SQS, SNS, DynamoDB). v0.3 adds the *network, access, and secrets*
surface that determines blast radius:

- **VPC + Subnet + SecurityGroup** — network topology
- **IAM Role** — access surface
- **SecretsManager + SSM Parameter Store** — AWS-native env-var equivalent
- **Route53 HostedZone + RecordSet** — DNS (the click-ops blind spot)

### Pioneer features (genuinely new)

**Value-match inference** (`engram/inference/value_match.py`):

> If your `.env` file says
> `DATABASE_URL=postgres://payments-prod.cluster.us-east-1.rds.amazonaws.com:5432/db`
> and `engram import-cloud` discovered an RDS with that endpoint, a
> `DEPENDS_ON` edge is auto-created between the file and the RDS — *without
> any IaC linking them*.

This is the wedge no other tool fills. Sourcegraph, Augment, AWS Learned
Topology all miss it because they don't cross-reference env-var values
with discovered cloud-resource endpoints. Engram does, deterministically,
zero LLM involvement.

**User annotations** (`engram annotate` CLI):

For the ECS task with no Terraform, the legacy EC2 box that "just runs
prod" — tell Engram once. The `annotation_user` table records owner,
runbook, environment, free-text notes. Every future `assess` and
`infra_context` call uses them; user-set environment **overrides**
auto-inferred environment.

### v0.3 test counts

```
total:                                         124 / 124 passing

new in v0.3:
  tests/test_value_match.py                      8 / 8
  tests/test_user_annotations.py                 6 / 6
  tests/test_aws_import_extended.py              7 / 7
  tests/test_scenario_replay.py (parametrized) 28 / 28
```

`tests/full_demo.sh` runs **11 end-to-end checks** in under 60 seconds and
returns `RESULT: all 11 checks passed` on a clean machine.

---

## v0.2 — Production hardening (SHIPPED ✓)

Pushed to https://github.com/Pulkit-debug/engram. Package
`engram-devops` ready for PyPI (token-required final step).

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
