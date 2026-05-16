"""Python AST extractor — uses the standard library, no tree-sitter required.

Why ast over tree-sitter for Python: ast.parse is correct, fast, and ships
with Python. tree-sitter-python is great for incremental editor use; we're
doing batch indexing, so ast wins on simplicity.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractionResult,
)


_OS_ENVIRON_RE = re.compile(r'os\.environ(?:\.get)?\s*\(?\s*["\']([A-Z_][A-Z0-9_]*)["\']')
_OS_ENVIRON_SUB_RE = re.compile(r'os\.environ\s*\[\s*["\']([A-Z_][A-Z0-9_]*)["\']')


class PythonExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("python")

        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError:
            # Still harvest env refs via regex (the file may be partially-broken
            # config or a template).
            self._regex_env_pass(content, path, result)
            return result

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._emit_function(node, result, path)
            elif isinstance(node, ast.ClassDef):
                self._emit_class(node, result, path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    result.entities.append(ExtractedEntity(
                        name=alias.name, entity_type="import",
                        value=alias.asname or "",
                        context_snippet=f"import {alias.name}",
                    ))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    full = f"{mod}.{alias.name}" if mod else alias.name
                    result.entities.append(ExtractedEntity(
                        name=full, entity_type="import",
                        value=alias.asname or "",
                        context_snippet=f"from {mod} import {alias.name}",
                    ))

        # Env refs (os.environ usage) — quick regex pass over raw source.
        self._regex_env_pass(content, path, result)
        return result

    def _emit_function(self, node, result, path):
        result.entities.append(ExtractedEntity(
            name=node.name, entity_type="function",
            context_snippet=f"def {node.name}() in {path.name}",
        ))

    def _emit_class(self, node, result, path):
        result.entities.append(ExtractedEntity(
            name=node.name, entity_type="class",
            context_snippet=f"class {node.name} in {path.name}",
        ))

    def _regex_env_pass(self, content: str, path: Path, result: ExtractionResult):
        seen: set[str] = set()
        for m in _OS_ENVIRON_RE.finditer(content):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            result.entities.append(ExtractedEntity(
                name=name, entity_type="env_ref",
                context_snippet=f"os.environ in {path.name}",
            ))
        for m in _OS_ENVIRON_SUB_RE.finditer(content):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            result.entities.append(ExtractedEntity(
                name=name, entity_type="env_ref",
                context_snippet=f"os.environ[...] in {path.name}",
            ))
