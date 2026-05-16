"""JavaScript / TypeScript extractor — uses tree-sitter for correctness.

tree-sitter-language-pack provides bundled grammars for js/ts/tsx. If it
fails to import, we fall back to a regex pass — degraded but functional.
"""

from __future__ import annotations

import re
from pathlib import Path

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEntity,
    ExtractionResult,
)


_IMPORT_RE = re.compile(
    r'^\s*import\s+(?:[\w*{},\s]+\s+from\s+)?["\']([^"\']+)["\']',
    re.MULTILINE,
)
_REQUIRE_RE = re.compile(r'\brequire\s*\(\s*["\']([^"\']+)["\']\s*\)')
_FUNCTION_RE = re.compile(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(')
_ARROW_NAME_RE = re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>')
_CLASS_RE = re.compile(r'\bclass\s+([A-Za-z_$][\w$]*)')
_EXPORT_DEFAULT_RE = re.compile(r'\bexport\s+default\s+(?:function\s+|class\s+)?([A-Za-z_$][\w$]*)')
_PROCESS_ENV_RE = re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)|process\.env\[\s*["\']([A-Z_][A-Z0-9_]*)["\']\s*\]')


class JsTsExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        ext = path.suffix.lower()
        if ext in (".ts", ".tsx"):
            result.technologies.append("typescript")
        else:
            result.technologies.append("javascript")

        # Try tree-sitter first (more accurate), fall back to regex.
        used_ts = self._extract_via_tree_sitter(content, path, result)
        if not used_ts:
            self._extract_via_regex(content, path, result)

        # Env refs are always regex'd from raw source — tree-sitter would
        # be overkill for this pattern.
        seen: set[str] = set()
        for m in _PROCESS_ENV_RE.finditer(content):
            name = m.group(1) or m.group(2)
            if not name or name in seen:
                continue
            seen.add(name)
            result.entities.append(ExtractedEntity(
                name=name, entity_type="env_ref",
                context_snippet=f"process.env in {path.name}",
            ))
        return result

    def _extract_via_tree_sitter(self, content: str, path: Path, result: ExtractionResult) -> bool:
        try:
            from tree_sitter_language_pack import get_parser
        except Exception:
            return False

        ext = path.suffix.lower()
        lang_name = {
            ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "tsx",
            ".mjs": "javascript", ".cjs": "javascript",
        }.get(ext)
        if not lang_name:
            return False
        try:
            parser = get_parser(lang_name)
            tree = parser.parse(content.encode("utf-8", errors="replace"))
        except Exception:
            return False

        root = tree.root_node
        # Walk for function/class/import declarations.
        for node in _walk(root):
            t = node.type
            if t in ("function_declaration", "function"):
                name = _child_text(node, "name", content)
                if name:
                    result.entities.append(ExtractedEntity(
                        name=name, entity_type="function",
                        context_snippet=f"function {name} in {path.name}",
                    ))
            elif t == "class_declaration":
                name = _child_text(node, "name", content)
                if name:
                    result.entities.append(ExtractedEntity(
                        name=name, entity_type="class",
                        context_snippet=f"class {name} in {path.name}",
                    ))
            elif t in ("import_statement", "import_clause"):
                # Source string.
                txt = _node_text(node, content)
                m = re.search(r'["\']([^"\']+)["\']', txt)
                if m:
                    result.entities.append(ExtractedEntity(
                        name=m.group(1), entity_type="import",
                        context_snippet=txt[:140],
                    ))
        return True

    def _extract_via_regex(self, content: str, path: Path, result: ExtractionResult):
        for m in _IMPORT_RE.finditer(content):
            result.entities.append(ExtractedEntity(
                name=m.group(1), entity_type="import",
                context_snippet=f"import in {path.name}",
            ))
        for m in _REQUIRE_RE.finditer(content):
            result.entities.append(ExtractedEntity(
                name=m.group(1), entity_type="import",
                context_snippet=f"require in {path.name}",
            ))
        for m in _FUNCTION_RE.finditer(content):
            result.entities.append(ExtractedEntity(
                name=m.group(1), entity_type="function",
                context_snippet=f"function in {path.name}",
            ))
        for m in _ARROW_NAME_RE.finditer(content):
            result.entities.append(ExtractedEntity(
                name=m.group(1), entity_type="function",
                context_snippet=f"arrow function in {path.name}",
            ))
        for m in _CLASS_RE.finditer(content):
            result.entities.append(ExtractedEntity(
                name=m.group(1), entity_type="class",
                context_snippet=f"class in {path.name}",
            ))


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _child_text(node, field_name: str, source: str) -> str | None:
    for child in node.children:
        # tree-sitter Python bindings expose field names via child_by_field_name,
        # but for portability across versions we scan by type.
        if child.type == "identifier":
            return _node_text(child, source)
    # Fall back to first identifier descendant.
    for n in _walk(node):
        if n.type == "identifier":
            return _node_text(n, source)
    return None


def _node_text(node, source: str) -> str:
    return source[node.start_byte:node.end_byte]
