from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import datetime, timedelta, timezone

import pytest

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def make_material(**overrides):
    values = {
        "id": "material-1",
        "title": "Policy",
        "source": "example.com",
        "published_at": NOW,
        "fetched_at": NOW,
        "type": "policy",
        "content": "Body",
        "original_format": "pdf",
        "ocr": False,
        "extracted_via": "pdfplumber",
        "raw_path": "raw/policy.pdf",
        "case_tags": ["case-1"],
        "url": "https://example.com/policy",
    }
    values.update(overrides)
    return Material(**values)


def test_domain_models_are_frozen_dataclasses():
    instances = [
        make_material(),
        EvolutionCase("case-1", "policy", "Policy case", NOW, "active"),
        EvolutionNode("node-1", "case-1", "publication", NOW, "Published", ["material-1"]),
        TemporalFact("Agency", "published", "Policy", NOW, None, NOW, ["material-1"], 0.9, "explicit"),
        Claim("claim-1", "Scholar", "Policy matters", "support", NOW, ["material-1"]),
    ]

    for instance in instances:
        assert is_dataclass(instance)
        with pytest.raises(FrozenInstanceError):
            instance.__class__.__setattr__(instance, fields(instance)[0].name, "changed")


def test_material_contains_required_ingestion_metadata():
    material = make_material()
    assert material.original_format == "pdf"
    assert material.ocr is False
    assert material.case_tags == ("case-1",)


def test_all_temporal_fields_require_timezone_aware_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        make_material(published_at=datetime(2026, 8, 31, 12, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        EvolutionCase("case-1", "policy", "Policy", datetime(2026, 8, 31, 12, 0), "active")
    with pytest.raises(ValueError, match="timezone-aware"):
        EvolutionNode("node-1", "case-1", "publication", datetime(2026, 8, 31, 12, 0), "Published", ["m"])
    with pytest.raises(ValueError, match="timezone-aware"):
        TemporalFact("A", "p", "B", datetime(2026, 8, 31, 12, 0), None, NOW, ["m"], 0.5, "explicit")
    with pytest.raises(ValueError, match="timezone-aware"):
        Claim("c", "A", "p", "support", datetime(2026, 8, 31, 12, 0), ["m"])


def test_temporal_order_is_validated():
    with pytest.raises(ValueError, match="fetched_at"):
        make_material(fetched_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="invalid_at"):
        TemporalFact("A", "p", "B", NOW, NOW - timedelta(seconds=1), NOW, ["m"], 0.5, "explicit")


def test_collections_are_normalized_to_immutable_tuples():
    case = EvolutionCase("case-1", "policy", "Policy", NOW, "active", ["n1"])
    node = EvolutionNode("node-1", "case-1", "publication", NOW, "Published", ["m1"], ["c1"])
    fact = TemporalFact("A", "p", "B", NOW, None, NOW, ["m1"], 0.5, "explicit")
    claim = Claim("c1", "A", "proposition", "support", NOW, ["m1"])

    assert case.node_ids == ("n1",)
    assert node.source_ids == ("m1",)
    assert node.claim_ids == ("c1",)
    assert fact.source_ids == ("m1",)
    assert claim.based_on == ("m1",)


def test_invalid_enum_values_are_rejected():
    with pytest.raises(ValueError, match="case_type"):
        EvolutionCase("case-1", "unknown", "Policy", NOW, "active")
    with pytest.raises(ValueError, match="node_type"):
        EvolutionNode("node-1", "case-1", "unknown", NOW, "Summary", ["m"])
    with pytest.raises(ValueError, match="stance"):
        Claim("c1", "A", "p", "unknown", NOW, ["m"])
    with pytest.raises(ValueError, match="original_format"):
        make_material(original_format="exe")


def test_confidence_is_bounded_and_boolean_is_not_a_number():
    with pytest.raises(ValueError, match="between"):
        TemporalFact("A", "p", "B", NOW, None, NOW, ["m"], 1.1, "explicit")
    with pytest.raises(TypeError, match="number"):
        TemporalFact("A", "p", "B", NOW, None, NOW, ["m"], True, "explicit")
