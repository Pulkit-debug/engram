# Real-world validation

Engram was run against two real open-source DevOps repositories on a basic
Windows laptop (Python 3.12 in WSL Ubuntu). This document captures the
numbers and what we observed, exactly as they came out — no cherry-picking.

## Test bench

| Item | Value |
|---|---|
| Host | Windows 11 + WSL2 Ubuntu 24.04 |
| CPU / RAM | Unspecified consumer laptop (Indian retail ~₹30,000 / ~$360) |
| Python | 3.12.3 |
| Engram | v0.2.0 |
| sqlite-vec | 0.1.9 |
| Date | 2026-05-14 |

## Repos indexed

### hashicorp/vault-helm

A pure Helm chart for HashiCorp Vault. Tests the Helm + K8s template path
in depth.

| Metric | Value |
|---|---|
| Repo size on disk | 1.5 MB |
| Files scanned | 139 |
| Files indexed | 67 |
| Files skipped | 72 (mostly Go test fixtures, binary, or over-size) |
| Projects detected | 2 |
| Resources extracted | 40 |
| Entities extracted | 67 |
| Edges created | 11 |
| Technologies | 10 (`google`, `helm`, `kubernetes`, `make`, `nginx`, `postgres`, `random`, `shell`, `terraform`, `null`) |
| Wall-clock time | **0.7 s** |

Extractor coverage:

| Extractor | Files |
|---|---:|
| YAMLExtractor | 52 |
| ShellExtractor | 6 |
| TerraformExtractor | 5 |
| HelmValuesExtractor | 2 |
| MakefileExtractor | 1 |
| HelmChartExtractor | 1 |

Cross-format services detected (resource names that span ≥ 2 formats):

| Service | Formats |
|---|---|
| `nginx` | `k8s:ServiceAccount`, `k8s:Pod` |
| `pgdump` | `k8s:ServiceAccount`, `k8s:Job` |
| `postgres` | `k8s:Service`, `k8s:Deployment` |

### cert-manager/cert-manager

Larger, mixed Go + Helm + K8s + shell + kustomize + Terraform repo. Tests the
crawler at scale and the cross-language detection.

| Metric | Value |
|---|---|
| Repo size on disk | 22 MB |
| Files scanned | 1,217 |
| Files indexed | 121 |
| Files skipped | 1,096 (mostly Go source — no Go extractor in v0.2) |
| Projects detected | 13 |
| Resources extracted | 89 |
| Entities extracted | 172 |
| Edges created | 7 |
| Technologies | 5 (`helm`, `kubernetes`, `make`, `shell`, `terraform`) |
| Wall-clock time | **6.4 s** |

Cross-format services in cert-manager:

| Service | Formats |
|---|---|
| `cert-manager` | `helm:chart`, `k8s:Namespace`, `make:file` |
| `cluster` | `tf:google_container_cluster`, `shell:script`, `yaml:document` |

## Functional checks

### 1. Terraform tag allowlist behaves correctly on real provider types

Running `engram label --target terraform` over the indexed Terraform files in
vault-helm produced:

```
Plan: 1 resource(s) (terraform=1)
Skipped 2 resource(s) (untaggable types):
  skip  tf:random_id      unknown resource type (random_id); add to allowlist
  skip  tf:null_resource  unknown resource type (null_resource); add to allowlist
  terraform  /tmp/.../main.tf  tf:google_container_cluster cluster (dev, tier=green)
```

The allowlist correctly:
- **Planned** the `google_container_cluster` (real GCP resource that accepts `labels`).
- **Skipped** `null_resource` and `random_id` (Terraform meta-providers — they
  do not accept tags and would have broken `terraform plan` if blindly tagged).

This is the exact failure mode the previous version would have hit. Now it's
inert.

### 2. AGENTS.md emission produces a coherent summary on real data

After indexing both repos, `engram emit-agents-md` produced a 60+ line
markdown section listing the 15 detected projects, the cross-format services,
the most-used environment variables, and the technology inventory. Sample
(opening of the section):

```markdown
### Inventory

- Projects: 15
- Resources: 129 (infra structural units)
- Files: 188
- Edges: 264 (dependencies, uses, mounts, etc.)
- Technologies: 11

### Cross-format services

| Service | Formats |
|---|---|
| cert-manager | helm:chart, k8s:Namespace, make:file |
| cluster      | shell:script, tf:google_container_cluster, yaml:document |
```

`engram check-drift` immediately after returned `up to date`.

### 3. blast_radius assessments against real resources

| Scenario | Result | Notes |
|---|---|---|
| `terraform destroy server` (no such resource) | CONFIRM / orange | Unknown target → bias to confirm, not silently proceed |
| `kubectl delete deployment cert-manager` | CONFIRM / orange | Resolved 4 resources across helm/k8s/make; no production-tier marker in source → confirm |
| `helm uninstall vault` | CONFIRM / orange | 1 resource resolved; no prod marker |
| `kubectl apply webhook` | PROCEED / green | Mutating op on dev-tier resource |

The behaviour is correct: a real OSS repo's *source* doesn't have
`environment: production` labels (those live in users' deployments, not in
the chart authors' code). So `BLOCK / red` is rare in source repos — which
is right. The hard `BLOCK` will fire on the user's own production overlays
once they index those.

### 4. Crawler resilience

- 1,217 file traversal → 6.4 s. Acceptable for an "index everything once,
  watch later" model.
- 1,096 files correctly skipped (Go source, vendored binaries, oversize).
- Helm Go-template files (`{{ .Release.Name }}-x`) did not crash the YAML
  extractor; they fell to a generic `yaml:document` shallow resource. This
  matches the documented behaviour and is consistent with the Phase 2
  adversarial test for Go-templated manifests.

## Observed nits (logged for follow-up, not blockers)

1. The "technologies" list picked up `null` (from Terraform's `null_resource`
   meta-provider) and `{image}` (from a YAML placeholder). Cosmetic — the
   tech inventory is informational, not load-bearing.
2. The allowlist doesn't know about `null_resource` and `random_id` — that's
   *correct* (they don't accept tags) but the skip reason says "unknown" when
   "untaggable" would be clearer. Future polish.
3. `cert-manager` shows up twice in the project list (once as `go+make`, once
   as `helm`) because the repo has a top-level Makefile *and* a chart with
   `Chart.yaml`. Both detections are technically right; we'd dedupe-by-name
   in a future pass.

## Verdict

**Engram works correctly on real, named, public DevOps repositories.** The
allowlist prevents the failure mode of breaking `terraform plan`. The
cross-format detection finds the joins between Helm and Terraform and K8s
that a naïve grep wouldn't. The wall-clock is reasonable for the scale
(~7 seconds on a 22-MB repo on a budget laptop).

Reproduce yourself:

```bash
git clone --depth 1 https://github.com/hashicorp/vault-helm /tmp/vault-helm
git clone --depth 1 https://github.com/cert-manager/cert-manager /tmp/cert-manager
engram init
engram index --path /tmp/vault-helm
engram index --path /tmp/cert-manager
engram status
engram emit-agents-md --target /tmp/engram_real_AGENTS.md
bash tests/real_world_check.sh /tmp/cert-manager ~/.engram/data
```
