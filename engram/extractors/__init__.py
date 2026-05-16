"""Engram extractors.

Each extractor understands one file format and returns an ExtractionResult
with Resources (infra structural units) and Entities (code-level units).
"""

from engram.extractors.base import (
    BaseExtractor,
    ExtractedEntity,
    ExtractedResource,
    ExtractionResult,
    get_extractor,
)

__all__ = [
    "BaseExtractor",
    "ExtractedEntity",
    "ExtractedResource",
    "ExtractionResult",
    "get_extractor",
]
