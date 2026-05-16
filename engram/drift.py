"""engram drift — show resources in cloud but not in IaC, and vice versa.

The single most-demoable command in Engram. CTOs see this once and ask
where to install.

A resource is "click-ops drift" if:
  - it was discovered from a cloud CLI (properties.discovered_from != '')
  - AND no IaC-source Resource matches it by name OR appears as an edge
    target referencing its UID

A resource is "stale-IaC drift" if:
  - it was extracted from a source file (no discovered_from)
  - AND no cloud-discovered Resource matches it

Output is grouped by environment + tier so production drift is at the top.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


# Cloud-side resource kind prefixes (set by aws_import / kubectl_import).
_CLOUD_PROVIDERS = ("aws:", "gcp:", "azure:", "k8s:")

# IaC source resource kind prefixes (set by extractors).
_IAC_KINDS = ("tf:", "helm:", "compose:", "k8s:", "docker:", "jenkins:", "make:")


@dataclass
class DriftReport:
    untracked_cloud: list[dict[str, Any]] = field(default_factory=list)
    stale_iac: list[dict[str, Any]] = field(default_factory=list)

    @property
    def untracked_prod_count(self) -> int:
        return sum(1 for r in self.untracked_cloud
                   if r.get("environment") == "production"
                   or r.get("risk_tier") == "red")


def detect_drift(conn: sqlite3.Connection) -> DriftReport:
    """Walk both populations and emit a DriftReport."""
    report = DriftReport()

    # --- Step 1: build the lookup index from IaC-source resources. ---
    #   name → set of (kind, uid) tuples
    iac_by_name: dict[str, list[tuple[str, str]]] = {}
    iac_rows = conn.execute(
        """
        SELECT r.uid, r.kind, r.name, r.properties
        FROM resource r
        WHERE r.properties NOT LIKE '%"discovered_from":%'
           OR r.properties LIKE '%"discovered_from": ""%'
        """
    ).fetchall()
    for r in iac_rows:
        if not r["name"]:
            continue
        iac_by_name.setdefault(r["name"].lower(), []).append((r["kind"], r["uid"]))

    # --- Step 2: walk cloud-discovered resources. ---
    cloud_rows = conn.execute(
        """
        SELECT uid, kind, name, namespace, environment, risk_tier, properties
        FROM resource
        WHERE properties LIKE '%"discovered_from":%'
          AND NOT properties LIKE '%"discovered_from": ""%'
        ORDER BY risk_tier DESC, environment DESC, name
        """
    ).fetchall()

    for r in cloud_rows:
        if not r["name"]:
            continue
        match = iac_by_name.get(r["name"].lower(), [])
        if match:
            # Found at least one IaC resource with the same name → tracked.
            continue
        # Also check if any edge points at this UID (e.g., value_match infer'd
        # edges from .env files to this cloud resource → it's "tracked enough").
        edge_hit = conn.execute(
            "SELECT 1 FROM edge WHERE dst_kind='resource' AND dst_id=? LIMIT 1",
            (r["uid"],),
        ).fetchone()
        if edge_hit:
            continue

        report.untracked_cloud.append({
            "uid": r["uid"],
            "kind": r["kind"],
            "name": r["name"],
            "namespace": r["namespace"],
            "environment": r["environment"] or "",
            "risk_tier": r["risk_tier"],
            "discovered_from": _discovered_from(r["properties"]),
        })

    # --- Step 3: walk IaC-side and find resources with no cloud match. ---
    cloud_by_name: dict[str, list[tuple[str, str]]] = {}
    for r in cloud_rows:
        if r["name"]:
            cloud_by_name.setdefault(r["name"].lower(), []).append((r["kind"], r["uid"]))

    for r in iac_rows:
        if not r["name"]:
            continue
        if r["name"].lower() in cloud_by_name:
            continue
        # An IaC-defined resource with no matching cloud presence is
        # "potentially stale" — but only emit it if the kind is something
        # that *should* manifest in the cloud. tf:* and k8s:* with a
        # production tier are the interesting cases.
        kind = r["kind"] or ""
        if not (kind.startswith("tf:") or kind.startswith("k8s:")
                or kind.startswith("helm:") or kind.startswith("aws:")):
            continue
        # File-level Resources (tf:module, helm:values, etc.) are noise
        # for drift — skip them.
        if kind in ("tf:module", "tf:local_file", "helm:values",
                    "yaml:document"):
            continue

        # Pull environment from properties if available.
        env = ""
        props = r["properties"]
        if props:
            try:
                p = json.loads(props) if isinstance(props, str) else props
                env = p.get("environment", "") if isinstance(p, dict) else ""
            except (json.JSONDecodeError, TypeError):
                env = ""

        report.stale_iac.append({
            "uid": r["uid"],
            "kind": kind,
            "name": r["name"],
            "environment": env,
            "file_path": _file_path_for(conn, r["uid"]),
        })

    return report


def render_drift(report: DriftReport, *, max_rows: int = 30) -> str:
    """Render a DriftReport as a plain-text table, returns the rendered string."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 76)
    lines.append("  ENGRAM DRIFT REPORT")
    lines.append("=" * 76)

    # Section 1: untracked cloud resources.
    lines.append("")
    lines.append("RESOURCES IN CLOUD BUT NOT IN IaC (untracked)")
    lines.append("-" * 76)
    if not report.untracked_cloud:
        lines.append("  (none — every discovered cloud resource has a matching")
        lines.append("   IaC reference or an edge linking it to source.)")
    else:
        # Production first.
        prod = [r for r in report.untracked_cloud
                if r["environment"] == "production" or r["risk_tier"] == "red"]
        nonprod = [r for r in report.untracked_cloud if r not in prod]
        for r in (prod + nonprod)[:max_rows]:
            tier_marker = "❗ " if (r["environment"] == "production"
                                    or r["risk_tier"] == "red") else "  "
            lines.append(
                f"  {tier_marker}{r['kind']:30s} {r['name']:30s}  "
                f"{r['environment'] or '-':12s} "
                f"tier={r['risk_tier']}"
            )
        remaining = len(report.untracked_cloud) - max_rows
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")

    # Section 2: stale IaC.
    lines.append("")
    lines.append("RESOURCES IN IaC BUT NOT IN CLOUD (stale or pending apply)")
    lines.append("-" * 76)
    if not report.stale_iac:
        lines.append("  (none — every meaningful IaC-declared resource has a")
        lines.append("   matching cloud presence.)")
    else:
        for r in report.stale_iac[:max_rows]:
            lines.append(
                f"    {r['kind']:30s} {r['name']:30s}  {r['file_path'] or ''}"
            )
        remaining = len(report.stale_iac) - max_rows
        if remaining > 0:
            lines.append(f"    ... and {remaining} more")

    # Summary.
    lines.append("")
    lines.append("SUMMARY")
    lines.append("-" * 76)
    lines.append(f"  {len(report.untracked_cloud):>4} cloud resources have no matching IaC reference.")
    lines.append(f"  {report.untracked_prod_count:>4}    of those are tagged or named as production.")
    lines.append(f"  {len(report.stale_iac):>4} IaC-declared resources have no matching cloud resource.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _discovered_from(props: str | None) -> str:
    if not props:
        return ""
    try:
        p = json.loads(props) if isinstance(props, str) else props
        return p.get("discovered_from", "") if isinstance(p, dict) else ""
    except (json.JSONDecodeError, TypeError):
        return ""


def _file_path_for(conn: sqlite3.Connection, resource_uid: str) -> str:
    row = conn.execute(
        "SELECT file_path FROM resource WHERE uid = ?", (resource_uid,),
    ).fetchone()
    return row["file_path"] if row else ""
