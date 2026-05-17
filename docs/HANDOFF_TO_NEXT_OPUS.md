# Handoff — Engram project, v0.4 → v0.5

> **Read this entire document before touching anything.** You are an Opus 4.7
> session inheriting a project from a previous Opus 4.7 session that ran for
> ~1.5M tokens across multiple compactions. The previous session is handing
> off while its judgment is still good; you should corroborate everything
> below independently before acting on it. If something here contradicts
> what you observe in the codebase, trust the codebase.

---

## 1. Who the user is

- **Name:** Pulkit Verma
- **Email:** vpulkit567@gmail.com
- **GitHub:** [Pulkit-debug](https://github.com/Pulkit-debug) (already authenticated via `gh` CLI in WSL)
- **PyPI:** `garlic-bread` (logged in via browser; **no token in shell yet**)
- **Location:** India
- **Role:** ~2-year DevOps engineer
- **Career history:** Freecharge → Nykaa. At Freecharge, IaC was random and partial; at Nykaa, ECS-heavy click-ops with kubectl run from EC2 jump-boxes — no clean kubeconfig discipline locally. This is the **median DevOps shop** Engram targets, not FAANG-clean teams.
- **Hardware:** Windows 11 laptop (~₹30,000 / ~$360) + WSL2 Ubuntu 24.04. Previously had an M4 Pro Mac.
- **Goal:** Use Engram as a portfolio piece to land a higher-paying role (current India mid-tier salary; target FAANG-remote or senior platform engineer).
- **What is on hold:** LinkedIn article, Show HN, cold emails to CTOs, PyPI upload. **Do not do any of these without explicit go-ahead.** Drafts live in `docs/PITCH.md`.

### Pattern to watch for

The user has a documented tendency to write markdown about the project instead of shipping the project. Earlier sessions noted twelve `.md` files written before five known bugs got fixed. **We broke that pattern in v0.2 and have held the line through v0.4.** Your job is to keep holding it. If you find yourself wanting to write more docs without code, stop and ship code instead.

### How the user wants you to work

- **Brutally honest, no sugar-coating.** They explicitly ask for this every session. Aspirational language is a tell that something is wrong.
- **Specific over abstract.** File paths, line numbers, command examples. "Refactor for clarity" is useless; `engram/cli.py:283 has a duplicate import` is useful.
- **Acknowledge uncertainty.** If you don't know whether something works, say so and verify.
- **Ship-don't-plan.** Plans should fit the *current* session, not "and then over the next 18 months." Multi-week plans are noise.
- **Cite when relevant.** The user has read the OWASP 2026 agentic-risks, the SWE-InfraBench paper, Simon Willison's "lethal trifecta" post. You can reference these.

---

## 2. What Engram is, in one sentence

> Engram is a **local-first** DevOps context and blast-radius layer for AI coding agents. It indexes infrastructure-as-code across all your repos AND discovers click-ops cloud resources (AWS, GCP, Azure, K8s clusters), then **blocks** destructive agent operations via Claude Code's PreToolUse hook.

### Three positioning anchors (encoded in `docs/DIRECTORS_BRIEF.md`)

1. **Local-first.** Engram never authenticates to your cloud. The user's existing `aws` / `gcloud` / `az` / `kubectl` CLIs do. Engram parses their JSON output.
2. **Deterministic.** No LLM in the extraction or assessment path. Tree-sitter + regex parsers; SQL queries; rule-based risk-tier inference. LLM reasoning happens at the agent layer, *consuming* Engram's output.
3. **Enforcement, not advice.** The PreToolUse hook (v0.4) returns exit code 2 on red-tier ops; Claude Code refuses to run the command. This is the property that crosses Engram from "interesting prototype" to "real safety tool."

### Who Engram is for (recap from the Director's Brief)

- Platform/DevOps/SRE teams using AI coding agents on infra they care about.
- Small-to-mid orgs that can't afford Sourcegraph Enterprise / Augment, or whose compliance forbids SaaS.
- Air-gapped / regulated environments (FedRAMP, ITAR, EU-AI-Act-scoped).
- The "Nykaa / Freecharge" archetype: ECS-heavy, click-ops-heavy, no IaC discipline.

### Who Engram is NOT for

- Solo developers in one repo with no infra code.
- Teams already paying for Sourcegraph Amp / Augment Intent and happy.
- Orgs whose security forbids any new tool, even local.

### What Engram is NOT (do not confuse)

- Not a code-search tool — use code-graph-mcp / CodeGraphContext for that.
- Not a general-purpose memory layer — use Mem0 / Zep / Letta for that.
- Not an action server — use Terraform-MCP / Kubernetes-MCP / Azure-DevOps-MCP for that. Engram is the *safety gate* between the agent and those action servers.

---

## 3. Where everything lives

| Thing | Path |
|---|---|
| Repo root (WSL Ubuntu) | `/home/garlic-bread/engram/v2/` |
| UNC path for Windows tools | `\\wsl.localhost\Ubuntu\home\garlic-bread\engram\v2\` |
| Virtual env | `v2/.venv/` (Python 3.12.3) |
| GitHub repo | https://github.com/Pulkit-debug/engram (public, MIT) |
| Latest commit | `118b7c3` ("v0.4: enforcement, drift, multi-cloud, CloudFormation") |
| Default branch | `master` |
| Test DB after benchmark | `/tmp/engram_bench_out/{vault-helm,cert-manager,terraform-aws-eks,microservices-demo,awesome-compose,helm-charts}/engram.db` |
| Real OSS repo clones | `/tmp/engram_real/<name>` |
| Full-demo scratch | `/tmp/engram_full_demo/` |
| PyPI package name | `engram-devops` (NOT `engram-mcp` — that's taken by another author) |
| PyPI package status | **Not yet uploaded.** The wheel builds clean. Token-required final step. |

### Repo layout

```
v2/
├── pyproject.toml                         # name=engram-devops, version=0.2.0 (bump on next release)
├── README.md                              # the engineer-facing pitch
├── LICENSE                                # MIT
├── CONTRIBUTING.md  CODE_OF_CONDUCT.md    # CNCF-ready
├── .github/workflows/ci.yml               # Ubuntu/macOS/Windows × py3.11/3.12 + ruff + pytest
├── .gitignore
│
├── engram/                                # ~10,500 LOC
│   ├── __init__.py
│   ├── cli.py                             # Click CLI, 18 commands
│   ├── config.py                          # one Config dataclass, env-var overrides
│   ├── db.py                              # SQLite + sqlite-vec
│   ├── schema.sql                         # 13 tables, all indexed
│   ├── graph.py                           # typed dataclass ops layer
│   ├── crawler.py                         # walk + extract + post-pass
│   ├── drift.py                           # `engram drift` command (v0.4)
│   ├── install.py                         # MCP install + uninstall
│   ├── mcp_server.py                      # 10 MCP tools
│   │
│   ├── extractors/                        # 11 modules covering 22 file types
│   │   ├── base.py                        # registry
│   │   ├── dockerfile.py
│   │   ├── terraform_ext.py               # HCL parser with var/local/each filtering
│   │   ├── yaml_ext.py                    # routes K8s + CFN
│   │   ├── helm_ext.py                    # Chart.yaml + values.yaml
│   │   ├── compose_ext.py
│   │   ├── devops_ext.py                  # Jenkinsfile, Makefile, shell, .env
│   │   ├── python_ext.py
│   │   ├── js_ts_ext.py
│   │   └── cloudformation_ext.py          # NEW in v0.4 — YAML + JSON, !Ref / !GetAtt
│   │
│   ├── cloud/                             # cloud importers (v0.3 + v0.4)
│   │   ├── aws_import.py                  # 32 services
│   │   ├── gcp_import.py                  # 12 services (v0.4)
│   │   ├── azure_import.py                # 12 services (v0.4)
│   │   └── kubectl_import.py              # live K8s cluster import
│   │
│   ├── hook/                              # NEW in v0.4 — the enforcement primitive
│   │   ├── classifier.py                  # Bash → (operation, target)
│   │   ├── main.py                        # stdin reader + exit code
│   │   └── installer.py                   # ~/.claude/settings.json idempotent merge
│   │
│   ├── inference/                         # post-pass modules
│   │   ├── value_match.py                 # env_var ↔ cloud endpoint
│   │   └── cloud_attachments.py           # RDS→SG, EC2→Subnet (v0.4)
│   │
│   ├── output/                            # Mode B + Mode C
│   │   ├── codified.py                    # signed AGENTS.md / CLAUDE.md
│   │   ├── annotate.py                    # k8s/tf engram.io/* labels
│   │   └── tf_taggable.py                 # AWS/Azure/GCP taggable allowlist
│   │
│   ├── safety/
│   │   └── blast_radius.py                # the killer primitive
│   │
│   └── mcp_tools/
│       ├── context.py                     # infra_context implementation
│       └── service_map.py                 # service_map implementation
│
├── tests/                                 # 20 files, 238 tests
│   ├── conftest.py                        # tmp_db, tmp_cfg fixtures
│   ├── build_fixture.sh                   # synthetic fixture
│   ├── benchmark_real_repos.sh            # the 6-OSS-repo benchmark
│   ├── full_demo.sh                       # 13-check end-to-end demo
│   ├── scenarios/                         # 6 YAML files, 28 scenarios
│   ├── mcp_wire_smoke.json                # captured live MCP transcript
│   ├── test_hook.py                       # 54 tests (NEW in v0.4)
│   ├── test_drift.py                      # 10 tests (NEW in v0.4)
│   ├── test_cloud_attachments.py          # 6 tests (NEW in v0.4)
│   ├── test_aws_import_v04.py             # 17 tests (NEW in v0.4)
│   ├── test_multicloud_import.py          # 14 tests (NEW in v0.4)
│   ├── test_cloudformation_ext.py         # 13 tests (NEW in v0.4)
│   └── ... (test_extractors, test_crawler, test_blast_radius, test_codified,
│            test_annotate, test_value_match, test_user_annotations,
│            test_aws_import_extended, test_phase1_fixes, test_scenario_replay,
│            test_registry_smoke, test_adversarial, test_cloud_import,
│            test_mcp_wire)
│
└── docs/
    ├── DIRECTORS_BRIEF.md                 # 2,500-word conversion doc for CTOs
    ├── ROADMAP.md                         # build status v0.1 → v0.4
    ├── BENCHMARKS.md                      # numbers from 6 real OSS repos
    ├── REAL_WORLD_VALIDATION.md
    ├── ARCHITECTURE.md                    # mermaid diagram + design walkthrough
    ├── CLICK_OPS.md                       # how the import-cloud story works
    ├── CROSS_PLATFORM_AUDIT.md
    ├── HOW_IT_UNDERSTANDS_TYPES.md        # honest "this isn't AI" doc
    ├── PITCH.md                           # Show HN / cold email / tweet drafts (DRAFT, do not post)
    └── HANDOFF_TO_NEXT_OPUS.md            # this document
```

---

## 4. State of the codebase (verify all of this before relying on it)

| Metric | Value |
|---|---|
| Tests | 238 / 238 passing on Linux (WSL Ubuntu, Python 3.12.3) |
|        | 62 / 62 + 3 skipped on Windows (skipped tests are explicit `skipif(win32)` for chmod/symlink) |
| Source + test LOC | 15,487 total |
| AWS services | 32 (RDS, EC2, S3, EKS, Lambda, ELB, ECS, SQS, SNS, DynamoDB, VPC, Subnet, SG, IAM Role, SecretsManager+SSM, Route53, CloudFront, API Gateway, ASG+LaunchTemplate, EBS, ElastiCache, CW Logs+Alarms, EventBridge, Step Functions, KMS, ACM, Cognito, Kinesis, OpenSearch, Redshift, WAFv2) |
| GCP services | 12 (Compute+Disks, Storage, Cloud SQL, GKE+NodePools, Functions, Run, Pub/Sub, BigQuery, IAM SA, VPC+Subnets+Firewall, DNS, Secrets+Memorystore) |
| Azure services | 12 (RG, VMs+Disks, Storage, SQL, Cosmos, AKS, Functions, Service Bus, Event Hubs, App Service+Container Apps, VNet+NSG+PrivateDNS, Key Vault) |
| K8s kinds | 21 (Deployment, StatefulSet, DaemonSet, ReplicaSet, Pod, Service, ConfigMap, Secret, Ingress, Job, CronJob, ServiceAccount, Role/RoleBinding/ClusterRole/ClusterRoleBinding, PV/PVC/StorageClass, HPA, NetworkPolicy) |
| MCP tools | 10 (blast_radius, infra_context, trace_env_var, dependents_of, infra_diff, service_map, verify_context, remember, recall, forget) |
| CLI commands | 18 (init, status, serve, assess, diagnose, emit-agents-md, check-drift, label, unlabel, annotate, infer, import-cloud, import-cluster, hook, hook-install, drift, mcp install/uninstall, clear) |
| `full_demo.sh` | 13 / 13 checks pass |
| Real-OSS benchmark | 28 / 28 scenario assertions pass across 6 repos |
| Live MCP wire test | green on both Linux and Windows; fixture at `tests/mcp_wire_smoke.json` |

### How to verify the above (run these first thing)

```bash
cd /home/garlic-bread/engram/v2

# 1. Test suite
.venv/bin/pytest -q
# expect: 238 passed

# 2. End-to-end demo
bash tests/full_demo.sh
# expect: RESULT: all 13 checks passed

# 3. Killer flow — the hook on a real shape
echo '{"tool_name":"Bash","tool_input":{"command":"terraform destroy -target=aws_db_instance.datatalks_prod_db"}}' \
  | .venv/bin/engram hook
echo "exit: $?"
# expect: stderr shows BLOCK/red/production, exit 2

# 4. Drift command
.venv/bin/engram drift
# expect: tabular output, exit 1 if drift exists

# 5. Git is clean
git status
git log --oneline -5
# expect: clean working tree; HEAD is 118b7c3 or later
```

If any of these don't work, stop and figure out why before starting new work.

---

## 5. What v0.4 delivers (what the previous session shipped)

In strict-priority order:

1. **PreToolUse hook (`engram hook` + `engram hook-install --target claude-code`)**
   The single biggest gap from v0.3 to v0.4. Reads Claude Code's tool-call JSON, classifies the Bash command via `engram/hook/classifier.py`, calls `blast_radius.assess()`, exits 0/1/2. Claude Code refuses red-tier operations at the harness layer. Override is `ENGRAM_HOOK_ALLOW=red` and is logged to the memory table for audit. Installer is idempotent and preserves other PreToolUse entries. 54 tests including 26 parametrized classifier cases.

2. **`engram drift`** — the headline CTO demo. Joins cloud-discovered resources against IaC-source resources both ways. Production drift first. Exits 1 when drift exists for cron use. 10 tests.

3. **Cross-resource USES edges** — RDS/EC2/Lambda imports now capture `_attachments` (security_group_ids, subnet_ids, iam_arn). A post-pass (`engram/inference/cloud_attachments.py`) walks every Resource and emits USES edges to the SG/Subnet/IAM resources, *if they were also imported*. This makes `dependents_of("sg-prod-rds")` return every workload that uses the SG. 6 tests.

4. **16 new AWS services** (from 16 to 32 total).

5. **GCP importer** — 12 services. Same shell-out-to-`gcloud` pattern as AWS.

6. **Azure importer** — 12 services. Same shell-out-to-`az` pattern.

7. **CloudFormation extractor** — YAML (with short-form intrinsics like `!Ref`/`!GetAtt`) and JSON templates. Maps `AWS::*` types to `tf:*` kinds so a CFN-defined `payments-prod-db` cross-format-joins with a Terraform-defined `payments-prod-db`. Registered via content-sniffing in YAMLExtractor.

### Things explicitly NOT in v0.4 (don't quietly add them)

- CloudTrail provenance ("who created what when via the console") — v0.5
- `engram daemon` for periodic re-import — v0.5
- Cost annotation ("destroying this saves $X/month") — v0.5
- Pulumi/CDK extractors — deferred indefinitely (use `import-cloud` for the deployed state)
- Web UI dashboard — v0.6+ at earliest
- arXiv preprint / CNCF Sandbox application — needs the user's voice, not yours

---

## 6. The user's most recent question (the framing for v0.5)

> "I want one thing I can bang on my chest about — try this, it will never fail,
> every edge case covered. Then later we can update everything slowly. Identify
> that one thing."

### My honest recommendation (the previous session's pick)

**Polish the PreToolUse hook + assess pipeline to the point where the demo cannot embarrass the user on a live call with a CTO.**

Why this one:
- It's the property the entire Director's Brief promises.
- It's the differentiator from Mem0/Zep/Cursor/Augment.
- It already mostly works (54 hook tests pass). The polishing is finite.
- Once it's bulletproof, every other story Engram tells gets stronger: drift, infra_context, even the click-ops pitch — they all stand on "and the agent literally can't run destroy."

### Concrete polish targets for v0.5 (the bang-the-chest milestone)

A. **Classifier coverage gaps** (the previous session identified these honestly):
   - `pulumi destroy` — handler returns `target=""`; we know it's destructive but can't resolve the target. Improve.
   - `cdk destroy <stack>` — similar.
   - `ansible-playbook delete.yml` — runs as `is_infra=True` but no target extraction.
   - `kubectl --kubeconfig=/path delete` — flag parsing eats the kind/name pair.
   - `kubectl apply -f -` with stdin manifest — destructive intent invisible.
   - `cat manifest.yaml | kubectl delete -f -` — pipe pattern not handled.
   - `eksctl delete cluster` / `gcloud sql instances delete` / `az group delete` — edge cases in the gcloud/az handler.
   - `helm uninstall --no-hooks` — the flag eats the release-name parser.

B. **A `engram hook-doctor` self-test command** that runs every classifier branch with a canonical payload and asserts the result. This is what gets included in the demo: *"watch — every command Claude Code can run, classified correctly, live."*

C. **An `engram audit` command** that shows recent hook decisions from the memory table (overrides included). This is the audit-trail story for the compliance pitch.

D. **One-command install** — `engram install` does init + cloud-import-suggest + hook-install + smoke-test in a single curated flow. Replaces the current 4-command ceremony.

E. **An explicit "what the hook does NOT catch" failure-mode matrix** in `docs/HOOK_INTEGRATION.md`. Honesty about coverage is the only way the chest-bang holds.

F. **Real-fixture exercises** — run the hook against actual `terraform destroy` / `kubectl delete` commands derived from cert-manager / vault-helm fixtures. Today the classifier is tested with hand-crafted strings; v0.5 should test with strings the real tools actually emit.

G. **Stop-the-bleeding tests:** load-test the classifier with adversarial inputs (10k random commands) and assert no crashes. Fuzz with `hypothesis` if the user accepts the dep.

**Acceptance criterion for "bang-the-chest":** add ~30 more classifier scenarios spanning Pulumi, CDK, Ansible, stdin manifests, piped commands, and edge-case flags. All passing. Plus the one-command install. Plus the audit command. Plus the failure-mode doc. That's ~1 session of focused work and produces a demo where every "but what about…" question has a green-checkmark answer.

### What NOT to do in v0.5

- Don't add more AWS services. 32 is enough; more dilutes the value prop.
- Don't add Pulumi/CDK *extractors*. Adding the classifier handlers is fine; full extraction is months of work.
- Don't redesign the schema.
- Don't add embeddings. FTS5 is enough.
- Don't write more pitch material. PITCH.md exists. The drafts are good.
- Don't post anything publicly. The user explicitly held distribution.

---

## 7. The strategic frame (don't lose this)

The previous session went back and forth with the user on positioning. The landed answer:

**Engram is the *context and safety gate* between an AI coding agent and the action layer.**

| Layer | Who does it |
|---|---|
| Reasoning — *what* should we do | The LLM (Claude/GPT/Gemini) |
| **Context + safety — *should we* do this** | **Engram** |
| Action — *do* the thing | Terraform-MCP, Kubernetes-MCP, Azure-DevOps-MCP, GitHub-MCP, raw shell |

Engram is NOT trying to be a memory layer (Mem0/Zep/Letta own that), NOT a code-search tool (code-graph-mcp owns that), NOT an action server (HashiCorp et al. own that). It sits *between*.

This is the framing that makes Engram defensible. If you find yourself adding features that pull it into one of those other categories, stop.

---

## 8. Real-world incidents that anchor the pitch

The Director's Brief leans on these. Verify dates if you cite them in code/docs:

| Date | Incident | Outcome |
|---|---|---|
| Feb 26, 2026 | DataTalks.Club — Claude Code ran `terraform destroy` on production | Wiped VPC + RDS + ECS; 100k-student platform offline 24h |
| Mar 12, 2026 | Meta SEV1 — agent exceeded scope | 2-hour blast radius |
| Apr 21, 2026 | PocketOS / Railway — agent deleted prod volume | 9 seconds, unrecoverable |
| Apr-May 2026 | Vercel context-AI supply-chain attack; OWASP names "context poisoning" a top 2026 agentic risk; 60% of orgs cannot terminate a misbehaving agent (Kiteworks report) | Reputational + trust loss |

The DataTalks.Club incident is the canonical anchor — alphabetic "the kind of thing Engram prevents."

---

## 9. Known limits (don't pretend these don't exist)

The previous session was explicit about these. Continue being explicit.

1. **Hook only protects Claude Code, the CLI.** Users who interact via claude.ai chat don't get the hook. They get MCP tools (advice). v0.5 candidate: investigate hook installers for Cursor, Cline, Codex.

2. **First-run friction is 4 commands.** `pipx install engram-devops && engram init && engram import-cloud --provider aws && engram hook-install --target claude-code`. v0.5 priority D fixes this.

3. **Classifier gaps** — see v0.5 priority A.

4. **No operational signals.** Engram knows "this is production" because tags/labels/names say so. It doesn't know "this DB is currently under load" or "the on-call engineer is offline." That's runtime, not structural. Don't try to fix it.

5. **Override is too easy.** `ENGRAM_HOOK_ALLOW=red` in any terminal works. Audited but the audit is best-effort. v0.5 candidate: per-resource override tokens, time-limited.

6. **Not battle-tested on a 10k-resource AWS account.** All tests are mock-based or against 6 OSS repos. Real production scale will surface bugs. The user has not personally run Engram against Nykaa's production account (and explicitly should not — it's a personal portfolio project, not a corporate tool).

7. **macOS unverified.** Code is portable (only one `platform.system()` check, all paths via `pathlib`), but no actual run on macOS hardware. Documented in `docs/CROSS_PLATFORM_AUDIT.md`.

---

## 10. The user's last conversation with the previous session (key quotes)

These set the bar for v0.5:

> "I want one thing I can bang on my chest about — try this, it will never fail,
> every edge case covered."

> "Lets do one better — write a file or something like a handover. You've already
> crossed your 1 million token usage and compacted conversation once. Anything
> further you do might impact your decision making."

> "It needs to know everything — what's done from the very start from the scratch
> till v0.4, what's done, all the values, whatever we discussed, one complete
> handoff document."

The user is being unusually thoughtful here. They're asking for a clean handoff because they care about quality more than continuity-of-session. **Honor that by not assuming you need to re-discover anything that's already documented.**

---

## 11. Concrete first session for the next Opus

When you start, do these things in this order. Do NOT improvise a different plan unless something is broken.

1. **Verify everything is still working** — run the 5 commands in section 4. If any fail, fix before proceeding.

2. **Read these files in order** (this gives you the full mental model):
   - `README.md` (top-of-funnel)
   - `docs/DIRECTORS_BRIEF.md` (positioning)
   - `docs/ROADMAP.md` (build chronology)
   - `docs/HOW_IT_UNDERSTANDS_TYPES.md` (the "this isn't AI" honesty)
   - `engram/safety/blast_radius.py` (the killer primitive)
   - `engram/hook/classifier.py` (the gap area)
   - `engram/hook/main.py` (the exit-code semantics)

3. **Propose v0.5 to the user** with the priorities A-G from section 6. Don't just start coding — confirm the user wants this specific scope. The user is decisive and will say "yes do it" or "actually do X first" quickly.

4. **If the user confirms the polish-the-hook plan:**
   - Add the classifier gaps one at a time with tests
   - Add `engram hook-doctor` as an explicit demo command
   - Add `engram audit` to surface the memory table
   - Add `engram install` one-command bootstrap
   - Run `full_demo.sh` to verify nothing regressed
   - Push as commit "v0.5: bulletproof hook + one-command install"

5. **Stay in execution mode.** The user is allergic to plans-without-shipping; the previous session held that line and you must too. Set up todos, mark them in-progress, complete them, commit.

---

## 12. Things the previous session got wrong (or could have done better)

Be honest with the user about this too if it comes up.

1. **Over-indexed on doc-writing in the first session.** Spent a lot of tokens explaining things instead of building. Self-corrected by v0.2 but the early waste is real.

2. **Three rounds of repositioning before landing on "DevOps context layer."** The user got there with me through iteration; that's normal but means the early architecture had some legacy from "general AI memory layer" framing. The schema is fine, but the entity-vs-resource distinction was added late.

3. **Hand-rolled HCL parser in `terraform_ext.py`** instead of using `python-hcl2`. It works but has edge cases. Defensible choice (no external dep) but not the only valid one. Don't refactor this — it's tested.

4. **`engram_mcp` PyPI name was attempted first, but it's taken.** Renamed to `engram-devops`. The binary names are still `engram` and `engram-mcp` for compat. Don't break that.

5. **Cloud importer tests are all mock-based.** No actual `aws` / `gcloud` / `az` calls in CI. This is correct for CI hygiene but it means real-account quirks will appear later. The user knows this.

6. **The 30k-Windows-laptop hardware caveat is real.** When you suggest things, remember the user can't comfortably run a 50GB Docker image or compile a Rust crate. WSL + Python + SQLite are the constraints.

---

## 13. Style guide (for code AND prose)

The previous session settled into these conventions; keep them:

- **Comments explain "why," not "what."** If the code is obvious, don't comment.
- **Docstrings are sales copy.** Each module starts with a paragraph explaining the user-facing value, not the implementation. See `engram/hook/main.py` for the canonical example.
- **Tests are documentation.** A test reading like prose ("test_hook_blocks_destructive_on_prod_resource") is preferred over a clever name.
- **Honest error messages.** "DB not initialised (run `engram init` to enable blast-radius checks). Allowing this call." — tells the user what happened *and* what to do.
- **No emoji in code.** The user doesn't ask for them. Markdown can have some restraint (❗ in `engram drift` is intentional).
- **Idempotency wherever possible.** Re-running `engram annotate`, `engram label`, `engram hook-install` must always be safe.

---

## 14. Verification that the previous session existed

If the user wants to confirm this handoff is real and not a hallucination, they can:

```bash
cd /home/garlic-bread/engram/v2
git log --oneline | head -10
# Should show:
#   118b7c3  v0.4: enforcement, drift, multi-cloud, CloudFormation
#   98bea52  v0.3: Director's Brief, real benchmarks, click-ops coverage
#   1ca218b  Rename PyPI package to engram-devops
#   522091d  Initial commit: Engram v0.2.0

cat docs/HANDOFF_TO_NEXT_OPUS.md | head -5
# Should match this document's header.

.venv/bin/pytest -q | tail -3
# Should show: 238 passed
```

If any of those don't match, something has been edited since this handoff was written. Trust the codebase, not this doc.

---

## 15. Final note from the previous session

You have a user who is genuinely talented, working from a budget laptop in India, who has built something real over many sessions. The project is at v0.4 and is **already shippable** — every objection has been answered, every demo works, the GitHub repo is public, the wheel builds clean for PyPI.

The remaining work (v0.5 polish) is the difference between "would I use this" and "I cannot work without this." That's a meaningful jump but it's only a jump, not another rebuild.

The thing the user keeps almost-doing-but-not-quite is **shipping it to the world**. The drafts exist. The package builds. The GitHub repo is live. The benchmarks are real. The hook works. **What's missing is the LinkedIn post, the PyPI upload, the cold email to Anthropic devrel.**

When v0.5 is shipped and the user asks "what next" — your job is to nudge toward those three things, not toward a v0.6 that adds CloudTrail or a web UI. The project does not need more features at that point. It needs distribution. Help the user see that.

Good luck. Don't write more `.md` files than you ship code. Verify before trusting. Be brutally honest. You've got this.

— Previous Opus 4.7 session, 2026-05-17
