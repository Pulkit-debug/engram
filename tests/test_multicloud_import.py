"""Tests for the GCP + Azure importers — multi-cloud parity with AWS.

Same mocked-subprocess pattern; no live cloud calls."""

from __future__ import annotations

import json
from types import SimpleNamespace


def _mk(stdout: str, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# GCP — _gcloud_json shape: each subcommand returns a JSON list
# ---------------------------------------------------------------------------

def _patch_gcp(monkeypatch, responses: dict[tuple[str, ...], list | dict]):
    def fake_run(argv, capture_output=False, text=False, timeout=None):
        # `gcloud config get-value project` is special.
        if "config" in argv and "get-value" in argv:
            return _mk("my-project\n")
        # `bq ls` is the special case for bigquery.
        if argv[0] == "bq":
            return _mk(json.dumps(responses.get(("bq", "ls"), [])))
        # Drop --project, --format=json, --quiet flags and their values.
        cmd: list[str] = []
        i = 1
        while i < len(argv):
            a = argv[i]
            if a.startswith("--"):
                if "=" in a:
                    i += 1
                else:
                    i += 2
                continue
            cmd.append(a)
            i += 1
        key = tuple(cmd)
        if key in responses:
            return _mk(json.dumps(responses[key]))
        for k, v in responses.items():
            if key[:len(k)] == k:
                return _mk(json.dumps(v))
        return _mk("[]")

    monkeypatch.setattr("engram.cloud.gcp_import.subprocess.run", fake_run)
    monkeypatch.setattr("engram.cloud.gcp_import.shutil.which",
                        lambda x: "/usr/local/bin/gcloud")


def test_gcp_compute_instance_with_prod_label(tmp_db, monkeypatch):
    from engram.cloud.gcp_import import import_gcp
    _patch_gcp(monkeypatch, {
        ("compute", "instances", "list"): [{
            "name": "api-prod-1",
            "zone": "projects/my-project/zones/us-central1-a",
            "machineType": "projects/my-project/zones/us-central1-a/machineTypes/n2-standard-4",
            "status": "RUNNING",
            "labels": {"environment": "production", "service": "api"},
            "networkInterfaces": [{"networkIP": "10.0.0.5"}],
        }],
        ("compute", "disks", "list"): [],
    })
    import_gcp(tmp_db, kinds=["compute"], project="my-project")
    rows = tmp_db.execute(
        "SELECT name, kind, environment, risk_tier FROM resource"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "api-prod-1"
    assert rows[0]["kind"] == "gcp:compute:Instance"
    assert rows[0]["environment"] == "production"
    assert rows[0]["risk_tier"] == "red"


def test_gcp_cloud_sql(tmp_db, monkeypatch):
    from engram.cloud.gcp_import import import_gcp
    _patch_gcp(monkeypatch, {
        ("sql", "instances", "list"): [{
            "name": "payments-prod-db",
            "databaseVersion": "POSTGRES_14",
            "settings": {"tier": "db-custom-4-15360"},
            "state": "RUNNABLE",
            "region": "us-central1",
            "ipAddresses": [{"type": "PRIMARY", "ipAddress": "34.71.10.5"}],
            "connectionName": "my-project:us-central1:payments-prod-db",
        }],
    })
    import_gcp(tmp_db, kinds=["sql"], project="my-project")
    row = tmp_db.execute(
        "SELECT name, environment, risk_tier, properties FROM resource"
    ).fetchone()
    assert row["name"] == "payments-prod-db"
    assert row["environment"] == "production"
    props = json.loads(row["properties"])
    assert props["endpoint"] == "34.71.10.5"
    assert props["database_version"] == "POSTGRES_14"


def test_gcp_storage_buckets(tmp_db, monkeypatch):
    from engram.cloud.gcp_import import import_gcp
    _patch_gcp(monkeypatch, {
        ("storage", "buckets", "list"): [
            {"name": "prod-uploads", "location": "US",
             "storageClass": "STANDARD", "timeCreated": "2024-01-01",
             "versioning": {"enabled": True}, "labels": {}},
            {"name": "dev-test", "location": "US",
             "storageClass": "STANDARD", "timeCreated": "2024-01-01",
             "labels": {}},
        ],
    })
    import_gcp(tmp_db, kinds=["storage"], project="my-project")
    rows = tmp_db.execute(
        "SELECT name, environment FROM resource ORDER BY name"
    ).fetchall()
    assert len(rows) == 2
    by_name = {r["name"]: r["environment"] for r in rows}
    assert by_name["prod-uploads"] == "production"
    assert by_name["dev-test"] == "dev"


def test_gcp_gke_with_node_pools(tmp_db, monkeypatch):
    from engram.cloud.gcp_import import import_gcp
    _patch_gcp(monkeypatch, {
        ("container", "clusters", "list"): [{
            "name": "prod-cluster",
            "location": "us-central1",
            "endpoint": "1.2.3.4",
            "currentNodeCount": 5,
            "currentMasterVersion": "1.28.5",
            "status": "RUNNING",
            "resourceLabels": {"environment": "production"},
            "nodePools": [{
                "name": "default-pool",
                "initialNodeCount": 3,
                "config": {"machineType": "n2-standard-4"},
                "status": "RUNNING",
            }],
        }],
    })
    import_gcp(tmp_db, kinds=["gke"], project="my-project")
    kinds = {r["kind"] for r in tmp_db.execute("SELECT kind FROM resource").fetchall()}
    assert "gcp:container:Cluster" in kinds
    assert "gcp:container:NodePool" in kinds


def test_gcp_pubsub_topics_and_subscriptions(tmp_db, monkeypatch):
    from engram.cloud.gcp_import import import_gcp
    _patch_gcp(monkeypatch, {
        ("pubsub", "topics", "list"): [
            {"name": "projects/my-project/topics/payments-events"},
        ],
        ("pubsub", "subscriptions", "list"): [
            {"name": "projects/my-project/subscriptions/payments-events-sub",
             "topic": "projects/my-project/topics/payments-events",
             "ackDeadlineSeconds": 10},
        ],
    })
    import_gcp(tmp_db, kinds=["pubsub"], project="my-project")
    kinds = {r["kind"] for r in tmp_db.execute("SELECT kind FROM resource").fetchall()}
    assert "gcp:pubsub:Topic" in kinds
    assert "gcp:pubsub:Subscription" in kinds


def test_gcp_handles_cli_missing(tmp_db, monkeypatch):
    from engram.cloud.gcp_import import import_gcp
    monkeypatch.setattr("engram.cloud.gcp_import.shutil.which", lambda x: None)
    stats = import_gcp(tmp_db, kinds=["compute"], project="my-project")
    assert stats.inserted == 0
    assert any("gcloud CLI not found" in r[1] for r in stats.errors)


def test_gcp_blast_radius_on_prod_resource(tmp_db, monkeypatch):
    """End-to-end: a GCP-discovered prod SQL → BLOCK on delete."""
    from engram.cloud.gcp_import import import_gcp
    from engram.safety.blast_radius import assess

    _patch_gcp(monkeypatch, {
        ("sql", "instances", "list"): [{
            "name": "payments-prod-db",
            "databaseVersion": "POSTGRES_14",
            "settings": {"tier": "db-custom-4-15360"},
            "state": "RUNNABLE",
            "region": "us-central1",
            "ipAddresses": [{"type": "PRIMARY", "ipAddress": "34.71.10.5"}],
        }],
    })
    import_gcp(tmp_db, kinds=["sql"], project="my-project")

    result = assess(tmp_db, "gcloud sql instances delete", "payments-prod-db")
    assert result.environment == "production"
    assert result.action == "block"
    assert result.risk_tier == "red"


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------

def _patch_azure(monkeypatch, responses: dict[tuple[str, ...], list | dict]):
    def fake_run(argv, capture_output=False, text=False, timeout=None):
        if "account" in argv and "show" in argv:
            return _mk("00000000-0000-0000-0000-000000000001\n")
        cmd: list[str] = []
        i = 1
        while i < len(argv):
            a = argv[i]
            if a.startswith("--"):
                i += 2
                continue
            cmd.append(a)
            i += 1
        key = tuple(cmd)
        if key in responses:
            return _mk(json.dumps(responses[key]))
        for k, v in responses.items():
            if key[:len(k)] == k:
                return _mk(json.dumps(v))
        return _mk("[]")

    monkeypatch.setattr("engram.cloud.azure_import.subprocess.run", fake_run)
    monkeypatch.setattr("engram.cloud.azure_import.shutil.which",
                        lambda x: "/usr/local/bin/az")


def test_azure_resource_groups(tmp_db, monkeypatch):
    from engram.cloud.azure_import import import_azure
    _patch_azure(monkeypatch, {
        ("group", "list"): [{
            "name": "rg-payments-prod",
            "id": "/subscriptions/1/resourceGroups/rg-payments-prod",
            "location": "eastus",
            "tags": {"Environment": "production"},
        }],
    })
    import_azure(tmp_db, kinds=["rg"], subscription="1")
    row = tmp_db.execute(
        "SELECT name, kind, environment, risk_tier FROM resource"
    ).fetchone()
    assert row["name"] == "rg-payments-prod"
    assert row["kind"] == "azure:resources:ResourceGroup"
    assert row["environment"] == "production"
    assert row["risk_tier"] == "red"


def test_azure_vm_and_disk(tmp_db, monkeypatch):
    from engram.cloud.azure_import import import_azure
    _patch_azure(monkeypatch, {
        ("vm", "list"): [{
            "name": "web-prod-1",
            "id": "/subscriptions/1/.../web-prod-1",
            "location": "eastus",
            "hardwareProfile": {"vmSize": "Standard_D4s_v3"},
            "storageProfile": {"osDisk": {"osType": "Linux"}},
            "tags": {"Environment": "Production"},
        }],
        ("disk", "list"): [{
            "name": "web-prod-1-os-disk",
            "id": "/subscriptions/1/.../web-prod-1-os-disk",
            "diskSizeGb": 128,
            "sku": {"name": "Premium_LRS"},
            "location": "eastus",
            "tags": {},
        }],
    })
    import_azure(tmp_db, kinds=["vm"], subscription="1")
    kinds = {r["kind"] for r in tmp_db.execute("SELECT kind FROM resource").fetchall()}
    assert "azure:compute:VirtualMachine" in kinds
    assert "azure:compute:Disk" in kinds


def test_azure_cosmosdb(tmp_db, monkeypatch):
    from engram.cloud.azure_import import import_azure
    _patch_azure(monkeypatch, {
        ("cosmosdb", "list"): [{
            "name": "prod-orders-cosmos",
            "id": "/subscriptions/1/.../prod-orders-cosmos",
            "documentEndpoint": "https://prod-orders-cosmos.documents.azure.com:443/",
            "kind": "MongoDB",
            "location": "eastus",
            "tags": {"env": "prod"},
        }],
    })
    import_azure(tmp_db, kinds=["cosmosdb"], subscription="1")
    row = tmp_db.execute(
        "SELECT name, environment, properties FROM resource"
    ).fetchone()
    assert row["name"] == "prod-orders-cosmos"
    assert row["environment"] == "production"


def test_azure_aks(tmp_db, monkeypatch):
    from engram.cloud.azure_import import import_azure
    _patch_azure(monkeypatch, {
        ("aks", "list"): [{
            "name": "prod-aks",
            "id": "/subscriptions/1/.../prod-aks",
            "kubernetesVersion": "1.28.5",
            "location": "eastus",
            "fqdn": "prod-aks-xxx.hcp.eastus.azmk8s.io",
            "agentPoolProfiles": [{"name": "default"}, {"name": "spot"}],
            "tags": {"Environment": "production"},
        }],
    })
    import_azure(tmp_db, kinds=["aks"], subscription="1")
    row = tmp_db.execute(
        "SELECT name, properties FROM resource"
    ).fetchone()
    assert row["name"] == "prod-aks"
    props = json.loads(row["properties"])
    assert props["node_pool_count"] == 2


def test_azure_keyvault(tmp_db, monkeypatch):
    from engram.cloud.azure_import import import_azure
    _patch_azure(monkeypatch, {
        ("keyvault", "list"): [{
            "name": "prod-secrets-kv",
            "id": "/subscriptions/1/.../prod-secrets-kv",
            "properties": {"vaultUri": "https://prod-secrets-kv.vault.azure.net/"},
            "location": "eastus",
            "tags": {"Environment": "production"},
        }],
    })
    import_azure(tmp_db, kinds=["keyvault"], subscription="1")
    row = tmp_db.execute(
        "SELECT name, environment, risk_tier FROM resource"
    ).fetchone()
    assert row["name"] == "prod-secrets-kv"
    assert row["environment"] == "production"
    assert row["risk_tier"] == "red"


def test_azure_handles_cli_missing(tmp_db, monkeypatch):
    from engram.cloud.azure_import import import_azure
    monkeypatch.setattr("engram.cloud.azure_import.shutil.which", lambda x: None)
    stats = import_azure(tmp_db, kinds=["vm"], subscription="1")
    assert stats.inserted == 0
    assert any("az CLI not found" in r[1] for r in stats.errors)


def test_azure_blast_radius_on_prod_keyvault(tmp_db, monkeypatch):
    """End-to-end: az keyvault delete on prod keyvault → BLOCK."""
    from engram.cloud.azure_import import import_azure
    from engram.safety.blast_radius import assess

    _patch_azure(monkeypatch, {
        ("keyvault", "list"): [{
            "name": "prod-secrets-kv",
            "id": "/subscriptions/1/.../prod-secrets-kv",
            "properties": {"vaultUri": "https://prod-secrets-kv.vault.azure.net/"},
            "location": "eastus",
            "tags": {"Environment": "production"},
        }],
    })
    import_azure(tmp_db, kinds=["keyvault"], subscription="1")

    result = assess(tmp_db, "az keyvault delete", "prod-secrets-kv")
    assert result.action == "block"
    assert result.risk_tier == "red"
