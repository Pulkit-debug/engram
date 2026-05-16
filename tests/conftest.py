"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from engram.config import Config
from engram.db import open_db


@pytest.fixture()
def tmp_db(tmp_path) -> sqlite3.Connection:
    """A throw-away SQLite DB initialised with the engram schema."""
    cfg = Config(data_dir=tmp_path / "data", log_dir=tmp_path / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    conn = open_db(cfg)
    yield conn
    conn.close()


@pytest.fixture()
def tmp_cfg(tmp_path) -> Config:
    cfg = Config(data_dir=tmp_path / "data", log_dir=tmp_path / "logs",
                 watch_paths=[], embeddings_enabled=False)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def fixture_repo(tmp_path):
    """Build a minimal DevOps fixture in a tmp directory."""
    root = tmp_path / "repo"
    services = root / "services" / "payments"
    infra_prod = root / "infra" / "prod"
    services.mkdir(parents=True)
    infra_prod.mkdir(parents=True)
    (root / ".git").mkdir()

    (services / "Dockerfile").write_text(
        "FROM python:3.12-slim\nENV DATABASE_URL=\nEXPOSE 8080\n", encoding="utf-8",
    )
    (services / "main.py").write_text(
        "import os\nDATABASE_URL = os.environ['DATABASE_URL']\n", encoding="utf-8",
    )
    (infra_prod / "main.tf").write_text(
        'resource "aws_db_instance" "prod_db" {\n'
        '  identifier = "prod-db"\n'
        '  engine = "postgres"\n'
        '  tags = { Environment = "production" }\n'
        '}\n',
        encoding="utf-8",
    )
    k8s = infra_prod / "k8s"
    k8s.mkdir()
    (k8s / "payments.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: payments\n"
        "  namespace: prod\n"
        "  labels:\n"
        "    app: payments\n"
        "    environment: production\n"
        "spec:\n"
        "  replicas: 2\n",
        encoding="utf-8",
    )
    return root
