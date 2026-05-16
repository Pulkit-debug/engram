"""Engram inference layer.

Post-pass modules that derive new graph edges from observations the
extractors and importers already made — but that a single extractor
couldn't have seen alone.

Currently:
  * value_match: env-var / secret values that contain a cloud resource's
    endpoint / DNS name / ARN / bucket → DEPENDS_ON edge

These edges carry `properties.inferred_from` so they're visually
distinct from edges declared in source files. Future agents that act on
the graph (or human readers via emit-agents-md) can tell signal from
heuristic.
"""
