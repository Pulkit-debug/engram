"""Tests for the CloudFormation extractor."""

from __future__ import annotations

from pathlib import Path

from engram.extractors.cloudformation_ext import CloudFormationExtractor
from engram.extractors.yaml_ext import YAMLExtractor


SIMPLE_CFN = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: payments service infrastructure

Resources:
  PaymentsDb:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceIdentifier: payments-prod-db
      Engine: postgres
      DBInstanceClass: db.r5.large
      MultiAZ: true
      Tags:
        - Key: Environment
          Value: production
        - Key: Owner
          Value: platform

  PaymentsRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: payments-prod-task-role
"""


CFN_WITH_REFS = """\
AWSTemplateFormatVersion: '2010-09-09'

Resources:
  Vpc:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16

  PrivateSubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      CidrBlock: 10.0.1.0/24

  WebServer:
    Type: AWS::EC2::Instance
    DependsOn:
      - PrivateSubnet
    Properties:
      SubnetId: !Ref PrivateSubnet
      InstanceType: t3.medium
"""


CFN_JSON = """\
{
  "AWSTemplateFormatVersion": "2010-09-09",
  "Resources": {
    "PaymentsLambda": {
      "Type": "AWS::Lambda::Function",
      "Properties": {
        "FunctionName": "payments-prod-handler",
        "Runtime": "python3.12",
        "Handler": "main.handler"
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

def test_extracts_resources_with_friendly_names():
    res = CloudFormationExtractor().extract(Path("template.yaml"), SIMPLE_CFN)
    by_name = {r.name: r for r in res.resources}
    assert "payments-prod-db" in by_name
    assert by_name["payments-prod-db"].kind == "tf:aws_db_instance"
    assert by_name["payments-prod-db"].environment == "production"
    assert "payments-prod-task-role" in by_name
    assert by_name["payments-prod-task-role"].kind == "tf:aws_iam_role"


def test_env_from_tags():
    res = CloudFormationExtractor().extract(Path("t.yaml"), SIMPLE_CFN)
    db = next(r for r in res.resources if r.name == "payments-prod-db")
    assert db.environment == "production"


def test_env_from_name_fallback():
    cfn = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Db:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceIdentifier: api-staging-db
"""
    res = CloudFormationExtractor().extract(Path("t.yaml"), cfn)
    db = res.resources[0]
    assert db.environment == "staging"


def test_logical_id_used_when_no_name_property():
    cfn = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  MyNamelessThing:
    Type: AWS::CloudWatch::Dashboard
"""
    res = CloudFormationExtractor().extract(Path("t.yaml"), cfn)
    assert res.resources[0].name == "MyNamelessThing"


# ---------------------------------------------------------------------------
# Cross-resource edges
# ---------------------------------------------------------------------------

def test_ref_creates_depends_on_edge():
    res = CloudFormationExtractor().extract(Path("net.yaml"), CFN_WITH_REFS)
    edges = res.edges
    # WebServer → PrivateSubnet (via Ref), PrivateSubnet → Vpc (via Ref),
    # WebServer → PrivateSubnet (via DependsOn).
    edge_pairs = {(e.source_name, e.target_name) for e in edges}
    assert ("WebServer", "PrivateSubnet") in edge_pairs
    assert ("PrivateSubnet", "Vpc") in edge_pairs


def test_depends_on_property_creates_edge():
    res = CloudFormationExtractor().extract(Path("net.yaml"), CFN_WITH_REFS)
    deps_via_dependson = [
        e for e in res.edges
        if e.properties.get("via") == "cfn_dependson"
    ]
    assert len(deps_via_dependson) >= 1


# ---------------------------------------------------------------------------
# JSON variant
# ---------------------------------------------------------------------------

def test_extracts_from_json_template():
    res = CloudFormationExtractor().extract(Path("template.json"), CFN_JSON)
    assert len(res.resources) == 1
    r = res.resources[0]
    assert r.name == "payments-prod-handler"
    assert r.kind == "tf:aws_lambda_function"
    assert r.environment == "production"


# ---------------------------------------------------------------------------
# YAMLExtractor routing
# ---------------------------------------------------------------------------

def test_yaml_extractor_routes_cfn_via_template_version():
    res = YAMLExtractor().extract(Path("template.yaml"), SIMPLE_CFN)
    # Should route through CFN extractor; tf:aws_db_instance kind tells us.
    kinds = {r.kind for r in res.resources}
    assert "tf:aws_db_instance" in kinds


def test_yaml_extractor_routes_cfn_via_resources_dict():
    """Even without AWSTemplateFormatVersion, a Resources: dict with Type:
    fields should route to CFN."""
    cfn_no_version = """\
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: prod-uploads
"""
    res = YAMLExtractor().extract(Path("template.yaml"), cfn_no_version)
    kinds = {r.kind for r in res.resources}
    assert "tf:aws_s3_bucket" in kinds


def test_yaml_extractor_does_not_misroute_k8s_as_cfn():
    """A K8s manifest must NOT be treated as CFN, even though it has top-level
    keys like 'kind' and 'metadata'."""
    k8s = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
spec:
  replicas: 3
"""
    res = YAMLExtractor().extract(Path("k8s.yaml"), k8s)
    kinds = {r.kind for r in res.resources}
    assert "k8s:Deployment" in kinds
    assert not any(k.startswith("tf:") for k in kinds)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_malformed_template_does_not_crash():
    res = CloudFormationExtractor().extract(Path("t.yaml"), "Resources: {\n")
    assert isinstance(res.resources, list)


def test_unknown_cfn_type_still_extracted():
    """A type we haven't mapped should still appear as cfn:<type>."""
    cfn = """\
Resources:
  Weird:
    Type: AWS::SomeNew::Service
    Properties:
      Name: weird-thing
"""
    res = CloudFormationExtractor().extract(Path("t.yaml"), cfn)
    assert len(res.resources) == 1
    assert res.resources[0].kind == "cfn:AWS::SomeNew::Service"


# ---------------------------------------------------------------------------
# End-to-end with blast_radius
# ---------------------------------------------------------------------------

def test_cfn_prod_resource_blocks_via_blast_radius(tmp_db):
    """Index a CFN template, then assess `terraform destroy` on it → BLOCK."""
    from engram.crawler import index_paths
    from engram.config import Config
    from engram.safety.blast_radius import assess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()  # mark as project root
        (repo / "template.yaml").write_text(SIMPLE_CFN, encoding="utf-8")

        cfg = Config(data_dir=Path(tmp) / "data", log_dir=Path(tmp) / "logs",
                     watch_paths=[repo], embeddings_enabled=False)
        cfg.ensure_dirs()
        index_paths(tmp_db, cfg)

    result = assess(tmp_db, "terraform destroy", "payments-prod-db")
    assert result.action == "block"
    assert result.environment == "production"
    assert result.risk_tier == "red"
