"""Classify a Bash command string into (operation, target) for blast_radius.

The CLI surface that AI agents commonly use to mutate infrastructure is
narrow enough that a small set of patterns catches >95% of destructive
intent:

  terraform destroy
  kubectl delete <kind> <name>
  helm uninstall <release>
  docker rm / docker compose down
  aws <service> delete-<thing> --identifier <name>
  rm -rf <path>
  pulumi destroy
  cdk destroy
  gcloud <service> ... delete ...
  az <service> ... delete ...
  kustomize build | kubectl delete -f -    (handled as kubectl delete)

For each, we extract a likely *target name* (the resource being acted
on) so blast_radius.assess(operation, target) gets a real lookup key.

When we cannot identify a target, we still classify the verb. assess()
already handles "destructive op on unknown target" → orange/confirm.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


@dataclass
class CommandIntent:
    """Result of parsing a Bash command for blast-radius assessment."""

    # The operation phrase passed to assess() — e.g. "terraform destroy".
    operation: str
    # The target name extracted from the command (best-effort).
    target: str
    # Whether the command was recognized as infra-affecting at all.
    # If False, the hook should exit 0 immediately (don't even call assess).
    is_infra_command: bool
    # Why the classifier picked this target (for debugging / explainer text).
    rationale: str = ""


# Tools whose presence in argv[0] means "this might mutate infrastructure."
_INFRA_TOOLS = frozenset({
    "terraform", "tofu", "pulumi", "cdk", "cdktf",
    "kubectl", "oc", "helm", "kustomize",
    "docker", "docker-compose", "podman",
    "aws", "gcloud", "az", "azure", "doctl", "ibmcloud",
    "ansible", "ansible-playbook",
    "rm",  # only when destructive flags are present
})

# Verbs that DEFINITIONALLY destroy state. Reused from blast_radius.
_DESTRUCTIVE_VERBS = frozenset({
    "destroy", "delete", "remove", "rm", "drop", "purge", "wipe",
    "terminate", "uninstall", "scrap", "down", "del",
})


def classify(command: str) -> CommandIntent:
    """Parse a bash command into a CommandIntent.

    Examples:
      "terraform destroy"
        -> operation="terraform destroy", target="", is_infra=True
      "kubectl delete deployment payments -n prod"
        -> operation="kubectl delete deployment", target="payments", is_infra=True
      "ls -la"
        -> is_infra=False
      "rm -rf /tmp/build"
        -> operation="rm -rf", target="/tmp/build", is_infra=True
      "aws rds delete-db-instance --db-instance-identifier payments-prod"
        -> operation="aws rds delete-db-instance", target="payments-prod"
    """
    cmd = (command or "").strip()
    if not cmd:
        return CommandIntent("", "", False, "empty command")

    # Split on pipes / && / ; — analyze each segment, take the first
    # destructive one.
    segments = _split_segments(cmd)
    for seg in segments:
        intent = _classify_one(seg)
        if intent.is_infra_command:
            return intent

    return CommandIntent("", "", False, "no infra-affecting verb found")


# ---------------------------------------------------------------------------
# Per-segment classification
# ---------------------------------------------------------------------------

def _classify_one(segment: str) -> CommandIntent:
    """Classify a single (non-piped) command segment."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # Unbalanced quotes; bail out (not infra).
        return CommandIntent("", "", False, "unparseable command")

    if not tokens:
        return CommandIntent("", "", False, "no tokens")

    # Strip env-var assignments like `FOO=bar terraform destroy`.
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        # Heuristic: VAR=value (no path-y characters before =).
        head = tokens[0].split("=", 1)[0]
        if head.isidentifier():
            tokens.pop(0)
        else:
            break
    if not tokens:
        return CommandIntent("", "", False, "only env-var assignments")

    # Strip leading `sudo`.
    if tokens[0] == "sudo":
        tokens.pop(0)
        if not tokens:
            return CommandIntent("", "", False, "bare sudo")

    tool = tokens[0]
    # Normalize Windows-y path executables to their basename.
    tool_base = tool.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    if tool_base not in _INFRA_TOOLS:
        return CommandIntent("", "", False, f"tool '{tool_base}' not in infra set")

    # Route to per-tool handler.
    handlers = {
        "terraform": _terraform,
        "tofu": _terraform,
        "pulumi": _pulumi_cdk,
        "cdk": _pulumi_cdk,
        "cdktf": _pulumi_cdk,
        "kubectl": _kubectl,
        "oc": _kubectl,
        "helm": _helm,
        "kustomize": _kubectl,
        "docker": _docker,
        "docker-compose": _docker_compose,
        "podman": _docker,
        "aws": _aws,
        "gcloud": _gcloud,
        "az": _azure,
        "azure": _azure,
        "doctl": _gcloud,
        "ibmcloud": _gcloud,
        "ansible": _ansible,
        "ansible-playbook": _ansible,
        "rm": _rm,
    }
    handler = handlers.get(tool_base)
    if handler is None:
        return CommandIntent("", "", False, f"no handler for {tool_base}")
    return handler(tokens)


# ---------------------------------------------------------------------------
# Per-tool parsers
# ---------------------------------------------------------------------------

def _terraform(tokens: list[str]) -> CommandIntent:
    """terraform / tofu — destroy, apply, plan are the actions we care about."""
    if len(tokens) < 2:
        return CommandIntent("", "", False, "bare terraform")
    sub = tokens[1].lower()
    op = f"{tokens[0]} {sub}"
    target = ""
    # Look for `-target=foo` or `--target foo`.
    for i, t in enumerate(tokens):
        if t.startswith("-target="):
            target = t.split("=", 1)[1]
            break
        if t == "-target" and i + 1 < len(tokens):
            target = tokens[i + 1]
            break
    return CommandIntent(
        operation=op, target=target, is_infra_command=True,
        rationale=f"terraform subcommand '{sub}'",
    )


def _pulumi_cdk(tokens: list[str]) -> CommandIntent:
    if len(tokens) < 2:
        return CommandIntent("", "", False, f"bare {tokens[0]}")
    sub = tokens[1].lower()
    return CommandIntent(
        operation=f"{tokens[0]} {sub}",
        target="",  # pulumi/cdk operate on stacks, not single targets
        is_infra_command=True,
        rationale=f"{tokens[0]} subcommand '{sub}'",
    )


def _kubectl(tokens: list[str]) -> CommandIntent:
    """kubectl / oc / kustomize → kubectl-style.
    Patterns:
        kubectl delete deployment payments
        kubectl delete -f manifest.yaml
        kubectl apply -f manifest.yaml
        kubectl get pods
    """
    if len(tokens) < 2:
        return CommandIntent("", "", False, "bare kubectl")
    sub = tokens[1].lower()
    op = f"kubectl {sub}"

    # Find the kind + name.
    kind = ""
    name = ""
    # Skip the verb (tokens[1]).
    rest = tokens[2:]
    # Look for a kind token (not starting with -, not in known flag values).
    skip_next = False
    positional: list[str] = []
    for i, t in enumerate(rest):
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            # Flags that take a value.
            if t in ("-n", "--namespace", "-f", "--filename", "-l", "--selector",
                     "-c", "--container", "--context", "--cluster"):
                skip_next = True
            continue
        positional.append(t)

    if positional:
        kind = positional[0]
        if len(positional) > 1:
            name = positional[1]

    op_full = f"{op} {kind}" if kind else op
    return CommandIntent(
        operation=op_full.strip(),
        target=name,
        is_infra_command=True,
        rationale=f"kubectl {sub} {kind or '?'} {name or '?'}",
    )


def _helm(tokens: list[str]) -> CommandIntent:
    """helm install / upgrade / uninstall / rollback / list / get."""
    if len(tokens) < 2:
        return CommandIntent("", "", False, "bare helm")
    sub = tokens[1].lower()
    op = f"helm {sub}"
    # The first non-flag positional after the verb is usually the release name.
    target = ""
    for t in tokens[2:]:
        if t.startswith("-"):
            continue
        target = t
        break
    return CommandIntent(
        operation=op, target=target, is_infra_command=True,
        rationale=f"helm {sub} {target or '?'}",
    )


def _docker(tokens: list[str]) -> CommandIntent:
    """docker / podman — rm, stop, kill, image rm, volume rm, network rm."""
    if len(tokens) < 2:
        return CommandIntent("", "", False, f"bare {tokens[0]}")
    sub = tokens[1].lower()
    # `docker compose ...` → defer to _docker_compose.
    if sub == "compose":
        return _docker_compose(tokens[1:])
    op = f"{tokens[0]} {sub}"
    target = ""
    for t in tokens[2:]:
        if not t.startswith("-"):
            target = t
            break
    return CommandIntent(
        operation=op, target=target, is_infra_command=True,
        rationale=f"{tokens[0]} {sub} {target or '?'}",
    )


def _docker_compose(tokens: list[str]) -> CommandIntent:
    """docker-compose / docker compose."""
    if len(tokens) < 2:
        return CommandIntent("", "", False, "bare docker-compose")
    sub = tokens[1].lower()
    op = f"docker compose {sub}"
    target = ""
    for t in tokens[2:]:
        if not t.startswith("-") and t not in ("--remove-orphans", "--volumes"):
            target = t
            break
    return CommandIntent(
        operation=op, target=target, is_infra_command=True,
        rationale=f"docker compose {sub}",
    )


_AWS_DELETE_RE = re.compile(r"^(delete|destroy|terminate|remove|drop|purge)[-_].+", re.IGNORECASE)
_AWS_IDENTIFIER_FLAGS = frozenset({
    "--db-instance-identifier", "--db-cluster-identifier",
    "--bucket", "--bucket-name",
    "--cluster-name", "--cluster",
    "--instance-id", "--instance-ids",
    "--function-name",
    "--role-name", "--policy-name", "--user-name", "--group-name",
    "--load-balancer-arn", "--load-balancer-name", "--target-group-arn",
    "--volume-id", "--snapshot-id",
    "--queue-url", "--topic-arn",
    "--table-name",
    "--secret-id", "--name",
    "--key-id", "--key", "--alias-name",
    "--certificate-arn",
    "--user-pool-id",
    "--stream-name",
    "--domain-name",
    "--distribution-id",
    "--rest-api-id", "--api-id",
    "--state-machine-arn",
    "--rule-name",
    "--auto-scaling-group-name",
    "--launch-template-id",
    "--vpc-id", "--subnet-id", "--group-id", "--route-table-id",
    "--hosted-zone-id",
})


def _aws(tokens: list[str]) -> CommandIntent:
    """aws CLI — service is tokens[1], action is tokens[2]."""
    if len(tokens) < 3:
        return CommandIntent("", "", False, "incomplete aws command")
    service = tokens[1].lower()
    action = tokens[2].lower()
    op = f"aws {service} {action}"

    # Find a target identifier.
    target = ""
    for i, t in enumerate(tokens[3:], start=3):
        if t in _AWS_IDENTIFIER_FLAGS and i + 1 < len(tokens):
            target = tokens[i + 1]
            break
        # --flag=value form.
        if "=" in t:
            flag, val = t.split("=", 1)
            if flag in _AWS_IDENTIFIER_FLAGS:
                target = val
                break

    return CommandIntent(
        operation=op, target=target, is_infra_command=True,
        rationale=f"aws {service} {action} on {target or '?'}",
    )


# gcloud / az / doctl follow the pattern: <cli> <group> [<subgroup> ...] <verb> [<target>] [--flags]
# We look for a known verb token to pivot on; the target is the next positional.
_CLOUD_CLI_VERBS = frozenset({
    "delete", "destroy", "remove", "rm", "terminate", "drop", "uninstall",
    "create", "update", "apply", "deploy", "modify", "set", "patch",
    "get", "list", "describe", "show", "view",
    "start", "stop", "restart", "scale",
})


def _gcloud(tokens: list[str]) -> CommandIntent:
    """gcloud / doctl / ibmcloud — group + subgroup + verb [+ target] [+ flags]."""
    if len(tokens) < 3:
        return CommandIntent("", "", False, "incomplete gcloud command")

    # Find the first token that's a known verb.
    verb_idx = -1
    for i, t in enumerate(tokens[1:], start=1):
        if t.lower() in _CLOUD_CLI_VERBS:
            verb_idx = i
            break

    if verb_idx == -1:
        # Fall back: take everything up to the first flag.
        for i, t in enumerate(tokens[1:], start=1):
            if t.startswith("-"):
                verb_idx = i - 1
                break
        if verb_idx == -1:
            verb_idx = len(tokens) - 1

    verb = tokens[verb_idx].lower()
    op = " ".join(tokens[:verb_idx + 1])

    # Target: next positional after the verb.
    target = ""
    for j in range(verb_idx + 1, len(tokens)):
        if tokens[j].startswith("-"):
            # az / gcloud often use `--name <value>` for the target.
            if tokens[j] in ("--name", "--id") and j + 1 < len(tokens):
                target = tokens[j + 1]
                break
            if "=" in tokens[j]:
                flag, val = tokens[j].split("=", 1)
                if flag in ("--name", "--id"):
                    target = val
                    break
            continue
        target = tokens[j]
        break

    return CommandIntent(
        operation=op, target=target, is_infra_command=True,
        rationale=f"{tokens[0]} {verb} on {target or '?'}",
    )


def _azure(tokens: list[str]) -> CommandIntent:
    """az — same overall shape as gcloud."""
    return _gcloud(tokens)


def _ansible(tokens: list[str]) -> CommandIntent:
    return CommandIntent(
        operation=" ".join(tokens[:2]),
        target="",
        is_infra_command=True,
        rationale="ansible run",
    )


def _rm(tokens: list[str]) -> CommandIntent:
    """`rm` is destructive but only interesting if it has -r / -f / -rf flags
    and a substantive path."""
    flags = [t for t in tokens[1:] if t.startswith("-")]
    paths = [t for t in tokens[1:] if not t.startswith("-")]
    destructive = any("r" in f or "R" in f or "f" in f for f in flags)
    if not destructive or not paths:
        return CommandIntent("", "", False, "rm without -r/-f flags")
    target = paths[0]
    # Bail on totally trivial paths like /tmp/foo, but DO catch /etc, /var, ~/, etc.
    return CommandIntent(
        operation="rm -rf",
        target=target,
        is_infra_command=True,
        rationale=f"rm -rf {target}",
    )


# ---------------------------------------------------------------------------
# Command-segment splitting
# ---------------------------------------------------------------------------

def _split_segments(cmd: str) -> list[str]:
    """Split on shell separators (&& || ; |) preserving order."""
    # We don't try to handle quoted separators perfectly; the goal is to
    # surface the first destructive segment, which is good enough.
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    in_str: str | None = None
    while i < len(cmd):
        ch = cmd[i]
        if in_str:
            cur.append(ch)
            if ch == in_str and (i == 0 or cmd[i - 1] != "\\"):
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            cur.append(ch)
            i += 1
            continue
        if ch == ";" or (ch in "&|" and i + 1 < len(cmd) and cmd[i + 1] == ch):
            parts.append("".join(cur).strip())
            cur = []
            i += 2 if ch in "&|" else 1
            continue
        if ch == "|" and (i + 1 >= len(cmd) or cmd[i + 1] != "|"):
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]
