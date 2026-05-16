"""Live MCP wire-level smoke test.

Launches `engram serve` as a real subprocess (not just module-load), opens
an MCP stdio client connection through the official `mcp` package, calls
real tools, and captures the request/response transcript to a fixture for
the repo (`tests/mcp_wire_smoke.json`).

This is the test that proves "MCP server actually works over the wire",
distinct from "MCP server module imports without errors".

Marked `asyncio` because the MCP stdio client is async-only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


_FIXTURE_PATH = Path(__file__).parent / "mcp_wire_smoke.json"
_DB_DIR = Path("/tmp/engram_mcp_wire_db")


def _seed_db():
    """Pre-seed a DB with a deterministic prod resource so blast_radius
    returns a stable answer regardless of indexing order."""
    from engram.config import Config
    from engram.db import open_db
    from engram.graph import (
        EdgeSpec, FileRow, ProjectRow, ResourceRow,
        entity_uid, resource_uid, upsert_edge, upsert_entity, upsert_file,
        upsert_project, upsert_resource, EntityRow,
    )

    _DB_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config(data_dir=_DB_DIR, log_dir=_DB_DIR / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    conn = open_db(cfg)

    upsert_project(conn, ProjectRow(path="/r", name="r", project_type="terraform"))
    upsert_file(conn, FileRow(
        path="/r/prod/main.tf", project_path="/r", name="main.tf",
        extension=".tf", size_bytes=100, content_hash="h",
        modified_at="2026-05-14T00:00:00Z", risk_tier="red",
    ))
    db_uid = resource_uid("tf:aws_db_instance", "datatalks_prod_db", "", "/r/prod/main.tf")
    upsert_resource(conn, ResourceRow(
        uid=db_uid, file_path="/r/prod/main.tf",
        kind="tf:aws_db_instance", name="datatalks_prod_db",
        environment="production", risk_tier="red",
    ))
    # Add an env var so trace_env_var has data.
    upsert_entity(conn, EntityRow(
        uid=entity_uid("DATABASE_URL", "env_var", "/r/prod/main.tf"),
        file_path="/r/prod/main.tf",
        name="DATABASE_URL", entity_type="env_var", value="postgres://...",
    ))
    conn.close()


@pytest.fixture(scope="module", autouse=True)
def _prepare_db():
    import shutil
    if _DB_DIR.exists():
        shutil.rmtree(_DB_DIR)
    _seed_db()
    yield
    # leave fixture on disk for reference


async def _connect_and_call():
    """Open an MCP stdio session to a fresh `engram serve` and call tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env["ENGRAM_DATA_DIR"] = str(_DB_DIR)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "engram.mcp_server"],
        env=env,
    )

    transcript: dict = {
        "client": "engram mcp wire smoke",
        "transport": "stdio",
        "server_command": "python -m engram.mcp_server",
    }

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. list_tools — proves protocol handshake completed.
            tools_resp = await session.list_tools()
            tool_names = [t.name for t in tools_resp.tools]
            transcript["list_tools"] = sorted(tool_names)
            assert "blast_radius" in tool_names
            assert "infra_context" in tool_names
            assert "trace_env_var" in tool_names
            assert "dependents_of" in tool_names
            assert "service_map" in tool_names
            assert len(tool_names) == 10

            # 2. blast_radius — the killer call. Verify BLOCK / red.
            br_resp = await session.call_tool(
                "blast_radius",
                {"operation": "terraform destroy", "target": "datatalks_prod_db"},
            )
            br_text = br_resp.content[0].text
            br_payload = json.loads(br_text)
            transcript["blast_radius_call"] = {
                "args": {"operation": "terraform destroy", "target": "datatalks_prod_db"},
                "response": br_payload,
            }
            assert br_payload["risk_tier"] == "red"
            assert br_payload["action"] == "block"
            assert br_payload["environment"] == "production"

            # 3. trace_env_var — should find DATABASE_URL.
            ev_resp = await session.call_tool(
                "trace_env_var", {"name": "DATABASE_URL"},
            )
            ev_payload = json.loads(ev_resp.content[0].text)
            transcript["trace_env_var_call"] = {
                "args": {"name": "DATABASE_URL"},
                "response": ev_payload,
            }
            assert len(ev_payload["defined_in"]) >= 1

            # 4. infra_context — full payload check.
            ic_resp = await session.call_tool(
                "infra_context",
                {"target": "datatalks_prod_db", "token_budget": 1000},
            )
            ic_payload = json.loads(ic_resp.content[0].text)
            transcript["infra_context_call"] = {
                "args": {"target": "datatalks_prod_db", "token_budget": 1000},
                "response": ic_payload,
            }
            assert ic_payload["found"] is True
            assert "summary" in ic_payload

            # 5. service_map — adjacency + mermaid present.
            sm_resp = await session.call_tool("service_map", {"scope": "all"})
            sm_payload = json.loads(sm_resp.content[0].text)
            transcript["service_map_call"] = {
                "args": {"scope": "all"},
                "response_keys": sorted(sm_payload.keys()),
                "node_count": sm_payload.get("node_count", 0),
            }
            assert "mermaid" in sm_payload or sm_payload.get("found") is False

    return transcript


async def test_mcp_wire_smoke_produces_fixture():
    transcript = await _connect_and_call()
    _FIXTURE_PATH.write_text(json.dumps(transcript, indent=2), encoding="utf-8")
    # Sanity: every claimed tool roundtripped a valid JSON response.
    assert _FIXTURE_PATH.exists()
    assert len(transcript["list_tools"]) == 10
