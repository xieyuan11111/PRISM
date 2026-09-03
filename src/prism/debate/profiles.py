"""Built-in observation positions for automatic debate."""

from __future__ import annotations

from .models import PerspectiveProfile

DEFAULT_PROFILES = (
    PerspectiveProfile(
        "institutional_regulatory",
        "Institutional and regulatory observation",
        "Interpret the case from institutions, rules, authority and enforcement.",
    ),
    PerspectiveProfile(
        "industry_execution",
        "Industry and execution observation",
        "Interpret the case from implementation, operations and organizational capacity.",
    ),
    PerspectiveProfile(
        "affected_groups",
        "Affected groups observation",
        "Interpret the case from the position of groups affected by the change.",
    ),
    PerspectiveProfile(
        "academic_observer",
        "Academic observer position",
        "Interpret the case from scholarly framing, evidence quality and research history.",
    ),
)

ACADEMIC_PROFILES = (
    PerspectiveProfile(
        "experimental_methods",
        "Experimental methods observation",
        "Focus on design, measurement, validity and reproducibility.",
    ),
    PerspectiveProfile(
        "mechanism_explanation",
        "Mechanism explanation observation",
        "Focus on causal mechanisms and alternative explanations.",
    ),
    PerspectiveProfile(
        "evidence_quality",
        "Evidence quality observation",
        "Focus on source quality, bias, uncertainty and evidence limits.",
    ),
    PerspectiveProfile(
        "research_history",
        "Research history observation",
        "Focus on how the scholarly debate developed and changed over time.",
    ),
)

__all__ = ["ACADEMIC_PROFILES", "DEFAULT_PROFILES"]
