# Engram — Director's Brief

*The conversion document. Read this before reading the README.*

---

## What it is, in one sentence

Engram is the local-first context and safety layer that sits between an AI
coding agent and your infrastructure — so when the agent decides to run
`terraform destroy`, it knows what's about to break, what's in production,
and whether to stop.

## Why now

Between February and May 2026, four publicly-named incidents made the same
shape:

| Date | Incident | Outcome |
|---|---|---|
| Feb 26, 2026 | DataTalks.Club — Claude Code ran `terraform destroy` on production | VPC + RDS + ECS wiped; 100,000-student learning platform offline for 24 hours; 1.94M rows recovered from a snapshot AWS hadn't told them existed |
| Mar 12, 2026 | Meta SEV1 — agent exceeded its intended scope | 2-hour blast radius before a human stopped it |
| Apr 21, 2026 | PocketOS / Railway — agent found an over-permissioned token, deleted a production data volume | 9 seconds from decision to destruction |
| Apr–May, 2026 | Vercel context-AI supply-chain attack; CSA paper on SKILL.md / CLAUDE.md poisoning; OWASP names "context poisoning" a top agentic risk for 2026 | Reputational + trust loss |

In all four, the agent wasn't malicious. It wasn't a model bug. The agent
had **no concept of "this is production."** The context it operated on was
either missing, stale, or hostile. The destruction was the rational
consequence of incomplete information.

This is now the shape of every CTO's recurring nightmare. The 2026 Kiteworks
report found that **60% of organisations cannot terminate a misbehaving
agent.** A simulation in the same report showed a single compromised agent
can poison 87% of downstream decisions within four hours.

The market has a name for the problem: *blast radius*. It does not yet
have a serious local-first tool for it. That is the gap.

---

## "Why don't we just …"  — the smart objections, answered

These are the objections I expect every senior engineer to raise. Each
one is real. None of them solve the problem fully on its own.

### "Just write a careful system prompt."

Prompts are **stochastic**. The same prompt produces different outputs
across runs, across model versions, across context-window pressures.
The DataTalks.Club agent was Claude Code at state of the art; if
"be careful" had worked, the database would still exist.

Prompts also **cost tokens on every operation**. A 500-token safety
preamble against a million tool calls a month is real money — and it
sits in the context window, displacing the work you want the agent to
do.

Prompts **cannot be enforced at the harness layer**. The agent can
silently override them. An Engram `blast_radius` call is deterministic,
sub-100ms, and can be wired into Claude Code's PreToolUse hook so the
agent cannot proceed without it. That's enforcement, not request.

### "Just verify everything manually."

You should. But this defeats the point of an autonomous agent. If a
human has to approve every action, the agent saves no time over typing
the commands yourself. The right model is *agent acts within a safe
envelope.* Engram defines the envelope. Manual review is what happens
when the agent hits the envelope's edge — not on every single op.

### "Isn't this AWS Config + good tags?"

AWS Config is a continuous-compliance product. It runs *in your AWS
account*, costs ~$0.003 per configuration item per region per recording,
and produces JSON snapshots — not a context layer that an LLM can read
through MCP, not a cross-format graph that joins your source IaC to
your live cloud state, not a tool that runs on a developer's laptop
before the destructive action ever leaves the shell.

Engram is **local**, **free**, joins **source files + cloud state +
service-to-resource graph** into one queryable surface, speaks **MCP
natively**, and runs in **air-gapped or MDM-locked** environments
where AWS Config cannot reach.

The two are complementary. AWS Config tells your auditor what happened.
Engram tells your agent what's about to happen.

### "Doesn't Cursor / Claude Code already index my codebase?"

Single-repo. App-code-focused. No infra extractors. No cloud state. No
blast-radius primitive. No cross-repo joins.

The agent that destroyed DataTalks-Club's database was Claude Code. The
indexing helped it write the destroy command faster — it didn't help it
not write the command at all.

### "Isn't this Sourcegraph Amp / Augment Intent?"

Both are cloud SaaS. Enterprise pricing. Cannot deploy into FedRAMP,
ITAR, on-prem, or air-gapped environments. They serve the top 5% of
organisations who can buy them. Engram serves the bottom 95% — the
small platform team, the lone DevOps engineer, the regulated industry,
the locked-down corporate laptop where the security team won't let you
SSO into a new SaaS vendor.

### "My agent isn't autonomous enough to need this."

Yet. The trend line is unambiguous. Claude Code, Cursor, Cline, Codex,
Devin — they're all moving toward longer-running, less-supervised tool
use. The DataTalks.Club agent was running unattended for the same
reason: the work it does well, it does fast.

The teams who install Engram in 2026 will be the teams who keep their
production intact through 2027.

### "We don't even have proper IaC."

This is the most important objection — and the one Engram was rebuilt
to handle. Most production resources at the median company are not in
Terraform. They were clicked into existence in the AWS console, or
`kubectl apply`-d once and never tracked. Engram handles this *natively*
with three commands:

```
engram import-cloud --provider aws        # discover everything in your AWS account
engram import-cluster                     # discover everything in your K8s cluster
engram annotate aws:rds:prod-db \         # tell Engram what only you know
    --env production --owner platform
```

Engram never talks to AWS itself. It shells out to *your* `aws` CLI
with *your* credentials. The local-first promise stays intact.

This is the wedge nobody else has filled. Cursor doesn't read your AWS
account. Sourcegraph can't see your console-clicked resources. AWS
Config can, but it can't tell an LLM about them. Engram does both.

---

## 30-second proof

This is real terminal output. Verbatim. From the test bench shipped with
the package.

```
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

This is what Claude Code or Cursor sees over MCP before the destroy runs.
At `tier: red`, an Engram-wired PreToolUse hook refuses the call entirely.
At `tier: orange`, it surfaces the assessment and asks a human. At `tier:
green`, it proceeds.

`docs/REAL_WORLD_VALIDATION.md` shows the same primitive working against
the open-source codebases of cert-manager (1,217 files indexed in 6.4
seconds on a ₹30,000 Windows laptop), vault-helm, and others. No demo
animations; just real terminal captures.

---

## Architecture in five bullets

1. **One SQLite file** at `~/.engram/data/engram.db`. WAL mode for
   concurrent reads. Plain text-readable with the `sqlite3` CLI. No
   external services; no daemons.

2. **21 file types parsed deterministically.** Kubernetes manifests,
   Terraform, Helm charts, Dockerfiles, Jenkinsfiles, docker-compose,
   `.env`, package.json, requirements.txt, Python AST, JS/TS via
   tree-sitter. No LLM in the extraction path — extraction is fast and
   reproducible.

3. **Cloud + cluster discovery via the user's own CLIs.** Engram never
   authenticates to AWS, GCP, Azure, or your Kubernetes API. It shells
   out to the `aws` / `kubectl` CLIs you already trust on the laptop
   you already control, and parses their JSON output. Air-gapped works;
   ITAR works; corporate-VPN-MDM works.

4. **MCP server, ten tools.** `blast_radius`, `infra_context`,
   `trace_env_var`, `dependents_of`, `infra_diff`, `service_map`,
   `verify_context`, `remember`, `recall`, `forget`. One-line install
   into Claude Code, Cursor, Cline, or Codex.

5. **Three output channels — pick whichever your environment allows.**
   MCP for hooked-up agents. Signed `AGENTS.md` markdown sections for
   any agent that reads the file (works air-gapped). `engram.io/*`
   labels written directly onto Kubernetes manifests and Terraform
   resources so even agents that have never heard of Engram see its
   context inline through `kubectl describe`.

---

## Why it composes, doesn't compete

Engram is not a new MCP action server. It does not run kubectl. It does
not apply terraform. It does not deploy anything.

| Layer | Who does it |
|---|---|
| Action — *do* the thing | Terraform-MCP, Kubernetes-MCP, Azure-DevOps-MCP, GitHub-MCP |
| **Context and safety — *should we* do the thing** | **Engram** |
| Reasoning — *what* should we do | The LLM (Claude, GPT, Gemini) |

Engram sits between the LLM and the action layer. The Anthropic
PreToolUse hook is the integration point: before any destructive bash
command runs, Engram is called. Below `red`, the call proceeds; at
`red`, it stops.

Vendors building action MCP servers should *want* Engram in front of
them — it's the difference between an agent destroying a production
database and an agent learning that the database has three dependents
and a runbook.

---

## The economic case

### Tokens, measured

From `examples/before_after_token_savings.md`, on the test fixture:

| Approach | Tokens |
|---|---|
| Raw file reads + grep to reconstruct one service's context | ~3,960 |
| One `infra_context("payments", token_budget=2000)` call | ~520 |

That's an **87% reduction per session**, on a deliberately small example.
Real repos amplify this — typically 5,000-15,000 tokens saved per
agent session.

A Claude Sonnet 4.6 user running 50 agent sessions a month, touching
5 services per session, at $3 per million input tokens, saves:

> 50 × 5 × ~10,000 = 2.5M tokens / month / developer
> = ~$7.50 / dev / month at list price
> = $90 / dev / year, scaling linearly with team size

For a 30-engineer platform team, that's ~$2,700/year. The deeper
benefit isn't the dollars — it's the **task success rate**. Agents
working from compressed structural context outperform agents working
from raw file dumps by 15-30 points on published infrastructure
benchmarks (SWE-InfraBench, DPIaC-Eval). Engram puts that delta
within reach for any developer.

### Incidents, prevented

The DataTalks-Club incident took the platform offline for 24 hours.
Direct revenue loss is private, but the team lost the equivalent of one
full sprint to recovery and post-mortem. PocketOS's 9-second volume
deletion was unrecoverable without backups. Meta's SEV1 cost two hours
of paged engineering time across multiple teams.

A single one of these incidents avoided pays for the cost of Engram
many times over. Engram costs zero dollars to install (MIT, no telemetry,
no upsell). The cost is a one-time half-hour of platform-team time to
wire it into the agent's PreToolUse hook.

### Compliance and posture

| Property | Engram |
|---|---|
| Cloud egress | None — fully local |
| Data movement | None — your code and infra state stay on your laptop |
| API keys required | None — uses your existing AWS / kubectl creds via CLI shell-out |
| License | MIT |
| SOC 2 / GDPR / FedRAMP / ITAR | Compatible by architecture (no SaaS surface to audit) |
| Audit trail | Annotation table records every label / tag / memory write |

---

## Who Engram is for

- A platform / DevOps / SRE team using AI coding agents on
  infrastructure they care about not losing.
- A small to mid-sized engineering org that can't afford Sourcegraph
  Enterprise / Augment, or whose compliance posture forbids SaaS.
- An air-gapped or regulated environment (defense contractors, regulated
  finance, healthcare, government, EU-AI-Act-scoped products).
- A developer on a locked-down corporate laptop where adding any new
  external dependency requires committee approval — but where a local
  Python package via internal mirror is fine.

## Who Engram is NOT for

- A solo developer working in one repo with no infrastructure code.
  Cursor's built-in indexing is enough.
- A team already paying for Sourcegraph Amp or Augment Intent and happy
  with the cloud-SaaS posture.
- An org whose security team forbids any new tool, even a local one.
  In that case Engram's value is in the AGENTS.md / labels channels —
  but it's a sell-internally exercise that probably isn't worth it.

---

## The honest limits

Engram, today, does not:

- Understand custom resource definitions semantically. A `kind:
  ArgoCDApplication` is stored as `k8s:ArgoCDApplication`; Engram knows
  it's a distinct type but not what it does. The agent (the LLM)
  supplies the meaning.
- Parse Pulumi (Python/TypeScript imperative) or AWS CDK code. The
  workaround is `engram import-cloud` to capture the deployed state
  regardless of how it was authored.
- Evaluate Helm template interpolation (`{{ .Values.image }}`). Helm
  chart metadata is fully indexed; templates fall to a generic shallow
  resource if PyYAML can't parse them.
- Pull runtime metrics (CPU, memory, restart counts). Engram is
  structural; observability lives in Datadog / Grafana / CloudWatch.
- Cover GCP or Azure cloud-import. AWS is the only provider supported
  today. GCP and Azure are scoped for the next release — the pattern is
  established and adding each is ~400 lines.

We are explicit about these so that what Engram *does* claim is
trustworthy.

---

## The two-paragraph pitch (for LinkedIn cover-letter use)

I built Engram after the February 2026 incident where Anthropic's Claude
Code ran `terraform destroy` on a production database serving 100,000
students. The agent wasn't malicious — it just didn't know the database
was production. That's a context gap, not a model bug. The state of the
art in 2026 still doesn't fix it for the median engineering team: cloud
SaaS solutions are priced out of small platform shops and barred from
regulated industries, and local AI coding agents lack a primitive for
"should this destructive operation proceed."

Engram is the local-first context and safety layer that fills the gap.
It indexes every Kubernetes manifest, Terraform module, Helm chart,
Dockerfile, Jenkinsfile, docker-compose file, and `.env` across every
repo on a developer's laptop, and — for the resources that were
clicked into existence rather than written in code — it pulls the live
state from AWS or Kubernetes via the developer's own CLIs without ever
authenticating to a cloud itself. The result is a graph that any
MCP-compatible AI agent can query with a single tool call to learn the
blast radius of an operation before performing it. Three output
channels (MCP server, signed AGENTS.md markdown, and `engram.io/*`
labels written directly into K8s and Terraform source) ensure the tool
works in air-gapped, MDM-locked, and on-prem environments — exactly
the environments cloud-SaaS competitors cannot reach.

---

## How to evaluate Engram in five minutes

For a CTO / VP-of-Eng / technical decider:

1. **Read this brief.** (Done, presumably.)
2. **Read [`docs/REAL_WORLD_VALIDATION.md`](REAL_WORLD_VALIDATION.md)**
   for measured numbers from real OSS repositories.
3. **Read [`docs/BENCHMARKS.md`](BENCHMARKS.md)** for the assessment-
   scenario accuracy across six different DevOps repo shapes.
4. **Spin up the demo:** clone the repo, run `bash tests/full_demo.sh`.
   60 seconds. All output is verbatim from a real run — no scripted
   demos. The script's exit code is the integration test.
5. **One question for your platform lead:** *"If a Claude Code agent on
   your laptop got my okay to run `terraform destroy` on production
   tomorrow morning, what's the layer that stops it?"* If the answer
   is "a careful prompt," install Engram by lunchtime.

The repo is at [`github.com/Pulkit-debug/engram`](https://github.com/Pulkit-debug/engram).
The package is `engram-devops` on PyPI. License is MIT.

---

*Engram was built by a 2-year DevOps engineer on a ₹30,000 Windows
laptop, in four weekends, after watching one too many post-mortems
about AI agents that didn't know they were touching production.*

*If this brief is on your desk, the person who put it there is asking
you to read past the buzzwords and look at the primitive. The primitive
is `blast_radius(operation, target) → (tier, action, reasons)`. Everything
else in this project is plumbing around that one function.*
