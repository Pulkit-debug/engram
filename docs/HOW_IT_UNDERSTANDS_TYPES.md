# How Engram understands types

Short answer: **Engram is a type-aware indexer, not an AI.** It trusts the
type tags that already exist in your YAML, HCL, Dockerfiles, etc. The
semantic understanding ("what is a Pod, exactly?") lives in the LLM that
calls Engram — not in Engram itself.

This is the right division of labor. Engram is fast, deterministic, and
testable. The LLM brings the reasoning. Together they work; alone, neither
does the job.

## What "type-aware" means in practice

### Kubernetes manifests

```yaml
apiVersion: apps/v1
kind: Deployment        # ← Engram reads this literally
metadata:
  name: payments
  namespace: prod
```

The YAML extractor reads the `kind:` field and stores the resource as
`k8s:Deployment`. A `kind: Pod` becomes `k8s:Pod`. A `kind: ReplicaSet`
becomes `k8s:ReplicaSet`. **Engram does not know what those things are.**
It only knows they're distinct types, and that they have a name and
namespace.

That's enough to answer questions like:

- "Show me all Deployments in `prod`" — straight SQL on the `resource`
  table filtered by `kind='k8s:Deployment'` and `namespace='prod'`.
- "What depends on this service?" — graph traversal over `DEPENDS_ON` /
  `USES_ENV` edges.
- "Is this in production?" — `risk_tier` column, populated by env
  inference from labels / namespace / path segments.

The LLM still does the reasoning ("a Pod usually has fewer guarantees
than a Deployment"). Engram just hands it the type-stamped facts.

### Terraform

```hcl
resource "aws_db_instance" "prod_db" {
  identifier     = "datatalks-prod-db"
  engine         = "postgres"
  instance_class = "db.r5.large"
  tags = { Environment = "production" }
}
```

The HCL parser extracts `(type="aws_db_instance", name="prod_db")` and
stores `tf:aws_db_instance` as the resource kind. It also reads the
`Environment = "production"` tag and sets `environment="production"` on
the resource. The taggable-resource allowlist (`engram/output/tf_taggable.py`)
encodes which provider resource types accept tags — that's the only
provider-specific knowledge baked in.

### Cross-format detection

When Engram sees:

```
docker:image       payments  ./services/payments/Dockerfile
compose:service    payments  ./services/payments/docker-compose.yml
jenkins:pipeline   payments  ./services/payments/Jenkinsfile
k8s:Deployment     payments  ./infra/prod/k8s/payments.yaml
tf:aws_ecs_service payments  ./infra/prod/main.tf
```

It does NOT use NLP or fuzzy matching. It uses the simplest possible
rule: **same `name` → same logical service.** That's a convention every
DevOps team already follows (otherwise debugging is impossible). When
you ask `engram infra_context("payments")`, all five rows come back
because they share a name.

## What Engram does NOT understand

Be explicit about the limits so you don't oversell:

1. **Custom Resource Definitions (CRDs).** A manifest with
   `kind: ArgoCDApplication` is stored as `k8s:ArgoCDApplication`. Engram
   has no idea ArgoCD Applications are deployment-like things. Risk tier
   inference from labels still works because the `metadata.labels.environment`
   check is type-agnostic.

2. **Helm template interpolation.** A template file with
   `name: {{ .Release.Name }}-payments` is *not* evaluated. Engram falls
   back to a generic `yaml:document` shallow record because PyYAML can't
   parse it. The Helm chart itself (Chart.yaml) is fully indexed.

3. **Pulumi / CDK / Crossplane compositions.** No extractor for these in
   v0.2. They're imperative-language IaC — supporting them properly means
   actually running the program or AST-parsing Python/TypeScript with
   intent recognition. Out of scope for the deterministic-extractor model.
   Workaround: use the cloud import (next section) to capture the deployed
   state regardless of how the IaC was written.

4. **What a resource is *for*.** Engram knows you have an
   `aws_db_instance` named `prod_db`. It does not know whether that
   database stores user accounts, billing records, or analytics. That's
   what the LLM is for. Engram can store an explicit memory via
   `engram remember` if you want to attach business meaning, but it
   won't infer it.

## The honest division of labor

| Layer | Who does it | How |
|---|---|---|
| File discovery | Engram crawler | os.walk + ignore patterns |
| Type extraction | Engram extractors | deterministic parsers (ast, PyYAML, regex, tree-sitter) |
| Cross-format joins | Engram graph | `same name → same service` |
| Risk-tier inference | Engram safety layer | path segments, labels, tags |
| Blast-radius assessment | Engram safety layer | graph walk + risk tier |
| Semantic interpretation | The LLM | "what does this resource mean in this codebase?" |
| Action selection | The LLM | "should I proceed, given Engram's tier?" |

The phrase "trust but verify" applies in both directions: the LLM trusts
that Engram's type tags are accurate (they are, because they came
verbatim from your YAML), and Engram trusts that the LLM will read the
risk tier and act accordingly.

If the LLM ignores the `BLOCK` recommendation, that's an agent-harness
problem (configure a PreToolUse hook), not an Engram problem. Engram's
job is to surface the facts.

## Why not use LLM-based extraction?

Two reasons:

1. **Determinism matters for safety.** A regex that reads `kind: Pod`
   always produces the same answer. An LLM might one day decide that a
   pod-with-no-resource-limits is "really a CronJob" and emit different
   metadata. For a tool whose job is preventing `terraform destroy` on
   prod, determinism is the entire point.

2. **Cost and latency.** Engram indexes 1,217 files in 6.4 seconds on
   a ₹30,000 laptop. An LLM-based equivalent would be at least 100x
   slower and cost money per index. The whole pitch is "local-first,
   zero API keys."

LLM-based interpretation happens at *query time*, when the agent asks
Engram a question and reasons over the response. That's where the
intelligence belongs.
