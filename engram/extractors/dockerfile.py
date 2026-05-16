"""Dockerfile extractor.

Emits:
  * one ExtractedResource of kind "docker:image" per Dockerfile (FROM lines may
    indicate multi-stage; we capture the *final* stage as the resource and
    intermediate stages as edges).
  * ExtractedEntity rows for env_var defs, env_ref usages, exposed ports.
  * Edges: the image DEPENDS_ON each FROM base image; USES_ENV per ENV; etc.
"""

from __future__ import annotations

import re
from pathlib import Path

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedResource,
    ExtractionResult,
)


_INSTRUCTION_RE = re.compile(
    r"^\s*(FROM|EXPOSE|ENV|ARG|COPY|ADD|RUN|CMD|ENTRYPOINT|WORKDIR|LABEL|VOLUME|USER|HEALTHCHECK)\s+(.+)$",
    re.IGNORECASE,
)
_ENV_REF_RE = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")


class DockerfileExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("docker")

        # Resource name: prefer last `FROM ... AS <stage>` name; else the file
        # parent directory (a common convention).
        stages: list[str] = []
        base_images: list[str] = []

        env_defs_emitted: set[str] = set()

        # Continuation handling: backslash-newline joins are common in Dockerfiles.
        joined_lines: list[str] = []
        buf = ""
        for raw in content.splitlines():
            stripped = raw.rstrip()
            if stripped.endswith("\\"):
                buf += stripped[:-1] + " "
                continue
            joined_lines.append(buf + stripped)
            buf = ""
        if buf:
            joined_lines.append(buf)

        for line in joined_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _INSTRUCTION_RE.match(stripped)
            if not m:
                continue
            instr = m.group(1).upper()
            args = m.group(2).strip()

            if instr == "FROM":
                # FROM image[:tag] [AS stage]
                parts = args.split()
                image = parts[0]
                base_images.append(image)
                stage_name = None
                for i, tok in enumerate(parts):
                    if tok.upper() == "AS" and i + 1 < len(parts):
                        stage_name = parts[i + 1]
                        break
                if stage_name:
                    stages.append(stage_name)

            elif instr == "EXPOSE":
                for port in args.split():
                    port_clean = port.split("/")[0].strip()
                    if port_clean:
                        result.entities.append(ExtractedEntity(
                            name=port_clean,
                            entity_type="port",
                            value=port,
                            context_snippet=f"EXPOSE {args}",
                        ))

            elif instr == "ENV":
                # ENV K=v or ENV K v (split by first '=' or first whitespace).
                for piece in _split_env_pairs(args):
                    k, v = piece
                    if k and k not in env_defs_emitted:
                        env_defs_emitted.add(k)
                        result.entities.append(ExtractedEntity(
                            name=k,
                            entity_type="env_var",
                            value=v,
                            context_snippet=f"ENV {k}={v}",
                        ))

            elif instr == "ARG":
                # ARG NAME[=default]
                a = args.split("=", 1)
                arg_name = a[0].strip()
                arg_val = a[1].strip() if len(a) > 1 else ""
                if arg_name:
                    result.entities.append(ExtractedEntity(
                        name=arg_name,
                        entity_type="build_arg",
                        value=arg_val,
                        context_snippet=f"ARG {args}",
                    ))

            # Any instruction can reference env vars via $VAR / ${VAR}.
            for ref in _ENV_REF_RE.findall(stripped):
                if ref not in env_defs_emitted:
                    result.entities.append(ExtractedEntity(
                        name=ref,
                        entity_type="env_ref",
                        value="",
                        context_snippet=stripped[:140],
                    ))

        # The Dockerfile itself is one image resource.
        resource_name = path.parent.name if path.name.lower().startswith("dockerfile") else path.stem
        final_image = base_images[-1] if base_images else ""
        resource = ExtractedResource(
            name=resource_name,
            kind="docker:image",
            properties={
                "base_images": base_images,
                "stages": stages,
                "final_base": final_image,
            },
            context_snippet=f"Dockerfile at {path}",
        )
        result.resources.append(resource)

        # Edges: image DEPENDS_ON each base image (as a technology).
        for img in base_images:
            # Normalize to the image *name* (drop tag).
            name_only = img.split(":")[0].split("@")[0].strip()
            if not name_only:
                continue
            result.technologies.append(name_only)
            result.edges.append(ExtractedEdge(
                source_name=resource_name,
                source_kind="resource",
                target_name=name_only,
                target_kind="technology",
                rel_type="DEPENDS_ON",
                properties={"image_ref": img},
            ))

        return result


def _split_env_pairs(args: str) -> list[tuple[str, str]]:
    """Parse the right-hand side of an ENV instruction into (key, value) pairs.

    Dockerfile supports two ENV syntaxes:
      ENV K=v K2=v2          (multiple pairs on one line)
      ENV K   value with spaces  (single pair, value continues to end of line)
    """
    out: list[tuple[str, str]] = []
    s = args.strip()
    if "=" not in s:
        # `ENV K rest of line` form.
        parts = s.split(None, 1)
        if not parts:
            return out
        key = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        out.append((key, val))
        return out

    # Multiple K=V pairs. Quote-aware tokenization keeps "K=\"a b\"" intact.
    tokens = _tokenize_env_pairs(s)
    for tok in tokens:
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        out.append((k.strip(), v.strip().strip('"').strip("'")))
    return out


def _tokenize_env_pairs(s: str) -> list[str]:
    """Split on whitespace honoring quoted values."""
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens
