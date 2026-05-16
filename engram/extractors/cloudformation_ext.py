"""CloudFormation template extractor.

CloudFormation templates are YAML or JSON dicts with a top-level `Resources`
section mapping logical names → resource definitions:

  Resources:
    PaymentsDb:
      Type: AWS::RDS::DBInstance
      Properties:
        DBInstanceIdentifier: payments-prod
        Tags:
          - Key: Environment
            Value: production

We emit:
  * one ExtractedResource per Resources entry, kind = "tf:<aws_type>"
    (we reuse the tf: prefix so cross-format detection joins with
    Terraform-defined resources).
  * Tags become entities + env-from-tags inference.
  * `Ref` and `Fn::GetAtt` references become DEPENDS_ON edges (best-effort).

YAML CFN uses short-form intrinsics (`!Ref`, `!GetAtt`, `!Sub`); we treat
these as opaque strings unless they're simple references we can parse.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractedResource,
    ExtractionResult,
)


# AWS::Service::Resource → tf:aws_resource_kind (so cross-format hits Terraform).
_TYPE_PREFIX_MAP = {
    "AWS::RDS::DBInstance":              "tf:aws_db_instance",
    "AWS::RDS::DBCluster":               "tf:aws_rds_cluster",
    "AWS::EC2::Instance":                "tf:aws_instance",
    "AWS::EC2::Volume":                  "tf:aws_ebs_volume",
    "AWS::EC2::VPC":                     "tf:aws_vpc",
    "AWS::EC2::Subnet":                  "tf:aws_subnet",
    "AWS::EC2::SecurityGroup":           "tf:aws_security_group",
    "AWS::Lambda::Function":             "tf:aws_lambda_function",
    "AWS::S3::Bucket":                   "tf:aws_s3_bucket",
    "AWS::ECS::Cluster":                 "tf:aws_ecs_cluster",
    "AWS::ECS::Service":                 "tf:aws_ecs_service",
    "AWS::ECS::TaskDefinition":          "tf:aws_ecs_task_definition",
    "AWS::EKS::Cluster":                 "tf:aws_eks_cluster",
    "AWS::IAM::Role":                    "tf:aws_iam_role",
    "AWS::IAM::Policy":                  "tf:aws_iam_policy",
    "AWS::SecretsManager::Secret":       "tf:aws_secretsmanager_secret",
    "AWS::SSM::Parameter":               "tf:aws_ssm_parameter",
    "AWS::Route53::HostedZone":          "tf:aws_route53_zone",
    "AWS::CloudFront::Distribution":     "tf:aws_cloudfront_distribution",
    "AWS::ApiGateway::RestApi":          "tf:aws_api_gateway_rest_api",
    "AWS::ApiGatewayV2::Api":            "tf:aws_apigatewayv2_api",
    "AWS::AutoScaling::AutoScalingGroup": "tf:aws_autoscaling_group",
    "AWS::AutoScaling::LaunchConfiguration": "tf:aws_launch_configuration",
    "AWS::EC2::LaunchTemplate":          "tf:aws_launch_template",
    "AWS::ElastiCache::CacheCluster":    "tf:aws_elasticache_cluster",
    "AWS::ElastiCache::ReplicationGroup": "tf:aws_elasticache_replication_group",
    "AWS::Logs::LogGroup":               "tf:aws_cloudwatch_log_group",
    "AWS::CloudWatch::Alarm":            "tf:aws_cloudwatch_metric_alarm",
    "AWS::Events::Rule":                 "tf:aws_cloudwatch_event_rule",
    "AWS::StepFunctions::StateMachine":  "tf:aws_sfn_state_machine",
    "AWS::KMS::Key":                     "tf:aws_kms_key",
    "AWS::CertificateManager::Certificate": "tf:aws_acm_certificate",
    "AWS::Cognito::UserPool":            "tf:aws_cognito_user_pool",
    "AWS::Kinesis::Stream":              "tf:aws_kinesis_stream",
    "AWS::OpenSearchService::Domain":    "tf:aws_opensearch_domain",
    "AWS::Redshift::Cluster":            "tf:aws_redshift_cluster",
    "AWS::WAFv2::WebACL":                "tf:aws_wafv2_web_acl",
    "AWS::DynamoDB::Table":              "tf:aws_dynamodb_table",
    "AWS::SQS::Queue":                   "tf:aws_sqs_queue",
    "AWS::SNS::Topic":                   "tf:aws_sns_topic",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "tf:aws_lb",
    "AWS::ElasticLoadBalancingV2::TargetGroup":  "tf:aws_lb_target_group",
}


# Match Ref / GetAtt references to other logical IDs in the template.
_REF_RE = re.compile(r'!Ref\s+([A-Z][A-Za-z0-9_]*)|"Ref"\s*:\s*"([A-Z][A-Za-z0-9_]*)"')
_GETATT_RE = re.compile(r'!GetAtt\s+([A-Z][A-Za-z0-9_]*)\.|"Fn::GetAtt"\s*:\s*\[\s*"([A-Z][A-Za-z0-9_]*)"')


class CloudFormationExtractor(BaseExtractor):
    """Parse a CloudFormation template (YAML or JSON)."""

    def extract(self, path: Path, content: str) -> ExtractionResult:
        result = ExtractionResult(file_path=str(path))
        result.technologies.append("cloudformation")
        result.technologies.append("aws")

        # Try YAML first (most common); fall back to JSON.
        template = _parse_template(content)
        if not template or not isinstance(template, dict):
            return result

        resources = template.get("Resources")
        if not isinstance(resources, dict):
            return result

        # Track logical ID → resource name for cross-resource edges.
        logical_to_name: dict[str, str] = {}

        for logical_id, res_def in resources.items():
            if not isinstance(res_def, dict):
                continue
            cfn_type = res_def.get("Type", "")
            kind = _TYPE_PREFIX_MAP.get(cfn_type, f"cfn:{cfn_type}")
            properties = res_def.get("Properties") or {}
            if not isinstance(properties, dict):
                properties = {}

            # Try to find a friendly name property.
            name = _resource_name(cfn_type, logical_id, properties)
            logical_to_name[logical_id] = name

            # Tags → entities + env inference.
            tags_list = properties.get("Tags") or []
            tags_dict = _tags_to_dict(tags_list)
            env = _env_from_tags(tags_dict) or _env_from_name(name)

            # Capture the most useful properties (deliberately a subset; the
            # full property bag can be huge).
            capture = _capture_useful_props(cfn_type, properties)

            result.resources.append(ExtractedResource(
                name=name, kind=kind,
                environment=env,
                properties={
                    "logical_id": logical_id,
                    "cfn_type": cfn_type,
                    **capture,
                },
                context_snippet=f"CloudFormation {cfn_type} '{logical_id}' in {path.name}",
            ))

            # Tag entities.
            for k, v in tags_dict.items():
                result.entities.append(ExtractedEntity(
                    name=k, entity_type="tag", value=v,
                    context_snippet=f"tag on {logical_id}",
                ))

        # Second pass: cross-resource references.
        for logical_id, res_def in resources.items():
            if not isinstance(res_def, dict):
                continue
            src_name = logical_to_name.get(logical_id, logical_id)
            blob = json.dumps(res_def.get("Properties") or {})

            # !Ref or {"Ref": "..."}
            for m in _REF_RE.finditer(blob):
                target_id = m.group(1) or m.group(2)
                if target_id and target_id != logical_id and target_id in logical_to_name:
                    result.edges.append(ExtractedEdge(
                        source_name=src_name, source_kind="resource",
                        target_name=logical_to_name[target_id], target_kind="resource",
                        rel_type="DEPENDS_ON",
                        properties={"via": "cfn_ref"},
                    ))
            # !GetAtt Foo.Bar or {"Fn::GetAtt": ["Foo", "Bar"]}
            for m in _GETATT_RE.finditer(blob):
                target_id = m.group(1) or m.group(2)
                if target_id and target_id != logical_id and target_id in logical_to_name:
                    result.edges.append(ExtractedEdge(
                        source_name=src_name, source_kind="resource",
                        target_name=logical_to_name[target_id], target_kind="resource",
                        rel_type="DEPENDS_ON",
                        properties={"via": "cfn_getatt"},
                    ))

            # Explicit DependsOn property (CloudFormation native).
            deps = res_def.get("DependsOn") or []
            if isinstance(deps, str):
                deps = [deps]
            if isinstance(deps, list):
                for dep_id in deps:
                    if dep_id in logical_to_name:
                        result.edges.append(ExtractedEdge(
                            source_name=src_name, source_kind="resource",
                            target_name=logical_to_name[dep_id], target_kind="resource",
                            rel_type="DEPENDS_ON",
                            properties={"via": "cfn_dependson"},
                        ))

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_template(content: str) -> dict | None:
    """Parse YAML (preferred) or JSON CloudFormation template."""
    # CloudFormation YAML has short-form intrinsics (!Ref, !GetAtt, etc.) that
    # PyYAML's safe loader rejects. We register harmless constructors that
    # treat them as plain strings, which is enough for our regex-based
    # reference pass.
    try:
        return yaml.load(content, Loader=_make_cfn_loader())
    except yaml.YAMLError:
        pass
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _make_cfn_loader():
    """Build a SafeLoader that accepts CFN short-form intrinsics."""
    class CfnLoader(yaml.SafeLoader):
        pass
    # Match !Ref, !GetAtt, !Sub, !Join, !If, !Equals, !FindInMap, etc.
    _intrinsics = (
        "!Ref", "!GetAtt", "!Sub", "!Join", "!Select", "!Split",
        "!If", "!Equals", "!And", "!Or", "!Not", "!FindInMap",
        "!Base64", "!Cidr", "!ImportValue", "!Transform", "!Condition",
    )
    for tag in _intrinsics:
        # The loader needs `tag[1:]` as the constructor key (drop the !).
        CfnLoader.add_constructor(
            f"!{tag[1:]}",
            lambda loader, node: _construct_cfn(loader, node),
        )
    return CfnLoader


def _construct_cfn(loader, node) -> str:
    """Turn any CFN intrinsic into a plain string for downstream regex."""
    if isinstance(node, yaml.ScalarNode):
        return f"!{node.tag.lstrip('!')} {loader.construct_scalar(node)}"
    if isinstance(node, yaml.SequenceNode):
        return f"!{node.tag.lstrip('!')} {loader.construct_sequence(node)}"
    if isinstance(node, yaml.MappingNode):
        return f"!{node.tag.lstrip('!')} {loader.construct_mapping(node)}"
    return ""


def _tags_to_dict(tags: list) -> dict[str, str]:
    """CFN tags are list of {Key, Value} dicts."""
    out: dict[str, str] = {}
    for t in tags:
        if isinstance(t, dict):
            k = t.get("Key", "")
            v = t.get("Value", "")
            if k:
                out[str(k)] = str(v)
    return out


def _env_from_tags(tags: dict[str, str]) -> str:
    for key in ("Environment", "env", "stage", "Stage", "tier", "ENV"):
        v = tags.get(key, "").strip().lower()
        if v in ("prod", "production", "live"):
            return "production"
        if v in ("stag", "staging", "preprod", "uat"):
            return "staging"
        if v in ("dev", "develop", "development", "test", "sandbox"):
            return "dev"
        if v:
            return v
    return ""


def _env_from_name(name: str) -> str:
    n = name.lower()
    for hint in ("prod", "production"):
        if hint in n:
            return "production"
    for hint in ("staging", "preprod", "uat"):
        if hint in n:
            return "staging"
    for hint in ("dev", "develop", "sandbox", "test"):
        if hint in n:
            return "dev"
    return ""


# Per-type name properties (CFN doesn't have a universal "name" field).
_NAME_PROPS = {
    "AWS::RDS::DBInstance":              "DBInstanceIdentifier",
    "AWS::RDS::DBCluster":               "DBClusterIdentifier",
    "AWS::Lambda::Function":             "FunctionName",
    "AWS::S3::Bucket":                   "BucketName",
    "AWS::ECS::Cluster":                 "ClusterName",
    "AWS::ECS::Service":                 "ServiceName",
    "AWS::EKS::Cluster":                 "Name",
    "AWS::IAM::Role":                    "RoleName",
    "AWS::IAM::Policy":                  "PolicyName",
    "AWS::DynamoDB::Table":              "TableName",
    "AWS::SQS::Queue":                   "QueueName",
    "AWS::SNS::Topic":                   "TopicName",
    "AWS::SecretsManager::Secret":       "Name",
    "AWS::Logs::LogGroup":               "LogGroupName",
    "AWS::CloudWatch::Alarm":            "AlarmName",
    "AWS::Events::Rule":                 "Name",
    "AWS::StepFunctions::StateMachine":  "StateMachineName",
    "AWS::AutoScaling::AutoScalingGroup": "AutoScalingGroupName",
    "AWS::Route53::HostedZone":          "Name",
}


def _resource_name(cfn_type: str, logical_id: str, properties: dict) -> str:
    """Pick the friendliest name from properties; fall back to logical ID."""
    prop_key = _NAME_PROPS.get(cfn_type)
    if prop_key:
        v = properties.get(prop_key)
        if isinstance(v, str) and v:
            return v
    return logical_id


# Per-type "useful properties" capture — keep the noise low.
def _capture_useful_props(cfn_type: str, props: dict) -> dict:
    """Pull the 3-5 most important properties per CFN type."""
    keep_keys = {
        "AWS::RDS::DBInstance": ("Engine", "DBInstanceClass", "MultiAZ", "Encrypted"),
        "AWS::EC2::Instance": ("InstanceType", "ImageId", "SubnetId"),
        "AWS::Lambda::Function": ("Runtime", "Handler", "MemorySize", "Timeout"),
        "AWS::S3::Bucket": ("VersioningConfiguration", "BucketEncryption"),
        "AWS::DynamoDB::Table": ("BillingMode", "TableName"),
        "AWS::IAM::Role": ("RoleName", "Path"),
    }
    keys = keep_keys.get(cfn_type, ())
    return {k: props.get(k) for k in keys if k in props}
