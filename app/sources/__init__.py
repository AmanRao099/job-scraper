"""Source registry.

Register new boards here; the pipeline discovers them by name.
"""

from __future__ import annotations

from app.sources.base import JobSource, RawJob
from app.sources.linkedin import LinkedInSource
from app.sources.naukri import NaukriSource

SOURCE_REGISTRY: dict[str, type[JobSource]] = {
    NaukriSource.name: NaukriSource,
    LinkedInSource.name: LinkedInSource,
}

AVAILABLE_SOURCES = sorted(SOURCE_REGISTRY)

__all__ = [
    "AVAILABLE_SOURCES",
    "SOURCE_REGISTRY",
    "JobSource",
    "LinkedInSource",
    "NaukriSource",
    "RawJob",
]
