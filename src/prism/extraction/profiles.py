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

#: The closed registry of selectable prompt profiles.  ``None`` (the
#: constructor default) is the baseline prompt with no additions.
KNOWN_PROMPT_PROFILES = frozenset({BASELINE_PROMPT_PROFILE, PROTOCOL_V1_PROFILE})


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
    raise ValueError(f"unknown prompt_profile {normalized!r}")
