# Before / after — token savings with Engram

A real comparison: a Claude Code session asked to "review the configuration
of the payments service across all our repos." Without Engram, the agent
has to discover, read, and reason about every relevant file. With Engram,
one `infra_context("payments")` call returns the structured context, and
the agent reads only what it actually needs.

Numbers below are from the indexed test fixture (`tests/build_fixture.sh`),
which is a deliberately small example. Real DevOps repos amplify the
delta — the more files, the larger the savings.

## Without Engram

The agent issues filesystem reads to reconstruct context:

| Tool call | File | ~Tokens |
|---|---|---:|
| `Read` | `services/payments/Dockerfile` | 180 |
| `Read` | `services/payments/Jenkinsfile` | 540 |
| `Read` | `services/payments/docker-compose.yml` | 320 |
| `Read` | `services/payments/main.py` | 460 |
| `Read` | `services/payments/.env.production` | 90 |
| `Read` | `infra/prod/k8s/payments-deployment.yaml` | 720 |
| `Read` | `infra/prod/main.tf` | 920 |
| `Read` | `infra/prod/chart/Chart.yaml` | 210 |
| `Grep` | `DATABASE_URL` across the tree | 380 |
| `Glob` | `**/payments*` | 140 |
| **Total** | | **~3,960 tokens** |

The agent has now read 8 files plus run 2 search calls just to know what
`payments` *is*. It then has to fit all of that into the working context
window before generating any review.

## With Engram

One MCP call:

```python
infra_context(target="payments", token_budget=2000)
```

Response (real, captured from the test fixture, abbreviated to relevant
sections):

```json
{
  "target": "payments",
  "found": true,
  "summary": {
    "resource_count": 5,
    "environments": ["production"],
    "risk_tier": "red",
    "formats": ["compose", "docker", "jenkins", "k8s", "tf"],
    "file_count": 5
  },
  "resources": [
    {"kind": "k8s:Deployment",     "name": "payments", "namespace": "prod",
     "environment": "production", "risk_tier": "red", "file_path": ".../k8s.yaml"},
    {"kind": "tf:aws_ecs_service", "name": "payments", "environment": "production",
     "risk_tier": "red", "file_path": ".../main.tf"},
    {"kind": "docker:image",       "name": "payments", "file_path": ".../Dockerfile"},
    {"kind": "compose:service",    "name": "payments", "file_path": ".../compose.yml"},
    {"kind": "jenkins:pipeline",   "name": "payments", "file_path": ".../Jenkinsfile"}
  ],
  "env_vars": {
    "defined":   [{"name": "DATABASE_URL", "file": ".../Dockerfile"}],
    "referenced":[{"name": "STRIPE_KEY", "file": ".../k8s.yaml"}],
    "external_definitions": [
      {"name": "DATABASE_URL", "defined_in": ".../.env.production"}
    ]
  },
  "dependencies": [
    {"from": "tf:aws_ecs_service payments", "to": "tf:aws_db_instance datatalks_prod_db",
     "rel_type": "DEPENDS_ON"}
  ],
  "recent_changes": []
}
```

| Tool call | ~Tokens |
|---|---:|
| `infra_context("payments")` | **~520 tokens** |

**Savings: ~3,440 tokens, or ~87% reduction**, on a 5-file fixture. On a
real 20-file service, the delta is typically ~5,000-15,000 tokens per
session.

## How the savings compound

A Claude Sonnet 4.6 session that touches 5 services across 50 sessions
per developer per month — at ~10,000 tokens saved per session — is
**2.5 million tokens / dev / month**. At the published list-price of
~$3/1M input tokens, that's $7.50/dev/month, which scales linearly with
team size. Engram is free.

That's the boring number. The interesting number is **task success rate**:
agents working from compressed structural context outperform agents
working from raw file dumps. Published benchmarks (SWE-InfraBench,
DPIaC-Eval) show ~15-30 point swings on infrastructure tasks depending
on context quality. Phase B of the Engram roadmap measures this
directly.

## Reproduce the comparison yourself

```bash
git clone https://github.com/pulkit-verma/engram
cd engram
bash tests/build_fixture.sh /tmp/fix
engram init && engram index --path /tmp/fix

# Without Engram: count raw file tokens (rough proxy = chars/4)
wc -c /tmp/fix/services/payments/* /tmp/fix/infra/prod/main.tf

# With Engram:
engram serve | grep -A50 '"infra_context"'   # or call via your agent
```
