# Pitch material

Everything in this file is **DRAFT — review before posting**. Replace
`<your-github-handle>`, `<your-email>`, screenshot placeholders, and any
language that doesn't sound like you before sending.

---

## Show HN draft (300 words)

**Title (≤80 chars):**

> Show HN: Engram – Local-first DevOps context layer that would have prevented the DataTalks.Club terraform-destroy incident

**Body:**

In February 2026, Claude Code ran `terraform destroy` on the production
infrastructure of DataTalks.Club, a learning platform serving 100,000
students. The agent wasn't malicious — it just had no concept of "this
is production." In April, an agent at PocketOS deleted a production volume
in 9 seconds. OWASP added "context poisoning" to its top-10 agentic risks
for 2026.

Engram is the local-first MCP server that fills the gap. It indexes every
Kubernetes manifest, Terraform module, Helm chart, Dockerfile, Jenkinsfile,
docker-compose file, and `.env` across every repo on your machine, builds
a cross-repo graph, and exposes 10 MCP tools to any agent that speaks
the protocol.

The one tool that matters: `blast_radius(operation, target)`. Before any
destructive operation, the agent calls this and gets back a structured
risk assessment — `red/orange/green`, dependents found, environment
inferred, recent incidents matching the target. If `red`, the
recommendation is `block`. If the agent (or you) had this hooked up on
February 26, it would have refused.

Three output channels (not just MCP): emit signed AGENTS.md sections that
any agent reads pre-session, *or* annotate K8s manifests and Terraform
files with `engram.io/*` labels that *every* agent sees inline via
`kubectl describe`. Works air-gapped. Works on locked-down corporate
laptops where you can't even install an MCP server.

**Works for click-ops too.** Most prod resources aren't in IaC — they were
created in the AWS console or `kubectl apply`-d once. Engram doesn't talk
to your cloud (no API keys, ever), but it parses the JSON output of *your*
`aws` and `kubectl` CLIs. Discovered resources land in the same graph as
IaC-defined ones, with the same `blast_radius` check.

100% local. SQLite + sqlite-vec. No cloud, no API keys, ~4,000 lines of
Python. Built by a 2-year DevOps engineer on a ₹30,000 (~$360) Windows
laptop.

GitHub: https://github.com/<your-github-handle>/engram
Install: `pipx install engram-devops && engram mcp install --target claude-code`

Happy to answer questions.

---

## 100-word cold email to a CTO

**Subject (≤72 chars):**

> Stops your AI agents from running `terraform destroy` on prod (local-first, MIT)

**Body:**

Hi [Name],

Engram is the local-first DevOps context layer I built for AI coding agents
after the DataTalks.Club / Claude Code terraform-destroy incident in February.
It indexes your infra-as-code across all repos, then exposes a
`blast_radius` MCP tool that any agent calls before destructive operations.

Three channels — MCP, AGENTS.md, K8s/Terraform labels — so it works in
air-gapped and MDM-locked environments too.

100% local. MIT-licensed. ~3,200 lines of Python. Indexed cert-manager
(1,217 files) in 6.4s on my laptop.

I'd value 10 minutes of your platform team's feedback. Repo:
https://github.com/<your-github-handle>/engram

— [Your name], [Your role]

---

## 5-tweet thread

**1/**

In Feb 2026, Claude Code ran `terraform destroy` on a learning
platform's prod database. 100k students. No malice — the agent just
had no concept of "this is production."

I built the thing that would have stopped it. 🧵

**2/**

Meet Engram — local-first MCP server that maps your DevOps codebases
(K8s, Terraform, Helm, Docker, Jenkins, env files) across every repo
on your laptop, and gives any AI agent a `blast_radius` check before
destructive operations.

[screenshot: terminal showing STOP/BLOCK/red]

**3/**

It's not just MCP. Three output channels:

🟦 MCP tools — for Claude Code / Cursor / Cline
📝 AGENTS.md sections — for air-gapped / MDM-locked setups
🏷️ engram.io/* labels on K8s + Terraform — works for *any* agent that
reads `kubectl describe`, no install needed on the agent side.

**4/**

100% local. Plain SQLite. Zero cloud. Indexed cert-manager (1,217 files)
in 6.4s on a ₹30,000 Windows laptop. Sqlite-vec for embeddings.
Tree-sitter for code. 64+ tests passing on Linux + Windows.

**5/**

`pipx install engram-devops && engram mcp install --target claude-code`

That's the install. MIT-licensed. Built solo in ~4 weeks.

Repo + docs: https://github.com/<your-github-handle>/engram

Feedback / issues / PRs welcome. 🙏

---

## 3-slide deck outline (for live demos)

### Slide 1 — The problem (15s)

- Title: **AI agents are blowing up production infrastructure**
- Bullets:
  - DataTalks.Club: terraform destroy on prod RDS (Feb 2026, 100k users)
  - PocketOS: 9 seconds to delete a prod volume (Apr 2026)
  - Meta SEV1: agent exceeded scope, 2h blast radius (Mar 2026)
  - 60% of orgs can't terminate a misbehaving agent
- Visual: timeline graphic

### Slide 2 — The wedge (20s)

- Title: **What Engram is**
- Three columns:
  - **Cross-repo context** — every K8s/Terraform/Helm/Docker/Jenkins/env file, one graph
  - **Blast-radius primitive** — pre-flight check before destructive ops
  - **Three output channels** — MCP + AGENTS.md + infra labels
- Visual: the architecture diagram from `docs/ARCHITECTURE.md`

### Slide 3 — Live demo (60s)

- Title: **30 seconds of `engram assess "terraform destroy" "datatalks_prod_db"`**
- The terminal output: STOP / BLOCK / red, 3 dependents, production
- Then the same command after `--force`: still surfaces the warning, but
  proceeds (audit-logged)
- Closing: `pipx install engram-devops && engram mcp install --target claude-code`
- Repo URL + "MIT-licensed, ~3,200 LOC, contributions welcome"

---

## Screenshot checklist (for the user to capture before posting)

To paste into Show HN / Twitter / cold-email replies:

- [ ] `engram assess "terraform destroy" "datatalks_prod_db"` showing BLOCK/red
- [ ] `engram status` table after indexing a real repo (cert-manager works well)
- [ ] `engram emit-agents-md` followed by `head -40 AGENTS.md` showing the
      auto-generated section with engram markers
- [ ] `engram label --dry-run` showing the planned changes + skipped
      untaggable resources
- [ ] `engram mcp install --target all` showing all four targets installed
- [ ] One real Claude Code chat where the agent calls `blast_radius` and
      surfaces the response (best capture: zoom-in of the tool-call panel)

---

## Outreach hit list (specific humans to email)

In rough priority order. Each is ~3-line ask with the install one-liner.

1. **Alexey Grigorev** (DataTalks.Club founder, lived through the incident).
   Pitch: "your incident is the canonical example in my README, I'd love
   your eyes on whether it would have actually prevented the destroy."
2. **Simon Willison** (writes constantly about MCP + lethal trifecta).
   Pitch: "local-first mitigation of the lethal trifecta for DevOps
   agents, three channels (MCP / AGENTS.md / metadata labels)".
3. **Anthropic developer relations.** Pitch: MCP server submission +
   "if you have a PreToolUse hook template for destructive bash, would
   love to include it as a recipe."
4. **HashiCorp Terraform-MCP team.** Pitch: complementary, not competitive.
   "Your MCP server *does* things; Engram tells it what it's about to do."
5. **Continue.dev / Coder.com.** Air-gapped angle — they own that market.
6. **Augment Code.** Their "Intent" feature is the closest cousin; we
   sit one layer below as the canonical infra context.
