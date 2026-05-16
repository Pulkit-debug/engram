"""Targeted extractor tests — each pins one contract."""

from pathlib import Path

from engram.extractors.compose_ext import DockerComposeExtractor
from engram.extractors.devops_ext import (
    EnvFileExtractor, JenkinsfileExtractor, MakefileExtractor, ShellExtractor,
)
from engram.extractors.dockerfile import DockerfileExtractor
from engram.extractors.helm_ext import HelmChartExtractor
from engram.extractors.python_ext import PythonExtractor
from engram.extractors.terraform_ext import TerraformExtractor
from engram.extractors.yaml_ext import YAMLExtractor


def test_dockerfile_extracts_env_port_and_image():
    content = (
        "FROM python:3.12-slim\n"
        "ENV DATABASE_URL=postgres://x\n"
        "ENV STRIPE_KEY=\n"
        "EXPOSE 8080\n"
        "CMD [\"python\", \"app.py\"]\n"
    )
    res = DockerfileExtractor().extract(Path("services/foo/Dockerfile"), content)
    assert any(e.entity_type == "env_var" and e.name == "DATABASE_URL" for e in res.entities)
    assert any(e.entity_type == "port" and e.name == "8080" for e in res.entities)
    assert any(r.kind == "docker:image" for r in res.resources)
    assert "docker" in res.technologies


def test_terraform_extracts_resources_and_depends_on():
    content = (
        'provider "aws" {}\n'
        'resource "aws_db_instance" "prod_db" {\n'
        '  identifier = "prod-db"\n'
        '  engine     = "postgres"\n'
        '  tags = { Environment = "production" }\n'
        '}\n'
        'resource "aws_ecs_service" "api" {\n'
        '  name       = "api"\n'
        '  depends_on = [aws_db_instance.prod_db]\n'
        '}\n'
    )
    res = TerraformExtractor().extract(Path("infra/prod/main.tf"), content)
    names = {r.name for r in res.resources}
    assert {"prod_db", "api"} <= names
    by_name = {r.name: r for r in res.resources}
    assert by_name["prod_db"].environment == "production"
    # `api` should DEPENDS_ON `prod_db` (explicit edge).
    assert any(
        e.rel_type == "DEPENDS_ON" and e.source_name == "api" and e.target_name == "prod_db"
        for e in res.edges
    )


def test_yaml_k8s_extracts_deployment_with_env_marker():
    content = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: payments\n"
        "  namespace: prod\n"
        "  labels:\n"
        "    environment: production\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: app\n"
        "        image: payments:1.0\n"
        "        env:\n"
        "        - name: DATABASE_URL\n"
        "          value: x\n"
    )
    res = YAMLExtractor().extract(Path("k8s/payments.yaml"), content)
    assert any(r.kind == "k8s:Deployment" and r.name == "payments" for r in res.resources)
    assert any(r.environment == "production" for r in res.resources)
    assert any(e.name == "DATABASE_URL" for e in res.entities)


def test_compose_extracts_services_and_depends_on():
    content = (
        "services:\n"
        "  web:\n"
        "    image: nginx:1.25\n"
        "    depends_on: [db]\n"
        "    environment:\n"
        "      DATABASE_URL: x\n"
        "  db:\n"
        "    image: postgres:15\n"
    )
    res = DockerComposeExtractor().extract(Path("compose.yml"), content)
    kinds = {r.name for r in res.resources}
    assert {"web", "db"} <= kinds
    assert any(e.rel_type == "DEPENDS_ON" and e.source_name == "web" and e.target_name == "db"
               for e in res.edges)


def test_helm_chart_extracts_metadata_and_deps():
    content = (
        "apiVersion: v2\n"
        "name: payments-chart\n"
        "version: 1.0.0\n"
        "appVersion: 1.2.3\n"
        "type: application\n"
        "dependencies:\n"
        "  - name: postgresql\n"
        "    version: 12.0.0\n"
        "    repository: https://charts.bitnami.com/bitnami\n"
    )
    res = HelmChartExtractor().extract(Path("chart/Chart.yaml"), content)
    assert any(r.kind == "helm:chart" for r in res.resources)
    assert any(
        e.target_name == "postgresql" and e.rel_type == "DEPENDS_ON" for e in res.edges
    )


def test_jenkinsfile_extracts_stages_and_envs():
    content = (
        "pipeline {\n"
        "  agent { docker { image 'python:3.12-slim' } }\n"
        "  environment { DATABASE_URL = 'postgres://x' }\n"
        "  stages {\n"
        "    stage('Build') { steps { sh 'echo' } }\n"
        "    stage('Deploy') { steps { sh 'kubectl apply' } }\n"
        "  }\n"
        "}\n"
    )
    res = JenkinsfileExtractor().extract(Path("Jenkinsfile"), content)
    stage_names = {r.name for r in res.resources if r.kind == "jenkins:stage"}
    assert stage_names == {"Build", "Deploy"}
    assert any(e.name == "DATABASE_URL" and e.entity_type == "env_var" for e in res.entities)


def test_makefile_extracts_targets():
    content = (
        "TARGET=foo\n"
        "all: build test\n"
        "build:\n"
        "\tgcc -o foo foo.c\n"
        "test:\n"
        "\t./foo --self-test\n"
    )
    res = MakefileExtractor().extract(Path("Makefile"), content)
    targets = {r.name for r in res.resources if r.kind == "make:target"}
    assert {"all", "build", "test"} <= targets


def test_shell_extracts_exports_and_refs():
    content = (
        "#!/bin/bash\n"
        "export DATABASE_URL=postgres://x\n"
        "echo $STRIPE_KEY\n"
        "source ./common.sh\n"
    )
    res = ShellExtractor().extract(Path("deploy.sh"), content)
    assert any(e.name == "DATABASE_URL" and e.entity_type == "env_var" for e in res.entities)
    assert any(e.name == "STRIPE_KEY" and e.entity_type == "env_ref" for e in res.entities)
    assert any(e.rel_type == "SOURCES" and e.target_name == "./common.sh" for e in res.edges)


def test_env_file_extracts_pairs():
    content = "FOO=bar\n# comment\nBAZ=\"quoted value\"\nLOG_LEVEL=INFO\n"
    res = EnvFileExtractor().extract(Path(".env"), content)
    by_name = {e.name: e for e in res.entities if e.entity_type == "env_var"}
    assert by_name["FOO"].value == "bar"
    assert by_name["BAZ"].value == "quoted value"
    assert "LOG_LEVEL" in by_name


def test_python_extracts_env_refs_and_imports():
    content = (
        "import os\n"
        "from typing import Any\n"
        "def fn():\n"
        "    return os.environ['DATABASE_URL']\n"
        "class X:\n    pass\n"
    )
    res = PythonExtractor().extract(Path("svc.py"), content)
    assert any(e.entity_type == "env_ref" and e.name == "DATABASE_URL" for e in res.entities)
    assert any(e.entity_type == "function" and e.name == "fn" for e in res.entities)
    assert any(e.entity_type == "class" and e.name == "X" for e in res.entities)
