"""Codified Context emitter.

Writes human-reviewable markdown that any AI agent reads pre-session — works
without MCP, works air-gapped, works on locked-down corporate laptops.

Design principles:
  * Engram only modifies sections marked with `<!-- engram:auto-generated -->`
    and `<!-- engram:end -->`. Any human-curated content above or below those
    markers is preserved byte-for-byte.
  * Every auto-generated section is HMAC-signed. Re-emission verifies the
    previous signature; if a section was tampered with after emission,
    Engram surfaces it (and refuses to overwrite without --force).
  * The signature is recorded in the `emission` table; `verify_context` MCP
    tool consults it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from engram.graph import graph_stats

if TYPE_CHECKING:
    from engram.config import Config


ENGRAM_START = "<!-- engram:auto-generated -->"
ENGRAM_END = "<!-- engram:end -->"
ENGRAM_META_PREFIX = "<!-- engram:meta "
ENGRAM_META_SUFFIX = " -->"


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def _signing_key(config: "Config") -> bytes:
    """Return the bytes used for HMAC signing.

    If config.signing_key is empty, derive one from the DB content_hash of
    the engram database file. Stable across runs, unique per machine.
    """
    if config.signing_key:
        return config.signing_key.encode("utf-8", errors="replace")
    # Fall back to a hash of the DB path + a constant. Not cryptographic-grade
    # against a determined attacker with disk access; the threat model here
    # is accidental drift, not deliberate forgery.
    seed = f"engram:{config.db_path}:v2".encode()
    return hashlib.sha256(seed).digest()


def sign_section(content: str, config: "Config") -> str:
    """Return a hex HMAC-SHA256 of the section content.

    Content is normalized (strip surrounding newlines, strip the metadata line
    if present) so that signing is invariant under whitespace round-trips.
    """
    normalized = _normalize_for_signing(content)
    return hmac.new(_signing_key(config), normalized.encode("utf-8", errors="replace"),
                    hashlib.sha256).hexdigest()[:32]


def _normalize_for_signing(s: str) -> str:
    """Strip surrounding newlines and an optional leading meta line."""
    s = s.strip("\n")
    if s.startswith(ENGRAM_META_PREFIX):
        eol = s.find("\n")
        if eol != -1:
            s = s[eol + 1:].strip("\n")
    return s


def verify_section(content: str, signature: str, config: "Config") -> bool:
    return hmac.compare_digest(sign_section(content, config), signature)


# ---------------------------------------------------------------------------
# AGENTS.md rendering
# ---------------------------------------------------------------------------

def render_agents_md_section(conn: sqlite3.Connection) -> str:
    """Render the auto-generated infrastructure overview section."""
    stats = graph_stats(conn)

    # Project list with file/resource counts.
    projects = conn.execute("""
        SELECT p.path, p.name, p.project_type,
               (SELECT count(*) FROM file WHERE project_path = p.path) AS file_count,
               (SELECT count(*) FROM resource r JOIN file f ON f.path = r.file_path
                WHERE f.project_path = p.path) AS resource_count
        FROM project p
        ORDER BY resource_count DESC, file_count DESC
        LIMIT 20
    """).fetchall()

    # Production resources (red tier).
    prod_resources = conn.execute("""
        SELECT kind, name, namespace, file_path
        FROM resource WHERE risk_tier = 'red' AND environment = 'production'
        ORDER BY kind, name
        LIMIT 50
    """).fetchall()

    # Cross-format resources for the same service name.
    # (We pick the top 10 names that appear in 3+ different kinds.)
    multi_kind = conn.execute("""
        SELECT name, count(DISTINCT kind) AS kind_count,
               group_concat(DISTINCT kind) AS kinds
        FROM resource
        WHERE name <> ''
        GROUP BY name
        HAVING kind_count >= 3
        ORDER BY kind_count DESC, name
        LIMIT 10
    """).fetchall()

    # Tech inventory.
    techs = conn.execute(
        "SELECT name FROM technology ORDER BY name LIMIT 50"
    ).fetchall()

    # Environment variables used in 2+ files (cross-cutting concerns).
    cross_env = conn.execute("""
        SELECT name, count(DISTINCT file_path) AS files
        FROM entity
        WHERE entity_type IN ('env_var','env_ref')
        GROUP BY name
        HAVING files >= 2
        ORDER BY files DESC, name
        LIMIT 30
    """).fetchall()

    parts: list[str] = []
    parts.append("## Infrastructure overview")
    parts.append("")
    parts.append("> This section is generated by [Engram](https://github.com/pulkit-verma/engram). "
                 "Anything outside the engram markers is yours — Engram will never overwrite it.")
    parts.append("")
    parts.append("### Inventory")
    parts.append("")
    parts.append(f"- **Projects:** {stats.get('project', 0)}")
    parts.append(f"- **Resources:** {stats.get('resource', 0)} (infra structural units)")
    parts.append(f"- **Files:** {stats.get('file', 0)}")
    parts.append(f"- **Edges:** {stats.get('edge', 0)} (dependencies, uses, mounts, etc.)")
    parts.append(f"- **Technologies:** {stats.get('technology', 0)}")
    parts.append("")

    if projects:
        parts.append("### Projects")
        parts.append("")
        parts.append("| Project | Type | Files | Resources |")
        parts.append("|---|---|---:|---:|")
        for p in projects:
            parts.append(f"| `{p['name']}` | {p['project_type'] or '-'} | "
                         f"{p['file_count']} | {p['resource_count']} |")
        parts.append("")

    if prod_resources:
        parts.append("### Production resources (treat as RED)")
        parts.append("")
        parts.append("Before any destructive operation against these, call `blast_radius` first.")
        parts.append("")
        parts.append("| Kind | Name | Namespace | File |")
        parts.append("|---|---|---|---|")
        for r in prod_resources:
            parts.append(f"| `{r['kind']}` | **{r['name']}** | "
                         f"{r['namespace'] or '-'} | `{r['file_path']}` |")
        parts.append("")

    if multi_kind:
        parts.append("### Cross-format services")
        parts.append("")
        parts.append("Service names that span multiple infrastructure formats — likely "
                     "represent a single logical service. Modifying any one usually "
                     "requires considering the others.")
        parts.append("")
        parts.append("| Service | Formats |")
        parts.append("|---|---|")
        for m in multi_kind:
            kinds = ", ".join(f"`{k}`" for k in sorted((m["kinds"] or "").split(",")))
            parts.append(f"| **{m['name']}** | {kinds} |")
        parts.append("")

    if cross_env:
        parts.append("### Cross-file environment variables")
        parts.append("")
        parts.append("| Variable | Files |")
        parts.append("|---|---:|")
        for e in cross_env:
            parts.append(f"| `{e['name']}` | {e['files']} |")
        parts.append("")

    if techs:
        names = sorted({t["name"] for t in techs})
        parts.append("### Detected technologies")
        parts.append("")
        parts.append(", ".join(f"`{n}`" for n in names))
        parts.append("")

    parts.append("### How to query more")
    parts.append("")
    parts.append("Use Engram's MCP tools for live, structured queries:")
    parts.append("- `blast_radius(operation, target)` — **call before any destructive operation**")
    parts.append("- `infra_context(target, token_budget)` — full structured context for a service")
    parts.append("- `trace_env_var(name)` — where is this env var defined and consumed")
    parts.append("- `dependents_of(target)` — reverse-lookup")
    parts.append("- `infra_diff(days)` — recent infra changes")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# File update logic
# ---------------------------------------------------------------------------

def upsert_agents_md(
    conn: sqlite3.Connection,
    config: "Config",
    target_path: Path,
    *,
    force: bool = False,
) -> dict:
    """Write or update an AGENTS.md / CLAUDE.md / INFRA.md.

    Returns a dict with keys: action ('created'|'updated'|'unchanged'|'tampered'),
    path, signature, sig_old (when applicable).

    If the existing engram section has a signature in the `emission` table and
    the section on disk does not match that signature, we surface
    action='tampered' and refuse to overwrite unless force=True.
    """
    section = render_agents_md_section(conn)
    signature = sign_section(section, config)
    now = datetime.now(timezone.utc).isoformat()

    # Read existing.
    existing_text = ""
    if target_path.exists():
        try:
            existing_text = target_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing_text = ""

    # Find existing engram block.
    start_idx = existing_text.find(ENGRAM_START)
    end_idx = existing_text.find(ENGRAM_END, start_idx + 1) if start_idx != -1 else -1

    # Tamper check: compare existing section against the recorded signature.
    sig_old = None
    if start_idx != -1 and end_idx != -1:
        body_start = existing_text.find("\n", start_idx) + 1
        meta_end = _find_meta_end(existing_text, body_start)
        section_body_start = meta_end if meta_end != -1 else body_start
        existing_section = existing_text[section_body_start:end_idx].rstrip("\n")
        emission_row = conn.execute(
            "SELECT content_hash, signature FROM emission WHERE path = ?",
            (str(target_path),),
        ).fetchone()
        if emission_row:
            sig_old = emission_row["signature"]
            if not verify_section(existing_section, sig_old, config) and not force:
                return {
                    "action": "tampered",
                    "path": str(target_path),
                    "reason": "existing engram section signature mismatch — pass --force to overwrite",
                }

    meta_line = f"{ENGRAM_META_PREFIX}{json.dumps({'sig': signature, 'emitted_at': now})}{ENGRAM_META_SUFFIX}"
    new_block = f"{ENGRAM_START}\n{meta_line}\n{section}\n{ENGRAM_END}"

    if start_idx == -1:
        # No existing block — append.
        if existing_text and not existing_text.endswith("\n"):
            existing_text += "\n"
        new_text = existing_text + ("\n" if existing_text else "") + new_block + "\n"
        action = "created" if not target_path.exists() else "updated"
    else:
        block_end = end_idx + len(ENGRAM_END)
        new_text = existing_text[:start_idx] + new_block + existing_text[block_end:]
        action = "updated" if new_text != existing_text else "unchanged"

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(new_text, encoding="utf-8")

    # Record the emission.
    content_hash = hashlib.sha256(section.encode("utf-8", errors="replace")).hexdigest()[:32]
    conn.execute(
        """
        INSERT INTO emission(path, kind, content_hash, signature, emitted_at, scope)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            kind=excluded.kind,
            content_hash=excluded.content_hash,
            signature=excluded.signature,
            emitted_at=excluded.emitted_at,
            scope=excluded.scope
        """,
        (str(target_path), _guess_kind(target_path), content_hash, signature, now,
         json.dumps({"scope": "global"})),
    )

    return {
        "action": action,
        "path": str(target_path),
        "signature": signature,
        "sig_old": sig_old,
    }


def detect_drift(conn: sqlite3.Connection, config: "Config", target_path: Path) -> dict:
    """Compare the file's current engram section against the current graph state.

    Returns: {drift: bool, on_disk_signature, current_signature, diff?}
    """
    if not target_path.exists():
        return {"drift": True, "reason": "file does not exist", "path": str(target_path)}

    text = target_path.read_text(encoding="utf-8", errors="replace")
    start_idx = text.find(ENGRAM_START)
    end_idx = text.find(ENGRAM_END, start_idx + 1) if start_idx != -1 else -1
    if start_idx == -1 or end_idx == -1:
        return {"drift": True, "reason": "no engram section present", "path": str(target_path)}

    body_start = text.find("\n", start_idx) + 1
    meta_end = _find_meta_end(text, body_start)
    section_body_start = meta_end if meta_end != -1 else body_start
    on_disk = text[section_body_start:end_idx].strip("\n")

    current = render_agents_md_section(conn).strip("\n")
    on_disk_sig = sign_section(on_disk, config)
    current_sig = sign_section(current, config)
    return {
        "drift": on_disk_sig != current_sig,
        "on_disk_signature": on_disk_sig,
        "current_signature": current_sig,
        "path": str(target_path),
    }


def render_drift_diff(conn: sqlite3.Connection, target_path: Path) -> str:
    """Return a unified-diff-style preview between on-disk and current."""
    import difflib

    text = target_path.read_text(encoding="utf-8", errors="replace") if target_path.exists() else ""
    start_idx = text.find(ENGRAM_START)
    end_idx = text.find(ENGRAM_END, start_idx + 1) if start_idx != -1 else -1
    if start_idx != -1 and end_idx != -1:
        body_start = text.find("\n", start_idx) + 1
        meta_end = _find_meta_end(text, body_start)
        body_start = meta_end if meta_end != -1 else body_start
        on_disk = text[body_start:end_idx]
    else:
        on_disk = ""

    current = render_agents_md_section(conn)
    diff = difflib.unified_diff(
        on_disk.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile=f"{target_path} (on disk)",
        tofile=f"{target_path} (current state)",
        n=2,
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_meta_end(text: str, body_start: int) -> int:
    """If the next line after ENGRAM_START is an engram:meta comment, return
    the position right after it. Else return -1."""
    eol = text.find("\n", body_start)
    if eol == -1:
        return -1
    line = text[body_start:eol].strip()
    if line.startswith(ENGRAM_META_PREFIX) and line.endswith(ENGRAM_META_SUFFIX):
        return eol + 1
    return -1


def _guess_kind(target_path: Path) -> str:
    name = target_path.name.lower()
    if name == "agents.md":
        return "agents-md"
    if name == "claude.md":
        return "claude-md"
    if name == "infra.md":
        return "infra-md"
    return "engram-md"
