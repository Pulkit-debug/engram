# Bringing click-ops resources into the graph

Most teams have production resources that were created in the cloud console
or `kubectl apply`-d once and never tracked in source. Engram's file-based
indexer would normally be blind to these — and that's the single biggest
gap in a tool that promises "blast-radius for any destructive operation."

This document is the fix. Engram now ingests live state via the user's
existing cloud / cluster CLIs.

## Design rule: Engram never authenticates to your cloud

The local-first promise stands. The flow is:

```
your laptop ─┬─► aws  CLI ──► AWS API   ──► JSON ──► engram parses ──► SQLite
             └─► kubectl ───► K8s API   ──► JSON ──► engram parses ──► SQLite
```

Engram only runs `subprocess.run()` on a CLI you already trust on a machine
you already control. No new credentials. No API keys in Engram's config.
No outbound network traffic from Engram itself.

If you're air-gapped, you can produce the JSON some other way and pipe it
in (a `--from-file` flag is on the roadmap — for v0.2 the CLI-shell-out is
the supported path).

## AWS

```bash
# Discover the common resource types:
engram import-cloud --provider aws \
  --kinds rds,ec2,s3,eks,lambda,elb,ecs,sqs,sns,dynamodb

# Restrict to a region and profile:
engram import-cloud --provider aws --region eu-west-1 --profile prod
```

Each discovered resource becomes a graph `Resource` with:

| Field | Source |
|---|---|
| `kind` | `aws:<service>:<type>` — e.g. `aws:rds:DBInstance`, `aws:s3:Bucket` |
| `name` | resource identifier (DBInstanceIdentifier, bucket name, ARN tail) |
| `namespace` | AWS region |
| `environment` | inferred from `Environment` / `env` / `stage` tag, else from name suffix |
| `risk_tier` | `red` if production, `orange` if staging, else `green` |
| `properties.discovered_from` | `"aws-cli"` (so you can tell IaC vs click-ops) |
| `properties.account` | the AWS account ID |
| `properties.arn` | the resource ARN |
| `properties.region` | the AWS region |

**Supported kinds in v0.2:** `rds`, `ec2`, `s3`, `eks`, `lambda`, `elb`,
`ecs`, `sqs`, `sns`, `dynamodb`. More will land as users ask for them
(open an issue; new kinds are ~80 LOC each).

### What this enables

Click-ops resource → real blast-radius check:

```text
$ engram import-cloud --provider aws --kinds rds
  Total inserted: 14

$ engram assess "terraform destroy" "users-prod"

STOP  Action: BLOCK  Tier: red
  Operation:    terraform destroy  (destructive)
  Target:       users-prod
  Environment:  production
  Resources:    1 resolved
Reasons:
  - Operation 'terraform destroy' is destructive.
  - Target is in PRODUCTION.
```

The `users-prod` RDS was never in Terraform. Engram still knows it's
production because the AWS tag said so.

## Kubernetes — live cluster state

```bash
# Current kubeconfig context, all namespaces:
engram import-cluster

# Specific context:
engram import-cluster --context production-east

# One namespace only:
engram import-cluster --namespace payments
```

Default kinds: `Deployment`, `StatefulSet`, `DaemonSet`, `ReplicaSet`,
`Pod`, `Service`, `ConfigMap`, `Secret`, `Ingress`, `Job`, `CronJob`,
plus RBAC and storage objects. Override with `--kinds Pod,Service`.

### Drift detection (automatic)

When you import both YAML manifests and the live cluster, Engram stores
*both* representations:

```text
file path                                          kind             discovered_from
/repo/payments/k8s.yaml                            k8s:Deployment   (none — from YAML)
kube://prod-cluster/prod                           k8s:Deployment   kubectl
```

Both are visible to `infra_context("payments")` and `assess(...)`. The
`properties.discovered_from` field tells you whether the row came from
source or from the live cluster. Run them at different times to spot
drift; future versions will surface drift as a first-class diff.

## Repeating the import

Re-running `engram import-cloud` / `engram import-cluster` is idempotent:
existing rows are updated, new resources are inserted. Schedule it (cron,
GitHub Actions on a cadence, etc.) to keep the graph fresh.

## When NOT to use cloud import

- Your cloud is small enough that Terraform / Pulumi covers everything in
  IaC. The source-file flow is sufficient.
- You're indexing a public OSS repo (no AWS account attached to it).
- Your security team forbids automated cloud reads — Engram respects this
  by being opt-in: nothing runs until you type `import-cloud`.

## Failure modes

| Failure | What happens |
|---|---|
| `aws` CLI not installed | Engram prints "AWS CLI not found" and exits 0 with stats showing zero inserts. |
| AWS credentials missing | Engram surfaces the `aws sts get-caller-identity` error message verbatim. |
| One service call fails (e.g. you lack RDS read but have EC2 read) | The failing kind is logged with its reason; other kinds continue. |
| Network timeout | 60-second per-call timeout per kind; logged and skipped. |
| Non-JSON output | Logged with the malformed kind name; skipped. |

In none of these does Engram crash, partially corrupt the DB, or silently
log nothing.
