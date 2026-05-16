"""Infrastructure annotation — Mode C.

We write `engram.io/*` labels onto Kubernetes manifest files and
`engram.io/*` tags onto Terraform `tags = { ... }` blocks. The point:
once these labels exist in the source, ANY AI agent reading the file
sees Engram's context — without Engram being installed on its side.

Design rules:
  * Source-file writes only. We never touch live clusters. The agent's
    existing `kubectl apply` / `terraform apply` flow propagates the
    annotations to the real cluster.
  * Default to --dry-run. The user must pass --apply to actually write.
  * Every annotation logged to the `annotation` table for safe rollback.
  * The label prefix `engram.io/` is reserved and reversible.
  * We preserve formatting where possible (we use ruamel-style round-trip
    only if available; otherwise we string-edit minimally).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ENGRAM_LABEL_PREFIX = "engram.io/"
ENGRAM_LABEL_KEYS = (
    "engram.io/environment",
    "engram.io/risk-tier",
    "engram.io/blast-radius",
    "engram.io/managed-by",
    "engram.io/dependents",
)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class AnnotationOp:
    target_kind: str       # 'k8s' | 'terraform'
    target_path: str       # source file we'll edit
    target_id: str         # resource uid in graph
    labels: dict[str, str]
    rationale: str = ""


@dataclass
class AnnotationPlan:
    ops: list[AnnotationOp] = field(default_factory=list)
    # Resources we deliberately skipped, with reason. Surfaced in `engram label`.
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, file, reason)

    def summary(self) -> dict[str, int]:
        by_target: dict[str, int] = {}
        for op in self.ops:
            by_target[op.target_kind] = by_target.get(op.target_kind, 0) + 1
        return by_target


def _build_labels_for_resource(row: sqlite3.Row, dependent_count: int) -> dict[str, str]:
    env = (row["environment"] or "").strip()
    tier = (row["risk_tier"] or "green").strip()
    blast = "critical" if tier == "red" else ("elevated" if tier == "orange" else "low")
    out = {
        "engram.io/managed-by": "engram",
        "engram.io/risk-tier": tier,
        "engram.io/blast-radius": blast,
    }
    if env:
        out["engram.io/environment"] = env
    if dependent_count:
        out["engram.io/dependents"] = str(dependent_count)
    return out


def plan_annotations(
    conn: sqlite3.Connection,
    *,
    target_kind: str = "all",
    only_red: bool = False,
) -> AnnotationPlan:
    """Build a list of annotation ops to apply.

    Args:
        target_kind: 'k8s' | 'terraform' | 'all'
        only_red:    if True, skip green-tier resources
    """
    plan = AnnotationPlan()
    kinds: list[str] = []
    if target_kind in ("k8s", "all"):
        kinds.append("k8s")
    if target_kind in ("terraform", "all"):
        kinds.append("terraform")

    for kind in kinds:
        if kind == "k8s":
            rows = conn.execute(
                """
                SELECT uid, file_path, kind, name, namespace, environment, risk_tier
                FROM resource
                WHERE kind LIKE 'k8s:%'
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT uid, file_path, kind, name, namespace, environment, risk_tier
                FROM resource
                WHERE kind LIKE 'tf:%' AND kind NOT LIKE 'tf:data:%'
                """
            ).fetchall()

        for row in rows:
            if only_red and row["risk_tier"] != "red":
                continue
            # Terraform: check the taggable-resource allowlist before planning.
            if kind == "terraform":
                from engram.output.tf_taggable import is_taggable
                ok, reason = is_taggable(row["kind"])
                if not ok:
                    plan.skipped.append((row["kind"], row["file_path"], reason))
                    continue
            # Count 1-hop dependents for label payload.
            dep_count = conn.execute(
                "SELECT count(*) FROM edge WHERE dst_kind='resource' AND dst_id=? AND rel_type='DEPENDS_ON'",
                (row["uid"],),
            ).fetchone()[0]
            labels = _build_labels_for_resource(row, dep_count)
            plan.ops.append(AnnotationOp(
                target_kind=kind,
                target_path=row["file_path"],
                target_id=row["uid"],
                labels=labels,
                rationale=f"{row['kind']} {row['name']} "
                          f"({row['environment'] or 'env unknown'}, tier={row['risk_tier']})",
            ))
    return plan


# ---------------------------------------------------------------------------
# Apply (idempotent edits to source files)
# ---------------------------------------------------------------------------

def apply_plan(
    conn: sqlite3.Connection, plan: AnnotationPlan, *, dry_run: bool = True,
) -> dict:
    """Execute the plan (or simulate it if dry_run).

    Returns a dict: { 'files_changed': [...], 'noop': [...], 'errors': [...] }.
    """
    changed: list[str] = []
    noop: list[str] = []
    errors: list[tuple[str, str]] = []

    # Group ops by file so we open/save once per file.
    by_file: dict[str, list[AnnotationOp]] = {}
    for op in plan.ops:
        by_file.setdefault(op.target_path, []).append(op)

    now = datetime.now(timezone.utc).isoformat()

    for fp_str, ops in by_file.items():
        fp = Path(fp_str)
        if not fp.exists():
            errors.append((fp_str, "file no longer exists"))
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append((fp_str, f"read: {exc}"))
            continue

        if ops[0].target_kind == "k8s":
            new_content, file_changed = _apply_k8s_labels(content, ops)
        else:
            new_content, file_changed = _apply_terraform_tags(content, ops)

        if not file_changed:
            noop.append(fp_str)
            continue

        changed.append(fp_str)
        if dry_run:
            continue

        try:
            fp.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            errors.append((fp_str, f"write: {exc}"))
            continue

        # Log to annotation table for safe rollback.
        for op in ops:
            for k, v in op.labels.items():
                conn.execute(
                    """
                    INSERT INTO annotation(target_kind, target_id, label_key, label_value, applied_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(target_kind, target_id, label_key) DO UPDATE SET
                        label_value=excluded.label_value,
                        applied_at=excluded.applied_at
                    """,
                    (op.target_kind, op.target_id, k, v, now),
                )

    return {
        "dry_run": dry_run,
        "changed": changed,
        "noop": noop,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Kubernetes: insert labels into metadata.labels
# ---------------------------------------------------------------------------

_LABELS_KEY_RE = re.compile(r'^(\s+)labels\s*:\s*$', re.MULTILINE)


def _apply_k8s_labels(content: str, ops: list[AnnotationOp]) -> tuple[str, bool]:
    """Insert engram.io/* labels into each K8s document's metadata.labels.

    We do this string-style to preserve existing formatting (yaml.dump would
    reorder keys and strip comments). For each YAML document, we:
      1. Locate `metadata:` and within it `labels:`.
      2. If `labels:` exists, append new keys under it.
      3. If not, insert `labels:` under `metadata:`.
    Idempotency: existing engram.io/* keys are replaced, not duplicated.
    """
    # Combine all ops into a single label dict — multiple ops for the same file
    # would be unusual (one per K8s doc); we conservatively union them.
    labels: dict[str, str] = {}
    for op in ops:
        labels.update(op.labels)
    if not labels:
        return content, False

    # Split into docs.
    docs = list(yaml.safe_load_all(content))
    raw_docs = content.split("\n---\n") if "\n---\n" in content else [content]
    if len(docs) != len(raw_docs):
        # Mismatch — fall back to single-document handling on the whole text.
        raw_docs = [content]
        docs = [yaml.safe_load(content)]

    new_docs: list[str] = []
    changed = False
    for raw, doc in zip(raw_docs, docs):
        if not isinstance(doc, dict) or "metadata" not in doc:
            new_docs.append(raw)
            continue
        new_raw, doc_changed = _inject_k8s_labels(raw, labels)
        new_docs.append(new_raw)
        changed = changed or doc_changed

    sep = "\n---\n" if "\n---\n" in content else "\n"
    return sep.join(new_docs), changed


def _inject_k8s_labels(text: str, labels: dict[str, str]) -> tuple[str, bool]:
    """Inject `engram.io/*: <value>` lines under metadata.labels in one doc."""
    md_match = re.search(r'^metadata\s*:\s*$', text, re.MULTILINE)
    if not md_match:
        return text, False
    md_start = md_match.end()
    # Find indentation of children of metadata: by scanning the next non-blank line.
    rest = text[md_start:]
    indent_match = re.search(r'\n([ \t]+)\S', rest)
    if not indent_match:
        return text, False
    child_indent = indent_match.group(1)

    # Find labels: under metadata at child_indent.
    labels_re = re.compile(
        r'^' + re.escape(child_indent) + r'labels\s*:\s*$',
        re.MULTILINE,
    )
    lm = labels_re.search(text, md_start)
    if lm:
        # Insert under existing labels block.
        label_indent = child_indent + "  "
        insertion_pos = lm.end()
        # Skip past existing labels lines.
        cursor = insertion_pos
        existing_engram_keys: dict[str, tuple[int, int]] = {}
        while True:
            nl = text.find("\n", cursor)
            if nl == -1:
                break
            line_start = cursor + 1 if text[cursor] == "\n" else cursor
            next_line = text[nl + 1: text.find("\n", nl + 1) if text.find("\n", nl + 1) != -1 else len(text)]
            # Stop when indentation breaks.
            if next_line and not next_line.startswith(label_indent) and not next_line.startswith(child_indent + " "):
                # No longer under labels
                break
            line_end = text.find("\n", nl + 1)
            if line_end == -1:
                line_end = len(text)
            line_text = text[nl + 1:line_end]
            if line_text.strip().startswith("engram.io/"):
                # Track range so we can replace.
                key_match = re.match(r'\s*(engram\.io/[\w.-]+)\s*:', line_text)
                if key_match:
                    existing_engram_keys[key_match.group(1)] = (nl + 1, line_end)
            if not line_text.startswith(label_indent):
                break
            cursor = line_end

        # Replace existing engram keys.
        new_text = text
        offset = 0
        for k in sorted(existing_engram_keys.keys()):
            ls, le = existing_engram_keys[k]
            ls += offset
            le += offset
            new_line = f'{label_indent}{k}: "{labels.get(k, "")}"' if k in labels else None
            if new_line is None:
                continue
            old_line = new_text[ls:le]
            new_text = new_text[:ls] + new_line + new_text[le:]
            offset += len(new_line) - len(old_line)

        # Append new engram keys that didn't exist.
        to_append = [
            f'{label_indent}{k}: "{v}"' for k, v in sorted(labels.items())
            if k not in existing_engram_keys
        ]
        if to_append:
            append_at = lm.end() + offset
            # Insert AFTER the last existing label line. Walk forward to find it.
            scan = append_at
            while scan < len(new_text):
                nl = new_text.find("\n", scan + 1)
                if nl == -1:
                    nl = len(new_text)
                line = new_text[scan + 1:nl]
                if not line.startswith(label_indent):
                    break
                scan = nl
            insertion = "\n" + "\n".join(to_append)
            new_text = new_text[:scan] + insertion + new_text[scan:]
        return new_text, True

    # No labels: block — insert one right after metadata:.
    eol = text.find("\n", md_start)
    if eol == -1:
        eol = len(text)
    label_indent = child_indent + "  "
    new_block = (
        "\n" + child_indent + "labels:\n"
        + "\n".join(f'{label_indent}{k}: "{v}"' for k, v in sorted(labels.items()))
    )
    return text[:eol] + new_block + text[eol:], True


# ---------------------------------------------------------------------------
# Terraform: insert tags into the tags = {} block
# ---------------------------------------------------------------------------

_TF_RESOURCE_RE = re.compile(
    r'^(resource\s+"([^"]+)"\s+"([^"]+)"\s*\{)',
    re.MULTILINE,
)
_TF_TAGS_RE = re.compile(r'(tags\s*=\s*\{)([^}]*)(\})', re.DOTALL)


def _apply_terraform_tags(content: str, ops: list[AnnotationOp]) -> tuple[str, bool]:
    """For each `resource "type" "name" {}` mentioned in ops, inject engram tags.

    Idempotent: replaces existing engram.io/* tags rather than appending.
    """
    by_name: dict[str, dict[str, str]] = {}
    for op in ops:
        # The resource name was hashed into the uid; the simplest path is to
        # rely on the resource row passed via plan_annotations -> here. But ops
        # only have uid/target_id, not name. We re-query the file's content and
        # match against ALL resources defined here.
        pass

    # Strategy: walk every `resource "x" "y" {}` block in the file. For any
    # resource that maps to one of our ops' UIDs (we re-derive uid from the
    # block), inject the engram tags.
    from engram.graph import resource_uid

    file_path = ops[0].target_path
    op_by_uid = {op.target_id: op for op in ops}

    changed = False
    out: list[str] = []
    cursor = 0
    for m in _TF_RESOURCE_RE.finditer(content):
        tf_type, tf_name = m.group(2), m.group(3)
        block_start = m.end()
        block_body = _extract_balanced(content, block_start)
        if block_body is None:
            continue
        block_end = block_start + len(block_body) + 1  # +1 for the closing brace

        # Re-derive uid: (kind, name, namespace="", file_path)
        derived = resource_uid(f"tf:{tf_type}", tf_name, "", file_path)
        if derived not in op_by_uid:
            continue
        op = op_by_uid[derived]

        out.append(content[cursor:block_start])
        new_body, body_changed = _inject_tf_tags(block_body, op.labels)
        out.append(new_body)
        cursor = block_end - 1  # land on the closing }
        changed = changed or body_changed
    out.append(content[cursor:])
    return "".join(out) if changed else content, changed


def _extract_balanced(s: str, start: int) -> str | None:
    """Same as terraform_ext._extract_balanced_body — local copy to avoid import."""
    depth = 1
    i = start
    in_str: str | None = None
    while i < len(s):
        ch = s[i]
        if in_str:
            if ch == "\\" and i + 1 < len(s):
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == '"':
            in_str = ch
            i += 1
            continue
        if ch == "#" or (ch == "/" and i + 1 < len(s) and s[i + 1] == "/"):
            nl = s.find("\n", i)
            i = len(s) if nl == -1 else nl + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i]
        i += 1
    return None


def _inject_tf_tags(block_body: str, labels: dict[str, str]) -> tuple[str, bool]:
    """Insert engram.io/* tags into the body of a Terraform resource block."""
    tag_match = _TF_TAGS_RE.search(block_body)
    if tag_match:
        tag_body = tag_match.group(2)
        # Strip existing engram.io/* tags.
        stripped = re.sub(
            r'^\s*"engram\.io/[\w.-]+"\s*=\s*"[^"]*"\s*,?\s*$', "",
            tag_body, flags=re.MULTILINE,
        )
        stripped = stripped.rstrip()
        new_tags = "\n".join(f'    "{k}" = "{v}"' for k, v in sorted(labels.items()))
        merged = stripped + ("\n" if stripped else "") + new_tags + "\n  "
        new_block = (
            block_body[:tag_match.start()]
            + tag_match.group(1) + merged + tag_match.group(3)
            + block_body[tag_match.end():]
        )
        return new_block, True

    # No tags block — append one before the closing brace.
    new_tags = "\n  tags = {\n" + "\n".join(
        f'    "{k}" = "{v}"' for k, v in sorted(labels.items())
    ) + "\n  }\n"
    return block_body.rstrip() + new_tags, True


# ---------------------------------------------------------------------------
# Unlabel — full removal of engram.io/* from source files.
# ---------------------------------------------------------------------------

def unlabel_all(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict:
    """Remove every engram.io/* label/tag from every source file Engram has
    touched. Uses the `annotation` table as the source of truth.
    """
    rows = conn.execute(
        "SELECT DISTINCT target_kind, target_id FROM annotation"
    ).fetchall()
    if not rows:
        return {"dry_run": dry_run, "changed": [], "noop": [], "errors": []}

    # Group by file via resource table.
    by_file: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        res_row = conn.execute(
            "SELECT file_path FROM resource WHERE uid = ?", (r["target_id"],),
        ).fetchone()
        if not res_row:
            continue
        by_file.setdefault(res_row["file_path"], []).append((r["target_kind"], r["target_id"]))

    changed: list[str] = []
    noop: list[str] = []
    errors: list[tuple[str, str]] = []
    for fp_str, _entries in by_file.items():
        fp = Path(fp_str)
        if not fp.exists():
            errors.append((fp_str, "no longer exists"))
            continue
        text = fp.read_text(encoding="utf-8", errors="replace")
        # Strip all engram.io/* lines (works for both YAML and HCL formats).
        new_text = re.sub(
            r'^[ \t]*"?engram\.io/[\w.-]+"?\s*[:=]\s*"[^"]*"\s*,?\s*\n', "",
            text, flags=re.MULTILINE,
        )
        if new_text == text:
            noop.append(fp_str)
            continue
        changed.append(fp_str)
        if not dry_run:
            fp.write_text(new_text, encoding="utf-8")

    if not dry_run:
        conn.execute("DELETE FROM annotation")

    return {"dry_run": dry_run, "changed": changed, "noop": noop, "errors": errors}
