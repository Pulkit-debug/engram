"""docker-compose.yml extractor.

Each service in `services:` becomes a compose:service Resource. We harvest:
  * image / build dependencies (DEPENDS_ON technology)
  * depends_on: explicit service edges
  * environment / env_file (env_var/env_ref entities)
  * ports (port entities)
  * volumes (volume entities, MOUNTS edges)
  * networks
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedResource,
    ExtractionResult,
)


class DockerComposeExtractor(BaseExtractor):
    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("docker-compose")
        try:
            doc = yaml.safe_load(content)
        except yaml.YAMLError:
            return result
        if not isinstance(doc, dict):
            return result

        services = doc.get("services") or {}
        if not isinstance(services, dict):
            return result

        for svc_name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            self._emit_service(str(svc_name), svc, path, result)

        # Top-level volumes/networks
        for vol_name in (doc.get("volumes") or {}):
            result.entities.append(ExtractedEntity(
                name=str(vol_name), entity_type="volume",
                context_snippet="top-level volume",
            ))
        for net_name in (doc.get("networks") or {}):
            result.entities.append(ExtractedEntity(
                name=str(net_name), entity_type="network",
                context_snippet="top-level network",
            ))
        return result

    def _emit_service(
        self, name: str, svc: dict, path: Path, result: ExtractionResult,
    ) -> None:
        props: dict[str, Any] = {}
        image = svc.get("image")
        if isinstance(image, str):
            props["image"] = image
            tech = image.split(":")[0].split("@")[0].split("/")[-1]
            if tech:
                result.technologies.append(tech)
                result.edges.append(ExtractedEdge(
                    source_name=name, source_kind="resource",
                    target_name=tech, target_kind="technology",
                    rel_type="USES",
                    properties={"image_ref": image},
                ))
        if "build" in svc:
            props["build"] = svc["build"]

        result.resources.append(ExtractedResource(
            name=name, kind="compose:service",
            properties=props,
            context_snippet=f"compose service {name} in {path.name}",
        ))

        # depends_on
        deps = svc.get("depends_on")
        dep_list: list[str] = []
        if isinstance(deps, list):
            dep_list = [str(d) for d in deps]
        elif isinstance(deps, dict):
            dep_list = [str(k) for k in deps.keys()]
        for dep in dep_list:
            result.edges.append(ExtractedEdge(
                source_name=name, source_kind="resource",
                target_name=dep, target_kind="resource",
                rel_type="DEPENDS_ON",
            ))

        # environment
        env = svc.get("environment")
        if isinstance(env, list):
            for item in env:
                if isinstance(item, str):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        result.entities.append(ExtractedEntity(
                            name=k.strip(), entity_type="env_var",
                            value=v.strip(),
                            context_snippet=f"service {name}",
                        ))
                    else:
                        result.entities.append(ExtractedEntity(
                            name=item.strip(), entity_type="env_ref",
                            context_snippet=f"service {name}",
                        ))
        elif isinstance(env, dict):
            for k, v in env.items():
                result.entities.append(ExtractedEntity(
                    name=str(k), entity_type="env_var",
                    value="" if v is None else str(v),
                    context_snippet=f"service {name}",
                ))

        # ports
        ports = svc.get("ports") or []
        if isinstance(ports, list):
            for p in ports:
                port_str = str(p)
                # take container side (after ':' if present)
                container_side = port_str.split(":")[-1].split("/")[0]
                if container_side.isdigit():
                    result.entities.append(ExtractedEntity(
                        name=container_side, entity_type="port",
                        value=port_str,
                        context_snippet=f"service {name}",
                    ))

        # volumes -> MOUNTS edges
        vols = svc.get("volumes") or []
        if isinstance(vols, list):
            for v in vols:
                vstr = str(v)
                target = vstr.split(":")[-1] if ":" in vstr else vstr
                result.edges.append(ExtractedEdge(
                    source_name=name, source_kind="resource",
                    target_name=target, target_kind="entity",
                    rel_type="MOUNTS",
                    properties={"raw": vstr},
                ))
