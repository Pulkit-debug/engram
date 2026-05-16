"""Allowlist of Terraform resource types that support `tags = { ... }` blocks.

Why an allowlist (not a denylist): a small set of canonical resources covers
~95% of what DevOps engineers care about, and the cost of *adding* tags to a
resource that doesn't support them is concrete — `terraform plan` fails. The
cost of *missing* tags on an unknown resource is small — the user can re-run
with `--include-types` or open a PR.

Sources:
  * HashiCorp AWS provider docs: types with the `tags` argument
  * HashiCorp AzureRM provider docs: types with the `tags` argument
  * HashiCorp Google provider docs: types with `labels` (we map both)

This list is intentionally NOT exhaustive — only the 80ish types that
genuinely show up in DevOps Terraform repos. Maintainers can extend at any
time without breaking compatibility.
"""

from __future__ import annotations


# AWS resources that accept `tags`. (Compiled from the AWS provider docs;
# the *_attachment, *_association, *_policy variants are deliberately excluded
# because they do NOT accept tags — adding them breaks `terraform plan`.)
_AWS_TAGGABLE = frozenset({
    # Compute
    "aws_instance", "aws_launch_template", "aws_launch_configuration",
    "aws_autoscaling_group", "aws_spot_instance_request", "aws_ami",
    "aws_ebs_volume", "aws_ebs_snapshot", "aws_key_pair",

    # Networking
    "aws_vpc", "aws_subnet", "aws_internet_gateway", "aws_nat_gateway",
    "aws_route_table", "aws_security_group", "aws_network_acl",
    "aws_vpn_gateway", "aws_customer_gateway", "aws_vpn_connection",
    "aws_eip", "aws_network_interface", "aws_vpc_peering_connection",

    # Load balancing
    "aws_lb", "aws_alb", "aws_lb_target_group", "aws_alb_target_group",

    # Databases
    "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_db_subnet_group", "aws_db_parameter_group",
    "aws_elasticache_cluster", "aws_elasticache_replication_group",
    "aws_dynamodb_table", "aws_neptune_cluster", "aws_redshift_cluster",
    "aws_docdb_cluster", "aws_memorydb_cluster",

    # Containers / serverless
    "aws_ecs_cluster", "aws_ecs_service", "aws_ecs_task_definition",
    "aws_eks_cluster", "aws_eks_node_group", "aws_eks_fargate_profile",
    "aws_lambda_function", "aws_lambda_layer_version",
    "aws_ecr_repository", "aws_ecrpublic_repository",

    # Storage
    "aws_s3_bucket", "aws_efs_file_system", "aws_fsx_lustre_file_system",
    "aws_fsx_windows_file_system",

    # Messaging
    "aws_sqs_queue", "aws_sns_topic", "aws_kinesis_stream",
    "aws_kinesis_firehose_delivery_stream", "aws_mq_broker",

    # Identity
    "aws_iam_role", "aws_iam_user", "aws_iam_policy",
    "aws_iam_instance_profile", "aws_iam_openid_connect_provider",

    # DNS
    "aws_route53_zone", "aws_route53_resolver_endpoint",

    # CloudFront / API
    "aws_cloudfront_distribution", "aws_api_gateway_rest_api",
    "aws_api_gateway_stage", "aws_apigatewayv2_api", "aws_apigatewayv2_stage",

    # Monitoring / logging
    "aws_cloudwatch_log_group", "aws_cloudwatch_metric_alarm",
    "aws_cloudwatch_dashboard", "aws_cloudtrail",

    # Security / KMS
    "aws_kms_key", "aws_acm_certificate", "aws_secretsmanager_secret",
    "aws_ssm_parameter", "aws_wafv2_web_acl", "aws_guardduty_detector",

    # Workflow / batch
    "aws_sfn_state_machine", "aws_batch_compute_environment", "aws_batch_job_queue",

    # Container registry
    "aws_codedeploy_app", "aws_codepipeline", "aws_codebuild_project",
})


# AWS resource patterns that are DEFINITELY untaggable.
# We hold these as a separate denylist for explicit logging (so users know
# why a resource was skipped).
_AWS_UNTAGGABLE = frozenset({
    # *_attachment / *_association — link resources, never taggable.
    "aws_iam_role_policy_attachment", "aws_iam_user_policy_attachment",
    "aws_iam_group_policy_attachment", "aws_iam_policy_attachment",
    "aws_route_table_association", "aws_main_route_table_association",
    "aws_network_acl_association", "aws_security_group_rule",
    "aws_vpc_dhcp_options_association", "aws_iam_role_policy",
    "aws_iam_user_policy", "aws_iam_group_policy",
    "aws_s3_bucket_policy", "aws_s3_bucket_versioning",
    "aws_s3_bucket_lifecycle_configuration", "aws_s3_bucket_acl",
    "aws_s3_bucket_logging", "aws_s3_bucket_website_configuration",
    "aws_s3_bucket_cors_configuration", "aws_s3_bucket_notification",
    "aws_lambda_permission", "aws_lambda_event_source_mapping",
    "aws_route", "aws_route53_record",
    "aws_eks_cluster_auth", "aws_lb_listener_rule", "aws_lb_listener",
    "aws_autoscaling_attachment", "aws_autoscaling_policy",
    "aws_ecs_account_setting_default",
})


# AzureRM resources that accept `tags`. (Most do; common subset listed.)
_AZURERM_TAGGABLE = frozenset({
    "azurerm_resource_group", "azurerm_virtual_network", "azurerm_subnet",
    "azurerm_network_security_group", "azurerm_public_ip", "azurerm_lb",
    "azurerm_virtual_machine", "azurerm_linux_virtual_machine",
    "azurerm_windows_virtual_machine", "azurerm_managed_disk",
    "azurerm_storage_account", "azurerm_storage_container",
    "azurerm_key_vault", "azurerm_key_vault_secret",
    "azurerm_postgresql_server", "azurerm_mysql_server",
    "azurerm_mssql_server", "azurerm_cosmosdb_account",
    "azurerm_kubernetes_cluster", "azurerm_container_registry",
    "azurerm_function_app", "azurerm_app_service",
    "azurerm_application_gateway", "azurerm_log_analytics_workspace",
    "azurerm_monitor_action_group", "azurerm_eventhub_namespace",
    "azurerm_servicebus_namespace", "azurerm_redis_cache",
})


# Google Cloud resources that accept `labels` (we treat as the tag analog).
_GCP_LABELABLE = frozenset({
    "google_compute_instance", "google_compute_instance_template",
    "google_compute_disk", "google_compute_image", "google_compute_network",
    "google_compute_subnetwork", "google_compute_router",
    "google_compute_forwarding_rule", "google_compute_global_forwarding_rule",
    "google_compute_address", "google_compute_global_address",
    "google_storage_bucket", "google_bigquery_table", "google_bigquery_dataset",
    "google_pubsub_topic", "google_pubsub_subscription",
    "google_sql_database_instance", "google_redis_instance",
    "google_container_cluster", "google_container_node_pool",
    "google_cloud_run_service", "google_cloudfunctions_function",
    "google_kms_crypto_key", "google_secret_manager_secret",
    "google_dataproc_cluster", "google_dataflow_job",
})


# Engram-specific patterns to ALWAYS skip:
#   * `data.*` references (not resources)
#   * variables / outputs / locals / module references
#   * data sources
_NEVER_TAG_PREFIXES = (
    "data.", "var.", "local.", "module.", "each.", "count.",
    "tf:data:", "tf:variable:", "tf:output:", "tf:module:",
)


def is_taggable(resource_kind: str) -> tuple[bool, str]:
    """Check whether a Terraform resource kind supports tags/labels.

    Returns (is_taggable, reason). Reason is human-readable, used for logging.

    Args:
        resource_kind: Engram resource kind like 'tf:aws_db_instance' or
                       'tf:azurerm_storage_account'. The 'tf:' prefix is
                       stripped before lookup.
    """
    if not resource_kind or not resource_kind.startswith("tf:"):
        return False, f"not a Terraform resource: {resource_kind}"

    tf_type = resource_kind[3:]

    # Engram-specific patterns we never tag.
    for prefix in _NEVER_TAG_PREFIXES:
        if tf_type.startswith(prefix):
            return False, f"non-resource construct: {tf_type}"

    # Known-untaggable AWS resources.
    if tf_type in _AWS_UNTAGGABLE:
        return False, f"{tf_type} does not accept tags (terraform plan would fail)"

    # Allowlists by provider.
    if tf_type in _AWS_TAGGABLE:
        return True, "aws-taggable"
    if tf_type in _AZURERM_TAGGABLE:
        return True, "azurerm-taggable"
    if tf_type in _GCP_LABELABLE:
        return True, "gcp-labelable"

    # Unknown — default to skipping. Conservative.
    return False, f"unknown resource type ({tf_type}); add to allowlist or use --include-types"


def tag_argument_for(resource_kind: str) -> str:
    """Return the HCL argument name to use (`tags` for AWS/Azure, `labels` for GCP)."""
    if not resource_kind.startswith("tf:"):
        return "tags"
    tf_type = resource_kind[3:]
    if tf_type in _GCP_LABELABLE:
        return "labels"
    return "tags"
