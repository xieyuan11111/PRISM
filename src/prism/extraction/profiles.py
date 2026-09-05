"""Experimental LLM prompt profiles for strict evolution extraction.

PRISM's semantic quality lives in the LLM prompt; deterministic code only
guards evidence, time, source, case and relation boundaries.  A prompt
profile therefore may only *add instructions in front of the baseline
strict evolution prompt* — it may never edit the baseline contract, relax
deterministic validation, or fabricate relations.  Profile selection is a
controlled, explicit constructor argument on
:class:`prism.extraction.service.ExtractionService`; the default
production composition never passes one, so the baseline prompt stays the
byte-identical default.
"""

from __future__ import annotations

from datetime import datetime

BASELINE_PROMPT_PROFILE = "baseline"
PROTOCOL_V1_PROFILE = "protocol-v1"
PROTOCOL_V2_PROFILE = "protocol-v2"

#: The closed registry of selectable prompt profiles.  ``None`` (the
#: constructor default) is the baseline prompt with no additions.
KNOWN_PROMPT_PROFILES = frozenset(
    {BASELINE_PROMPT_PROFILE, PROTOCOL_V1_PROFILE, PROTOCOL_V2_PROFILE}
)


def normalize_prompt_profile(value: object) -> str | None:
    """Validate a prompt profile selection, or raise ``ValueError``.

    ``None`` and ``"baseline"`` both denote the untouched baseline prompt;
    every other value must be a known profile name, and unknown, empty,
    wrongly-cased or non-string selections are rejected instead of being
    silently coerced to the baseline.
    """

    if value is None:
        return None
    if not isinstance(value, str) or value not in KNOWN_PROMPT_PROFILES:
        allowed = ", ".join(sorted(KNOWN_PROMPT_PROFILES))
        raise ValueError(
            f"unknown prompt_profile {value!r}; known profiles: {allowed} "
            "(or None for the untouched baseline prompt)"
        )
    return value


def _protocol_v1_self_check(fetched_at: datetime) -> str:
    """The SILENT PRE-JSON SELF-CHECK block for the protocol-v1 profile.

    The check is model-side reasoning only: the block explicitly forbids
    any trace of the check in the returned JSON.  It restates the four
    deterministic boundaries the parser already enforces (verbatim
    paragraph-unique quotes, timestamp ordering against ``fetched_at``,
    prediction-as-uncertain-claim, explicit verbatim-evidenced relations
    with ``relations: []`` as the default) so the model drops hopeless
    candidates before emitting them — it never widens what the parser
    accepts.
    """

    return (
        "SILENT PRE-JSON SELF-CHECK — experimental profile protocol-v1.\n"
        "Perform this check silently in your own reasoning BEFORE writing "
        "the JSON object. The check itself is never part of the response: "
        "the JSON you return must contain no self-check results, notes, "
        "fields, or mention of this check.\n"
        "1. QUOTE CHECK: for every candidate you are about to emit, each "
        "evidence quote is copied verbatim, character for character, as one "
        "continuous fragment of a single non-empty paragraph of MATERIAL "
        "CONTENT, and the quote occurs in exactly that one paragraph "
        "(paragraph-unique). A retyped, normalized, translated, paraphrased "
        "or cross-paragraph quote fails the check: drop that candidate "
        "entirely instead of repairing or shortening it.\n"
        "2. TIME CHECK: for every candidate you are about to emit, the "
        "timestamps you keep — observed_at, valid_at, happened_at, stated_at "
        "(whichever apply) — are timezone-aware, ordered (observed_at not "
        "earlier than valid_at, and not earlier than happened_at or stated_at "
        "when those apply), and none is later than the material fetched time "
        f"{fetched_at.isoformat()}. A candidate that cannot satisfy its time "
        "invariants is dropped, never coerced or adjusted.\n"
        "3. PREDICTION CHECK: a forecast, possibility, recommendation, or "
        "hypothetical is emitted only as a claim with claim_type prediction "
        "and stance uncertain; it is never a node, temporal_fact, conflict, "
        "or relation.\n"
        "4. RELATION CHECK: emit a relation only when the material "
        "explicitly states that relationship, each of source_ref and "
        "target_ref references a candidate actually emitted in this same "
        "response, and a verbatim quote supports the relationship. Otherwise "
        "the relations array must be exactly [] — never emit an inferred, "
        "chronological, or hand-built relation.\n"
        "After the silent check completes, return exactly one JSON object "
        "as specified below; the self-check leaves no trace in the JSON.\n\n"
    )


def _protocol_v2_canonical_id_check() -> str:
    """The fifth silent check added by protocol-v2.

    Protocol-v2 addresses unstable model-chosen identifiers.  It constrains
    only the spelling and construction of identifiers; it does not add a
    JSON field, ask for metadata, or replace any deterministic validation.
    """

    return (
        "5. CANONICAL ID CHECK: before writing JSON, silently select one "
        "canonical event/fact identity for each retained candidate and "
        "construct its id only from the formats below. IDs contain only "
        "ASCII lowercase letters, digits, and '-'.\n"
        "- node: node-{node_type}-{source_id}-{YYYYMMDD}; one material, one "
        "date, and one node_type for a common policy change produces exactly "
        "one node; put the separate details in facts.\n"
        "- temporal_fact: fact-{source_id}-p{paragraph}-{ordinal}; the "
        "ordinal is the 1-based position in original-text order among "
        "same-kind candidates in that paragraph.\n"
        "- claim: claim-{source_id}-p{paragraph}-{ordinal}; use the same "
        "ordinal rule.\n"
        "- relation: rel-{relation_type}-{source_ref}-{target_ref}; "
        "source_ref and target_ref are exact canonical IDs of emitted "
        "candidates.\n"
        "Normalize node_type, source_id, and relation_type deterministically: "
        "lowercase the component, replace every character other than ASCII "
        "lowercase letters, ASCII digits, and '-' with '-', collapse "
        "consecutive '-' to one '-', and remove leading and trailing '-'. "
        "Use YYYYMMDD as exactly eight ASCII digits, and use paragraph and "
        "ordinal as positive integers without padding. The source_id and date "
        "components must come only from the supplied material metadata or the "
        "candidate's verified evidence/time fields; never infer, translate, or "
        "complete them from topic knowledge.\n"
        "If two candidates would produce the same canonical id, retain only "
        "the first candidate in original-text order with complete evidence and "
        "drop every later collision; never add a semantic suffix, random token, "
        "or extra number to avoid a collision.\n"
        "Never invent a semantic English name, topic slug, or random number; "
        "never emit underscores, uppercase letters, non-ASCII characters, or "
        "an id that does not match its format.\n"
        "The canonical choice is silent: Do not add any JSON field, alternate "
        "id, canonical-id metadata, note, or self-check output; use only the "
        "existing id/fact_id/claim_id/relation_id fields.\n"
        "Before emitting JSON, ensure every candidate reference field points "
        "to a node, temporal_fact, or claim actually emitted in this same "
        "response. A relation still requires an explicitly stated "
        "relationship and a verbatim quote. The id rule never replaces "
        "quote, time, source, case, or relation checks; a valid id alone "
        "never rescues a candidate.\n"
        "If a paragraph or original-text order cannot be determined, drop "
        "that candidate.\n"
    )


def _protocol_v2_self_check(fetched_at: datetime) -> str:
    """Protocol-v1's four checks plus the canonical ID check.

    The complete protocol-v1 instruction text is retained byte-for-byte;
    protocol-v2 changes only the profile label and inserts item 5 before
    the existing final instruction.
    """

    v1 = _protocol_v1_self_check(fetched_at)
    v2_header = "SILENT PRE-JSON SELF-CHECK — experimental profile protocol-v2.\n"
    v1_header = "SILENT PRE-JSON SELF-CHECK — experimental profile protocol-v1.\n"
    return v1.replace(v1_header, v2_header, 1).replace(
        "After the silent check completes",
        _protocol_v2_canonical_id_check() + "After the silent check completes",
        1,
    )


def build_profiled_prompt(
    profile: str | None,
    *,
    baseline_prompt: str,
    fetched_at: datetime,
) -> str:
    """Compose the profiled prompt from an untouched baseline prompt.

    The baseline prompt bytes are always preserved verbatim: the only
    transformation any profile may apply is prepending its instruction
    block in front of the complete baseline text.
    """

    normalized = normalize_prompt_profile(profile)
    if normalized is None or normalized == BASELINE_PROMPT_PROFILE:
        return baseline_prompt
    if normalized == PROTOCOL_V1_PROFILE:
        return _protocol_v1_self_check(fetched_at) + baseline_prompt
    if normalized == PROTOCOL_V2_PROFILE:
        return _protocol_v2_self_check(fetched_at) + baseline_prompt
    raise ValueError(f"unknown prompt_profile {normalized!r}")
