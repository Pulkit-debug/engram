"""Cloud-discovery importers.

The "click-ops gap": most teams have production resources that exist only
in the cloud console, not in any source file. Engram indexes files, so it
would normally be blind to these. This module fills that gap WITHOUT
breaking the local-first promise:

  * Engram does NOT call cloud APIs. The user's `aws` / `gcloud` / `kubectl`
    CLI does that, using the user's existing credentials.
  * Engram parses the CLI's JSON output and inserts Resources into the
    graph with `discovered_from = '<provider>-cli'` for traceability.
  * Re-running detects drift between discovered state and source-file state.

Subcommands:
  engram import-cloud  --provider aws  --kinds rds,ec2,s3,...
  engram import-cluster  --context prod  --namespace default
"""
