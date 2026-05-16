"""Terraform HCL extractor.

Pure Python regex/state-machine parser. We don't take a tree-sitter dep for
HCL because we only need shallow structure: resource / module / data blocks
and their first-level attributes. The post-pass on the graph builds edges
from `depends_on` references and module sources.

Emits:
  * tf:<resource_type> resources (one per `resource "<type>" "<name>" {}`)
  * tf:module resources (one per `module "<name>" {}`)
  * tf:data resources (one per `data "<type>" "<name>" {}`)
  * Edges:
      - DEPENDS_ON for explicit depends_on = [...]
      - USES for module sources / provider refs
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedResource,
    ExtractionResult,
)


# Top-level block matcher.
# Matches:  block_type "label1" "label2" {   ... }
# block_type is one of resource / data / module / provider / variable / output.
_BLOCK_HEADER_RE = re.compile(
    r'^\s*(resource|data|module|provider|variable|output|locals|terraform)'
    r'(?:\s+"([^"]+)")?(?:\s+"([^"]+)")?\s*\{',
    re.MULTILINE,
)

_DEPENDS_ON_RE = re.compile(r'depends_on\s*=\s*\[([^\]]+)\]', re.DOTALL)
_SOURCE_RE = re.compile(r'source\s*=\s*"([^"]+)"')
_VERSION_RE = re.compile(r'version\s*=\s*"([^"]+)"')
# HCL references look like `<type>.<name>` (e.g. aws_db_instance.prod_db).
# We capture (type, name) so we can filter out variable / local / each / etc.
# refs that are NOT inter-resource dependencies.
_REFERENCE_RE = re.compile(r'\b([a-z][a-z0-9_]*)\.([a-zA-Z_][\w]*)\b')

# HCL prefixes that are NOT resource references — they're language constructs
# or built-in objects. Adding DEPENDS_ON edges to them would be wrong.
_HCL_NON_RESOURCE_PREFIXES = frozenset({
    "var", "local", "locals", "each", "count", "path", "terraform", "self",
    "data", "module",  # we handle module separately; data refs are not deps
    # Common HCL functions whose call sites look like `length.foo` would be
    # mis-tokenized; functions are normally called as `length(x)` not
    # `length.x`, but be defensive.
    "length", "lookup", "merge", "concat", "join", "split", "format",
    "regex", "regexall", "lower", "upper", "title", "trimspace",
    "abspath", "dirname", "basename", "file", "fileexists",
    "jsonencode", "jsondecode", "yamlencode", "yamldecode",
    "tobool", "tonumber", "tostring", "tolist", "tomap", "toset",
    "try", "can", "coalesce", "min", "max", "ceil", "floor",
    "timestamp", "formatdate", "uuid", "uuidv5",
    "base64encode", "base64decode", "sha1", "sha256", "sha512", "md5",
})


def _filter_resource_refs(refs: list[tuple[str, str]]) -> list[str]:
    """Drop refs whose type prefix is a Terraform language construct.

    Input: list of (type_prefix, name) pairs from `_REFERENCE_RE.findall(...)`.
    Output: list of target names that look like real resource references.
    """
    out: list[str] = []
    for type_prefix, name in refs:
        if type_prefix in _HCL_NON_RESOURCE_PREFIXES:
            continue
        # The prefix should look like a provider-typed resource: aws_X, google_X,
        # azurerm_X, etc. We allow any `<word>_<more>` shape since custom and
        # community providers vary.
        if "_" not in type_prefix:
            # No underscore — most builtins and trivial vars; skip to be safe.
            continue
        out.append(name)
    return out


class TerraformExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("terraform")

        # Top-level block scan.
        for m in _BLOCK_HEADER_RE.finditer(content):
            block_type = m.group(1)
            label1 = m.group(2) or ""
            label2 = m.group(3) or ""
            body_start = m.end()
            body = _extract_balanced_body(content, body_start)
            if body is None:
                continue

            if block_type == "resource":
                tf_type, tf_name = label1, label2
                if not tf_type or not tf_name:
                    continue
                self._emit_resource(tf_type, tf_name, body, path, result)
            elif block_type == "data":
                tf_type, tf_name = label1, label2
                if not tf_type or not tf_name:
                    continue
                self._emit_data(tf_type, tf_name, body, path, result)
            elif block_type == "module":
                if not label1:
                    continue
                self._emit_module(label1, body, path, result)
            elif block_type == "provider":
                if label1:
                    result.technologies.append(label1)

        return result

    def _emit_resource(
        self, tf_type: str, tf_name: str, body: str, path: Path, result: ExtractionResult,
    ) -> None:
        env = self._infer_environment(str(path), tf_name, body)
        props: dict[str, Any] = {"tf_type": tf_type}

        # Pull a few common attrs cheaply (region, environment, tags).
        if m := re.search(r'\bregion\s*=\s*"([^"]+)"', body):
            props["region"] = m.group(1)
        if m := re.search(r'\bengine\s*=\s*"([^"]+)"', body):
            props["engine"] = m.group(1)

        result.resources.append(ExtractedResource(
            name=tf_name,
            kind=f"tf:{tf_type}",
            namespace="",
            environment=env,
            properties=props,
            context_snippet=f'resource "{tf_type}" "{tf_name}" in {path.name}',
        ))

        # Provider technology.
        provider = tf_type.split("_", 1)[0]
        if provider:
            result.technologies.append(provider)
            result.edges.append(ExtractedEdge(
                source_name=tf_name, source_kind="resource",
                target_name=provider, target_kind="technology",
                rel_type="USES",
            ))

        # Explicit depends_on — filter language constructs out.
        if m := _DEPENDS_ON_RE.search(body):
            for target in _filter_resource_refs(_REFERENCE_RE.findall(m.group(1))):
                result.edges.append(ExtractedEdge(
                    source_name=tf_name, source_kind="resource",
                    target_name=target, target_kind="resource",
                    rel_type="DEPENDS_ON",
                ))

        # Implicit references in the body — same filter, marked `implicit`.
        seen_implicit: set[str] = set()
        for target in _filter_resource_refs(_REFERENCE_RE.findall(body)):
            if not target or target == tf_name or target in seen_implicit:
                continue
            seen_implicit.add(target)
            result.edges.append(ExtractedEdge(
                source_name=tf_name, source_kind="resource",
                target_name=target, target_kind="resource",
                rel_type="DEPENDS_ON",
                properties={"implicit": True},
            ))

    def _emit_data(
        self, tf_type: str, tf_name: str, body: str, path: Path, result: ExtractionResult,
    ) -> None:
        result.resources.append(ExtractedResource(
            name=tf_name,
            kind=f"tf:data:{tf_type}",
            context_snippet=f'data "{tf_type}" "{tf_name}" in {path.name}',
        ))

    def _emit_module(self, name: str, body: str, path: Path, result: ExtractionResult) -> None:
        props: dict[str, Any] = {}
        if m := _SOURCE_RE.search(body):
            props["source"] = m.group(1)
        if m := _VERSION_RE.search(body):
            props["version"] = m.group(1)
        env = self._infer_environment(str(path), name, body)
        result.resources.append(ExtractedResource(
            name=name, kind="tf:module",
            environment=env, properties=props,
            context_snippet=f'module "{name}" in {path.name}',
        ))
        if "source" in props:
            result.edges.append(ExtractedEdge(
                source_name=name, source_kind="resource",
                target_name=props["source"], target_kind="resource",
                rel_type="USES",
                properties={"version": props.get("version", "")},
            ))

    def _infer_environment(self, path: str, name: str, body: str) -> str:
        p = path.lower().replace("\\", "/")
        for seg in p.split("/"):
            if seg in ("prod", "production", "live"):
                return "production"
            if seg in ("stag", "staging", "preprod", "uat"):
                return "staging"
            if seg in ("dev", "develop", "development", "local", "sandbox"):
                return "dev"
        if "prod" in name.lower() or "production" in name.lower():
            return "production"
        if "stag" in name.lower():
            return "staging"
        # Body hints: tags = { Environment = "production" }
        if m := re.search(r'Environment\s*=\s*"([^"]+)"', body, re.IGNORECASE):
            v = m.group(1).lower()
            if v in ("production", "prod"):
                return "production"
            if v in ("staging", "stage", "uat"):
                return "staging"
            return v
        return ""


def _extract_balanced_body(s: str, start: int) -> str | None:
    """Given a string and a position immediately AFTER an opening `{`, return
    the body (excluding the closing `}`). Handles nested braces and strings.
    Returns None on unbalanced input.
    """
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
        if ch in ('"',):
            in_str = ch
            i += 1
            continue
        if ch == "#" or (ch == "/" and i + 1 < len(s) and s[i + 1] == "/"):
            # line comment — skip to EOL
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
