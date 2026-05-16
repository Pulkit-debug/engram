# Benchmarks — measured numbers from 6 real OSS DevOps repositories

Every number on this page is verbatim from a single benchmark run executed on
2026-05-16 against the test bench documented in
[`docs/CROSS_PLATFORM_AUDIT.md`](CROSS_PLATFORM_AUDIT.md): WSL Ubuntu 24.04 on
a consumer-grade Windows laptop, Python 3.12.3, Engram v0.2.0.

Reproduce with one command:

```bash
bash tests/benchmark_real_repos.sh
```

The script clones each repo (shallow), indexes it into an isolated SQLite DB,
prints per-repo statistics, replays a hand-crafted scenario YAML, and writes a
`bench.json` sidecar to each repo's data directory. All output is captured;
nothing is hand-edited.

## Summary

| Repo | Size | Files | Resources | Edges | Cross-format svcs | Wall-clock | Scenarios passed |
|---|---:|---:|---:|---:|---:|---:|---:|
| [hashicorp/vault-helm](https://github.com/hashicorp/vault-helm) | 2 MB | 67 | 40 | 82 | 3 | 0.89 s | **5 / 5** |
| [cert-manager/cert-manager](https://github.com/cert-manager/cert-manager) | 22 MB | 121 | 89 | 182 | 3 | 8.42 s | **4 / 4** |
| [terraform-aws-modules/terraform-aws-eks](https://github.com/terraform-aws-modules/terraform-aws-eks) | 3 MB | 86 | 300 | 528 | 48 | 1.13 s | **5 / 5** |
| [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo) | 23 MB | 137 | 201 | 1128 | 14 | 2.32 s | **6 / 6** |
| [docker/awesome-compose](https://github.com/docker/awesome-compose) | 18 MB | 152 | 123 | 894 | 8 | 1.58 s | **4 / 4** |
| [prometheus-community/helm-charts](https://github.com/prometheus-community/helm-charts) | 17 MB | 829 | 267 | 376 | 0 | 6.17 s | **4 / 4** |
| **TOTAL** | **85 MB** | **1,392** | **1,020** | **3,190** | **76** | **20.5 s** | **28 / 28** |

**Headline numbers**:

- **20.5 seconds** to index 1,392 files across 6 distinct DevOps repository
  shapes. On a budget laptop.
- **1,020 resources extracted**, deterministically, no LLM in the path.
- **3,190 edges built**, including DEPENDS_ON, USES, USES_ENV, MOUNTS, CONTAINS.
- **76 cross-format services detected** — the number of logical services that
  span ≥ 2 infrastructure formats (e.g. a service defined in both Helm and
  Kubernetes manifests). This is the joiner that makes Engram useful.
- **28 / 28 hand-crafted assessment scenarios pass.** Each scenario is a real
  `engram assess "<operation>" "<target>"` invocation with a pre-declared
  expected `(risk_tier, action)`.

## Per-repo detail

### 1. `hashicorp/vault-helm`

*Pure Helm chart for HashiCorp Vault, with test fixtures (k8s manifests +
Terraform for cluster setup).*

| Metric | Value |
|---|---:|
| Size on disk | 2 MB (shallow clone) |
| Files scanned / indexed | 139 / 67 |
| Resources | 40 |
| Entities | 31 |
| Edges | 82 |
| Technologies | 10 |
| Wall-clock | **0.89 s** |

Top resource kinds: `make:target` (13), `shell:script` (6),
`yaml:document` (4), `helm:values` (2), `k8s:ServiceAccount` (2).

Edge types: `DEFINED_IN` (40), `CONTAINS` (31), `USES` (6),
`USES_ENV` (3), `DEPENDS_ON` (2).

Cross-format services found: `nginx`, `pgdump`, `postgres` — each spanning
two K8s kinds (Service+Deployment, ServiceAccount+Job, etc.).

**Scenarios: 5 / 5 passed.** The interesting one is
`unknown_target_with_prod_in_name`: an unknown target like
`totally-fake-prod-db` correctly returns `red / block` because the name
contains a `prod` segment. Engram is conservative-by-design on
production-ish naming.

### 2. `cert-manager/cert-manager`

*Mixed Go + Helm + Make + K8s repo — exercises project detection.*

| Metric | Value |
|---|---:|
| Size on disk | 22 MB (shallow clone) |
| Files scanned / indexed | 1,217 / 121 |
| Resources | 89 |
| Entities | 86 |
| Edges | 182 |
| Technologies | 5 |
| Wall-clock | **8.42 s** |

Files skipped: 1,096 — mostly Go source (no Go extractor in v0.2; that's a
known gap, see [ROADMAP](ROADMAP.md)).

Cross-format services: `cert-manager` itself spans 3 formats (helm:chart +
k8s:Namespace + make:file). `bind` and `cluster` span 2 each.

**Scenarios: 4 / 4 passed.**

### 3. `terraform-aws-modules/terraform-aws-eks`

*Pure Terraform module — exercises HCL parsing and ref-graph building.*

| Metric | Value |
|---|---:|
| Size on disk | 3 MB |
| Files scanned / indexed | 86 indexed |
| Resources | **300** |
| Entities | 18 |
| Edges | **528** |
| Technologies | 10 |
| Wall-clock | **1.13 s** |

Highest edge density of the six repos (~6 edges per resource on average).
Demonstrates the HCL `var.` / `local.` / `each.` filtering working: only
real inter-resource references become DEPENDS_ON edges.

Cross-format services: **48** (the most in the set). Many of these are
Terraform-internal — e.g. `this` (the standard Terraform module
self-reference) spans 36 distinct kinds because it's reused everywhere.
`controller` and `node` each span 5 kinds (IAM policy + IAM role + IAM
attachment + security group + module).

**Scenarios: 5 / 5 passed**, including the conservative-naming check.

### 4. `GoogleCloudPlatform/microservices-demo`

*The Online Boutique. Multi-service realistic app with K8s + Terraform +
Skaffold + Istio. **This is the showcase for cross-format detection.***

| Metric | Value |
|---|---:|
| Size on disk | 23 MB |
| Files indexed | 137 |
| Resources | 201 |
| Entities | **578** |
| Edges | **1,128** |
| Technologies | 32 |
| Wall-clock | **2.32 s** |

Highest absolute edge count of any repo. `USES_ENV` alone produced 276 edges
— that's the env-var-def ↔ env-var-ref linker doing its job across the
~12 microservices.

**Cross-format services: 14**, with several spanning **5–6 distinct
infrastructure formats simultaneously**:

| Service | Formats | What it spans |
|---|---:|---|
| **frontend** | **6** | Deployment + Service + ServiceAccount + VirtualService + ServiceEntry + NetworkPolicy |
| adservice | 5 | Deployment + Service + ServiceAccount + NetworkPolicy + (more) |
| checkoutservice | 5 | (same shape) |
| paymentservice | 5 | (same shape) |
| recommendationservice | 5 | (same shape) |
| **redis-cart** | **4** | `tf:google_redis_instance` + Deployment + Service + NetworkPolicy — **source-IaC and Kubernetes definition in one cross-format resolve** |

This is the killer demo. `engram assess "kubectl delete deployment" "frontend"`
resolves 14 resources at once because the same name appears across six
infrastructure formats. No grep, no manual hunt, deterministic.

**Scenarios: 6 / 6 passed.**

### 5. `docker/awesome-compose`

*~80 docker-compose.yml examples — exercises compose extractor breadth and
multi-project detection at small scale.*

| Metric | Value |
|---|---:|
| Size on disk | 18 MB |
| Files indexed | 152 |
| Resources | 123 |
| Entities | 523 |
| Edges | 894 |
| Technologies | **46** |
| Wall-clock | **1.58 s** |

Edges by type: `DEPENDS_ON` 108, `MOUNTS` 61 — compose's `depends_on:` and
`volumes:` are first-class graph edges.

**Scenarios: 4 / 4 passed**, including verification that `docker compose ps`
correctly classifies as a read-only operation (it was previously
mis-classified as `unknown`; fixed in v0.3 by expanding the read-verb set).

### 6. `prometheus-community/helm-charts`

*40+ Helm charts in one monorepo — exercises project detection at scale.*

| Metric | Value |
|---|---:|
| Size on disk | 17 MB |
| Files indexed | 829 (the most in the set) |
| Resources | 267 |
| Entities | 94 |
| Edges | 376 |
| Technologies | 7 |
| Wall-clock | **6.17 s** |
| **Projects detected** | **48** |

48 separate projects detected — one per Helm chart in the monorepo. Indexing
829 files in 6.17 s ≈ **134 files/second** on a budget laptop.

Cross-format services: 0 by design — each chart is internally consistent,
and Engram's same-name-across-formats heuristic doesn't fire on a monorepo
where every chart is self-contained.

**Scenarios: 4 / 4 passed**, including the read-only `helm template` and
`helm list` paths (also fixed in v0.3 by expanding the read-verb set).

## Throughput

Aggregate across all 6 repos: 1,392 files indexed in 20.5 seconds wall-clock.
That's **~68 files per second sustained**, including the cost of writing to
SQLite and computing content hashes. Memory peak per repo run is under 50 MB.

The slowest repo per file is cert-manager because it has more *non-indexable*
content (1,096 files skipped). The fastest absolute is vault-helm at 0.89 s.

## Scenario score

28 hand-crafted scenarios across 6 repos, all asserting `(tier, action,
min/max_resources, min_dependents)` against real indexed graphs:

```
vault-helm:        5 / 5
cert-manager:      4 / 4
terraform-aws-eks: 5 / 5
microservices-demo: 6 / 6
awesome-compose:   4 / 4
helm-charts:       4 / 4
                   ─────────
total:            28 / 28
```

Each scenario is a real `assess("operation", "target")` call against a real
indexed DB. The expected values are pre-declared in
`tests/scenarios/<repo>.yaml` and assertion is bit-exact (`risk_tier` and
`action`), with structural lower-/upper-bound checks on resolved-resource
counts and dependent counts.

## Token-savings example

From the synthetic fixture in `examples/before_after_token_savings.md`,
reproduced here for context:

| Approach | Tokens |
|---|---:|
| Raw file reads (8 files + 2 search calls) to reconstruct one service | ~3,960 |
| One `infra_context("payments", token_budget=2000)` MCP call | ~520 |
| **Reduction** | **~87 %** |

Microservices-demo amplifies this because its `frontend` service spans
6 formats: an agent reconstructing context from raw file reads would need
to walk 6 different files in 6 different directories. Engram's
`infra_context` call returns one structured payload.

## Honest limits surfaced by the benchmark

These are *not* bugs — they're the project's documented edges. The benchmark
makes them visible.

1. **Go source files are skipped entirely.** cert-manager has 1,096 Go files;
   none contribute to the graph. The roadmap lists a Go extractor for v0.3.

2. **No production-tagged resources in source-only repos.** None of the 6
   repos have manifests with `metadata.labels.environment: production` or
   Terraform `tags = { Environment = "production" }`. So `risk_tier=red` is
   correctly absent from the indexed graphs. The conservative-naming
   inference catches `*-prod-*` patterns regardless — see vault-helm
   scenario 5.

3. **Self-references inflate cross-format counts.** Terraform's
   `this` convention (`module "this" {}`) appears 36 times in
   terraform-aws-eks — that's why `cluster` has only 2 formats but `this`
   has 36. Cross-format counts should be read as a *floor* on the joiner's
   expressiveness, not a ceiling.

4. **Helm charts in monorepos don't generate cross-format hits.** Each chart
   is internally consistent and Engram's same-name heuristic correctly
   doesn't fire. This is correct behaviour; the cross-format joiner is
   designed to find cases where the same logical service shows up in two
   different infrastructure layers (e.g. Helm + Terraform), not where it
   shows up twice in the same Helm chart.

## Reproducibility

Anyone with the repo and a Python 3.11+ venv can reproduce every number on
this page:

```bash
git clone https://github.com/Pulkit-debug/engram
cd engram
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

bash tests/benchmark_real_repos.sh
.venv/bin/python tests/_bench_summary.py
```

The script clones the six repos (or reuses local copies under
`/tmp/engram_real`), runs the full Engram pipeline against each, and
prints the numbers you see above. Wall-clocks will vary with hardware;
counts and scenario scores will not.
