"""Automatic, evidence-bounded multi-perspective debate service (FR-5)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Protocol

from prism.analyzer import EvolutionAnalysis
from prism.llm import MissingRoleError, TaskRole

from .models import (
    CrossChallenge,
    CrossExamination,
    DebateFailure,
    DebateResult,
    DebateStatement,
    DebateSynthesis,
    FollowUpResult,
    IndependentInterpretation,
    KeyEvidence,
    PerspectiveProfile,
    PerspectiveResult,
    SynthesisPoint,
    STATEMENT_CLASSIFICATIONS,
)
from .profiles import ACADEMIC_PROFILES, DEFAULT_PROFILES

_DEBATE_ROLE = TaskRole.DEBATE
_ID = re.compile(r"[A-Za-z0-9_.:-]+\Z")
_SECRET = re.compile(
    r"(?i)\b(?:sk-[a-z0-9][a-z0-9_-]*|bearer\s+[a-z0-9._-]+|"
    r"(?:api[_-]?key|authorization|credential|password|passwd|secret|token)"
    r"\s*[:=]\s*[^\s,;]+)\b"
)
# URL runs are protected before path redaction and restored afterwards, so a
# scheme reference next to an absolute path ("...\\notes.md; see
# https://host/a/b") is never consumed by a path matcher.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'|]+")
_URL_MARK = "\uE000"
# Absolute path components never contain whitespace: components separated by
# "\\" or "/" stop at the next whitespace, so trailing prose ("; see
# https://...", " at 10:30") is never swallowed up to a later ":" or "/".
# A drive-letter path whose final component contains a space (e.g. a bare
# "C:\\Program Files") is only partially redacted; the remaining tail is no
# longer an absolute path.
_WINDOWS_PATH = re.compile(
    r"(?i)\b[A-Za-z]:[\\/](?:[^\s\\/:*?\"<>|\r\n]+[\\/])*[^\s\\/:*?\"<>|\r\n]+"
)
# Redacts absolute filesystem paths while preserving URL references in
# untrusted material text: the lookbehind keeps scheme separators
# ("https://") intact and components never contain whitespace, so a bare
# "/" inside prose is never touched and prose after a path stays readable.
_UNIX_PATH = re.compile(
    r"(?<![\w:./])/(?:[^\s\\/:*?\"<>|\r\n]+/)*[^\s\\/:*?\"<>|\r\n]+"
)


class _AnalyzerLike(Protocol):
    async def analyze(
        self,
        case_id: str,
        as_of: datetime | None = None,
        *,
        kinds: Iterable[str] | None = None,
    ) -> EvolutionAnalysis: ...


class _RouterLike(Protocol):
    async def complete(self, role: object, prompt: str) -> object: ...


class _LedgerLike(Protocol):
    def find(self, input_hash: str) -> DebateResult | None: ...

    def record(
        self, result: DebateResult, rounds: list[dict[str, Any]], input_hash: str
    ) -> DebateResult: ...


class _OutputInvalid(ValueError):
    """A completion cannot be trusted as structured debate output."""


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _protect_urls(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def keep(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"{_URL_MARK}{len(urls) - 1}{_URL_MARK}"

    return _URL.sub(keep, text), urls


def _restore_urls(text: str, urls: list[str]) -> str:
    for index, url in enumerate(urls):
        text = text.replace(f"{_URL_MARK}{index}{_URL_MARK}", url)
    return text


def _sanitize_text(value: str) -> str:
    text = _SECRET.sub("[REDACTED]", value)
    text, urls = _protect_urls(text)
    text = _WINDOWS_PATH.sub("[REDACTED]", text)
    text = _UNIX_PATH.sub("[REDACTED]", text)
    return _restore_urls(text, urls)


def _sanitize_question(value: str) -> str:
    text = _SECRET.sub(" ", value)
    text, urls = _protect_urls(text)
    text = _WINDOWS_PATH.sub(" ", text)
    text = _UNIX_PATH.sub(" ", text)
    text = _restore_urls(text, urls)
    return " ".join(text.split())


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item) for item in value]
    return value


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _locator_payload(locator: Any) -> dict[str, Any]:
    return {
        "source_id": locator.source_id,
        "corpus_path": locator.corpus_path,
        "paragraph": locator.paragraph,
        "page": locator.page,
        "quote": locator.quote,
    }


def _stage_payload(stage: Any) -> dict[str, Any]:
    return {
        "entry_id": stage.episode_key,
        "kind": stage.kind,
        "layer": stage.layer,
        "summary": stage.summary,
        "valid_at": _iso(stage.valid_at),
        "invalid_at": _iso(stage.invalid_at),
        "reference_time": _iso(stage.reference_time),
        "source_ids": list(stage.source_ids),
        "node_type": stage.node_type,
        "claim_type": stage.claim_type,
        "relation_type": stage.relation_type,
        "source_ref": stage.source_ref,
        "target_ref": stage.target_ref,
        "record_id": stage.record_id,
        "confidence": stage.confidence,
        "provenance_type": stage.provenance_type,
        "evidence_role": stage.evidence_role,
        "evidence": [_locator_payload(locator) for locator in stage.evidence],
    }


def _analysis_payload(analysis: EvolutionAnalysis) -> dict[str, Any]:
    stages = [_stage_payload(stage) for stage in analysis.stages]
    invalidated = [_stage_payload(stage) for stage in analysis.invalidated_stages]
    conflicts = [
        stage
        for stage in stages
        if stage["kind"] == "temporal_relation"
        and stage["relation_type"] in {"contradicts", "conflicts_with", "conflicts"}
    ]
    return _sanitize_payload(
        {
            "case_id": analysis.case_id,
            "as_of": analysis.as_of.isoformat(),
            "case_type": analysis.case_type,
            "case_status": analysis.case_status,
            "effective_entries": stages,
            "invalidated_entries": invalidated,
            "conflicts": conflicts,
            "turning_points": [
                {
                    "entry_id": point.episode_key,
                    "category": point.category,
                    "at": point.at.isoformat(),
                    "summary": point.summary,
                    "source_ids": list(point.source_ids),
                    "evidence": [_locator_payload(locator) for locator in point.evidence],
                }
                for point in analysis.turning_points
            ],
            "change_reasons": [
                {
                    "entry_id": reason.episode_key,
                    "reason_type": reason.reason_type,
                    "nature": reason.nature,
                    "at": reason.at.isoformat(),
                    "summary": reason.summary,
                    "source_ids": list(reason.source_ids),
                }
                for reason in analysis.change_reasons
            ],
            "evidence_gaps": [
                {
                    "gap_type": gap.gap_type,
                    "detail": gap.detail,
                    "entry_id": gap.episode_key,
                    "source_ids": list(gap.source_ids),
                }
                for gap in analysis.evidence_gaps
            ],
            "open_questions": [
                {
                    "entry_id": question.episode_key,
                    "origin": question.origin,
                    "question": question.question,
                    "raised_by": question.raised_by,
                    "at": question.at.isoformat(),
                    "source_ids": list(question.source_ids),
                }
                for question in analysis.open_questions
            ],
        }
    )


def _evidence_ids(analysis: EvolutionAnalysis) -> frozenset[str]:
    evidence: set[str] = set()

    def add(entry_id: str | None, source_ids: Iterable[str]) -> None:
        if entry_id:
            evidence.add(entry_id)
        evidence.update(source_ids)

    for stage in analysis.stages:
        add(stage.episode_key, stage.source_ids)
        evidence.update(item.source_id for item in stage.evidence)
    for stage in analysis.invalidated_stages:
        add(stage.episode_key, stage.source_ids)
        evidence.update(item.source_id for item in stage.evidence)
    for point in analysis.turning_points:
        add(point.episode_key, point.source_ids)
    for reason in analysis.change_reasons:
        add(reason.episode_key, reason.source_ids)
    for gap in analysis.evidence_gaps:
        add(gap.episode_key, gap.source_ids)
    for question in analysis.open_questions:
        add(question.episode_key, question.source_ids)
    return frozenset(evidence)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _profile_map() -> dict[str, PerspectiveProfile]:
    return {profile.id: profile for profile in DEFAULT_PROFILES + ACADEMIC_PROFILES}


def _configured_profiles(
    profiles: Iterable[PerspectiveProfile | str] | None,
) -> tuple[PerspectiveProfile, ...]:
    if profiles is None:
        return ()
    available = _profile_map()
    selected: list[PerspectiveProfile] = []
    for item in profiles:
        if isinstance(item, PerspectiveProfile):
            profile = item
        elif isinstance(item, str):
            if item not in available:
                allowed = ", ".join(sorted(available))
                raise ValueError(
                    f"unknown perspective profile {item!r}; must be one of: {allowed}"
                )
            profile = available[item]
        else:
            raise TypeError("profiles must contain PerspectiveProfile objects or ids")
        if any(existing.id == profile.id for existing in selected):
            raise ValueError(f"duplicate perspective profile: {profile.id!r}")
        selected.append(profile)
    return tuple(selected)


def _selected_profiles(
    configured: tuple[PerspectiveProfile, ...],
    case_type: str | None,
    perspectives: Iterable[str] | None,
) -> tuple[PerspectiveProfile, ...]:
    if perspectives is not None:
        if isinstance(perspectives, str):
            raise TypeError("perspectives must be an iterable of ids, not a string")
        requested = tuple(perspectives)
        if requested:
            available = {
                profile.id: profile
                for profile in (configured or DEFAULT_PROFILES + ACADEMIC_PROFILES)
            }
            selected = []
            for profile_id in requested:
                _require_text("perspectives", profile_id)
                if profile_id not in available:
                    allowed = ", ".join(sorted(available))
                    raise ValueError(
                        f"unknown perspective {profile_id!r}; must be one of: {allowed}"
                    )
                if any(existing.id == profile_id for existing in selected):
                    raise ValueError(
                        f"duplicate perspective profile: {profile_id!r}"
                    )
                selected.append(available[profile_id])
            return tuple(selected)
    if configured:
        return configured
    normalized = (case_type or "").strip().lower()
    return (
        ACADEMIC_PROFILES
        if normalized in {"academic", "academic_discourse"}
        else DEFAULT_PROFILES
    )


def _load_payload(text: object) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise _OutputInvalid("completion must contain a JSON object")
    candidate = text.strip()
    if not candidate.startswith("{") or candidate.endswith("```"):
        raise _OutputInvalid("completion must contain a JSON object")

    def reject_constant(value: str) -> None:
        raise ValueError(f"unsupported JSON constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            candidate,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise _OutputInvalid(f"completion is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise _OutputInvalid("completion must contain a JSON object")
    return payload


def _fields(
    path: str,
    value: dict[str, Any],
    required: set[str],
    *,
    extra_allowed: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Validate a strict field set and return the tolerated extras present.

    Every ``required`` field must be present and any field outside
    ``required | extra_allowed`` is rejected as unknown.  Tolerated extras
    are the confirmed non-critical provider shapes (an overall top-level
    ``classification`` and per-statement ``reasoning``); they are returned so
    the caller can audit the structural deviation without ever promoting the
    ignored content - model reasoning must not become a fact.
    """
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - extra_allowed)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise _OutputInvalid(f"{path} has invalid fields ({'; '.join(details)})")
    return tuple(sorted(value.keys() & extra_allowed))


def _text(path: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _OutputInvalid(f"{path} must be a non-empty string")
    return _sanitize_text(value)


def _identifier(path: str, value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise _OutputInvalid(f"{path} must be an identifier")
    return value


def _boolean(path: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise _OutputInvalid(f"{path} must be a boolean")
    return value


def _array(path: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise _OutputInvalid(f"{path} must be a JSON array")
    return value


def _citations(path: str, value: object, allowed: frozenset[str]) -> tuple[str, ...]:
    result = tuple(
        _identifier(f"{path}[{index}]", item)
        for index, item in enumerate(_array(path, value))
    )
    unknown = sorted(item for item in result if item not in allowed)
    if unknown:
        raise _OutputInvalid(
            f"{path} references unknown evidence id(s): " + ", ".join(unknown)
        )
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in result:
        if item in seen:
            duplicates.add(item)
        else:
            seen.add(item)
    duplicates = sorted(duplicates)
    if duplicates:
        raise _OutputInvalid(
            f"{path} contains duplicate evidence id(s): " + ", ".join(duplicates)
        )
    return result


_CLASSIFICATION_TYPE_KEYS = frozenset({"type", "classification", "category"})


def _classifications_value(path: str, value: object) -> str:
    """Map one confirmed ``classifications`` shape to its single class.

    Only unique, explicit shapes map: a bare class string, a list holding
    exactly one class string, an object whose single type/classification/
    category key carries a class string, or an object whose only key is one
    of the five classes.  Ambiguous or unknown shapes are rejected, never
    guessed; confidence, reason or evidence metadata never selects a class.
    """
    if isinstance(value, str):
        if value in STATEMENT_CLASSIFICATIONS:
            return value
        raise _OutputInvalid(f"{path}.classifications is not supported")
    if isinstance(value, list):
        if (
            len(value) == 1
            and isinstance(value[0], str)
            and value[0] in STATEMENT_CLASSIFICATIONS
        ):
            return value[0]
        raise _OutputInvalid(f"{path}.classifications is not supported")
    if isinstance(value, dict):
        type_keys = [key for key in value if key in _CLASSIFICATION_TYPE_KEYS]
        class_keys = [key for key in value if key in STATEMENT_CLASSIFICATIONS]
        if len(type_keys) == 1 and not class_keys:
            label = value[type_keys[0]]
            if isinstance(label, str) and label in STATEMENT_CLASSIFICATIONS:
                return label
            raise _OutputInvalid(f"{path}.classifications is not supported")
        if not type_keys and len(class_keys) == 1:
            detail = value[class_keys[0]]
            if isinstance(detail, dict):
                return class_keys[0]
    raise _OutputInvalid(f"{path}.classifications is not supported")


def _parse_independent(
    payload: dict[str, Any], profile_id: str, allowed: frozenset[str]
) -> tuple[IndependentInterpretation, tuple[str, ...]]:
    # A real provider wrapped its independent output with an overall
    # "classification" beside "statements".  The value is overall metadata,
    # never a statement, so it is ignored and only audited as a deviation.
    ignored = _fields(
        "independent",
        payload,
        {"statements"},
        extra_allowed=frozenset({"classification"}),
    )
    warnings: list[str] = []
    if ignored:
        warnings.append(
            f"independent output of profile {profile_id!r} carried ignored "
            "top-level field(s) "
            + ", ".join(repr(name) for name in ignored)
            + "; treated as overall metadata, not as a statement"
        )
    raw_statements = _array("independent.statements", payload["statements"])
    if not raw_statements:
        raise _OutputInvalid("independent.statements must not be empty")
    statements: list[DebateStatement] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_statements):
        path = f"independent.statements[{index}]"
        if not isinstance(raw, dict):
            raise _OutputInvalid(f"{path} must be a JSON object")
        # A real provider attached a "reasoning" explanation to statements.
        # Reasoning is explanation metadata, not evidence: it is ignored and
        # audited, and never enters DebateStatement or the fact timeline.
        has_text = "text" in raw
        has_statement = "statement" in raw
        if has_text == has_statement:
            raise _OutputInvalid(
                f"{path} requires exactly one of text or statement"
            )
        text_key = "text" if has_text else "statement"
        # The canonical single-string field stays authoritative; the plural
        # field is only accepted when it drifted into one confirmed explicit
        # shape, and both fields together are ambiguous and rejected.
        has_classification = "classification" in raw
        has_classifications = "classifications" in raw
        if has_classification == has_classifications:
            raise _OutputInvalid(
                f"{path} requires exactly one of classification or classifications"
            )
        classification_key = (
            "classification" if has_classification else "classifications"
        )
        required = {classification_key, text_key, "evidence_ids"}
        if "id" in raw:
            required.add("id")
        ignored_fields = _fields(
            path,
            raw,
            required,
            extra_allowed=frozenset({"reasoning"}),
        )
        if "id" in raw:
            statement_id = _identifier(f"{path}.id", raw["id"])
        else:
            statement_id = f"{profile_id}:independent:{index}"
        if ignored_fields:
            warnings.append(
                f"statement {statement_id!r} of profile {profile_id!r} carried "
                "ignored explanation field(s) "
                + ", ".join(repr(name) for name in ignored_fields)
                + "; model reasoning is not evidence and was not recorded"
            )
        if statement_id in seen:
            raise _OutputInvalid(f"duplicate statement id {statement_id!r}")
        seen.add(statement_id)
        if has_classification:
            classification = raw["classification"]
            if (
                not isinstance(classification, str)
                or classification not in STATEMENT_CLASSIFICATIONS
            ):
                raise _OutputInvalid(f"{path}.classification is not supported")
        else:
            classification = _classifications_value(path, raw["classifications"])
        evidence_ids = _citations(f"{path}.evidence_ids", raw["evidence_ids"], allowed)
        if classification != "unresolved" and not evidence_ids:
            raise _OutputInvalid(f"{path} requires evidence ids")
        statements.append(
            DebateStatement(
                id=statement_id,
                classification=classification,
                text=_text(f"{path}.{text_key}", raw[text_key]),
                evidence_ids=evidence_ids,
            )
        )
    return (
        IndependentInterpretation(profile_id, tuple(statements)),
        tuple(warnings),
    )


_CROSS_CANONICAL_FIELDS = frozenset(
    {
        "challenge_id",
        "target_profile_id",
        "target_statement_id",
        "challenge",
        "reply",
        "withdrawn",
        "evidence_ids",
    }
)
# Only the addressing fields distinguish the canonical schema from the
# confirmed drifted one; challenge/reply/withdrawn/evidence_ids are shared.
_CROSS_CANONICAL_ADDRESSING = frozenset(
    {"challenge_id", "target_profile_id", "target_statement_id"}
)
_CROSS_DRIFTED_FIELDS = frozenset(
    {
        "challenged_id",
        "challenged_text",
        "grounds",
        "reply",
        "withdrawn",
        "evidence_ids",
    }
)
# Confirmed real-provider cross shapes attach an overall reply/answer/response
# text beside (or instead of) the challenges array.  That text is cross-phase
# metadata only: it may serve as reply metadata for a drifted challenge that
# has none, and it must never become a statement fact or an evidence citation.
_CROSS_META_FIELDS = frozenset({"reply", "answer", "response"})


def _parse_cross(
    payload: dict[str, Any],
    profile_id: str,
    allowed: frozenset[str],
    targets: dict[str, set[str]],
) -> tuple[CrossExamination, tuple[str, ...]]:
    """Parse one perspective's cross_examination output.

    The canonical schema (every challenge carries challenge_id,
    target_profile_id, target_statement_id, challenge, reply, withdrawn and
    evidence_ids, and no top-level text field exists) is unchanged and stays
    strict.  The only tolerated deviations are the confirmed real-provider
    shapes: a top-level ``reply``/``answer``/``response`` text, and challenge
    items addressed by ``challenged_id`` (with the challenge text in
    ``challenged_text`` or ``grounds``).  Drifted items are mapped through
    known independent statement ids only - the target perspective is the
    unique owner of the challenged statement id, never guessed - and every
    item that cannot be mapped without inventing content is dropped and
    audited.  ``challenges`` may be absent only when one of the tolerated
    top-level texts is present (an empty no-challenge cross result).
    """
    warnings: list[str] = []
    unknown = sorted(
        set(payload) - {"challenges"} - _CROSS_META_FIELDS
    )
    if unknown:
        raise _OutputInvalid(
            "cross_examination has invalid fields (unknown "
            + ", ".join(unknown)
            + ")"
        )
    meta_present = sorted(set(payload) & _CROSS_META_FIELDS)
    for name in meta_present:
        # The tolerated top-level texts are validated like any text field;
        # their content is only ever reply metadata, never a fact.  An empty
        # value is an audited no-op, not a reason to fail the whole phase.
        if not isinstance(payload[name], str):
            raise _OutputInvalid(
                f"cross_examination.{name} must be a non-empty string"
            )
    if meta_present:
        warnings.append(
            f"cross_examination output of profile {profile_id!r} carried "
            "top-level reply/answer/response field(s) "
            + ", ".join(repr(name) for name in meta_present)
            + "; treated as cross-phase reply metadata, never as a statement "
            "fact or evidence citation"
        )
    if "challenges" not in payload:
        if not meta_present:
            raise _OutputInvalid("cross_examination requires challenges")
        raw_challenges: list[Any] = []
    else:
        raw_challenges = _array(
            "cross_examination.challenges", payload["challenges"]
        )
    # With exactly one non-empty tolerated top-level text it is unambiguous
    # enough to serve as reply metadata for drifted items that carry no reply
    # of their own; several texts would be ambiguous, so none is used.
    shared_reply: str | None = None
    if len(meta_present) == 1:
        text = payload[meta_present[0]]
        shared_reply = _text(
            f"cross_examination.{meta_present[0]}", text
        ) if text.strip() else None

    def drop(index: int, reason: str) -> None:
        warnings.append(
            f"cross_examination output of profile {profile_id!r} dropped "
            f"challenge item {index}: {reason}; the challenge was not recorded"
        )

    challenges: list[CrossChallenge] = []
    seen: set[str] = set()
    mapped_drifted = 0
    for index, raw in enumerate(raw_challenges):
        path = f"cross_examination.challenges[{index}]"
        if not isinstance(raw, dict):
            raise _OutputInvalid(f"{path} must be a JSON object")
        keys = set(raw)
        canonical_keys = keys & _CROSS_CANONICAL_ADDRESSING
        if "challenged_id" in keys and not canonical_keys:
            # Drifted item: addressed by challenged_id; the canonical target
            # pair is reverse-mapped from the known statement id.
            extra = sorted(keys - _CROSS_DRIFTED_FIELDS)
            if extra:
                raise _OutputInvalid(
                    f"{path} has invalid fields (unknown " + ", ".join(extra) + ")"
                )
            challenged_id = _identifier(
                f"{path}.challenged_id", raw["challenged_id"]
            )
            matches = [
                owner
                for owner, statement_ids in targets.items()
                if challenged_id in statement_ids
            ]
            if not matches:
                drop(
                    index,
                    "challenged_id does not reference any known independent "
                    "statement id",
                )
                continue
            if len(matches) > 1:
                drop(
                    index,
                    "challenged_id references statements of more than one "
                    "perspective; the target would have to be guessed",
                )
                continue
            has_challenge_text = "challenged_text" in raw
            has_grounds = "grounds" in raw
            if has_challenge_text == has_grounds:
                drop(
                    index,
                    "no unique challenge text: needs exactly one of "
                    "challenged_text or grounds, without inventing one",
                )
                continue
            text_field = "challenged_text" if has_challenge_text else "grounds"
            challenge = _text(f"{path}.{text_field}", raw[text_field])
            if "reply" in raw:
                reply = _text(f"{path}.reply", raw["reply"])
            elif shared_reply is not None:
                reply = shared_reply
            else:
                drop(
                    index,
                    "no per-challenge reply and no single top-level reply "
                    "metadata to use",
                )
                continue
            if "withdrawn" in raw:
                withdrawn = _boolean(f"{path}.withdrawn", raw["withdrawn"])
            else:
                withdrawn = False
            if "evidence_ids" in raw:
                evidence_ids = _citations(
                    f"{path}.evidence_ids", raw["evidence_ids"], allowed
                )
            else:
                # No citation was supplied; the challenge stays unresolved
                # metadata and is never promoted as if it were evidenced.
                evidence_ids = ()
            target_profile = matches[0]
            challenge_id = f"{profile_id}:cross:{index}"
            if challenge_id in seen:
                raise _OutputInvalid(
                    f"duplicate challenge id {challenge_id!r}"
                )
            seen.add(challenge_id)
            challenges.append(
                CrossChallenge(
                    challenge_id=challenge_id,
                    target_profile_id=target_profile,
                    target_statement_id=challenged_id,
                    challenge=challenge,
                    reply=reply,
                    withdrawn=withdrawn,
                    evidence_ids=evidence_ids,
                )
            )
            mapped_drifted += 1
            continue
        if canonical_keys and "challenged_id" not in keys:
            # Canonical item: the strict contract is unchanged.  Every field
            # is required and nothing else is tolerated.
            _fields(path, raw, _CROSS_CANONICAL_FIELDS)
            challenge_id = _identifier(f"{path}.challenge_id", raw["challenge_id"])
            if challenge_id in seen:
                raise _OutputInvalid(f"duplicate challenge id {challenge_id!r}")
            seen.add(challenge_id)
            target_profile = _identifier(
                f"{path}.target_profile_id", raw["target_profile_id"]
            )
            target_statement = _identifier(
                f"{path}.target_statement_id", raw["target_statement_id"]
            )
            if (
                target_profile not in targets
                or target_statement not in targets[target_profile]
            ):
                raise _OutputInvalid(
                    f"{path} targets an unknown perspective/statement pair"
                )
            challenges.append(
                CrossChallenge(
                    challenge_id=challenge_id,
                    target_profile_id=target_profile,
                    target_statement_id=target_statement,
                    challenge=_text(f"{path}.challenge", raw["challenge"]),
                    reply=_text(f"{path}.reply", raw["reply"]),
                    withdrawn=_boolean(f"{path}.withdrawn", raw["withdrawn"]),
                    evidence_ids=_citations(
                        f"{path}.evidence_ids", raw["evidence_ids"], allowed
                    ),
                )
            )
            continue
        # Drifted-style items that name no challenged statement cannot be
        # attributed to any target: drop and audit them rather than guessing.
        if keys and keys <= _CROSS_DRIFTED_FIELDS - {"challenged_id"}:
            drop(
                index,
                "no challenged_id referencing a known independent statement id",
            )
            continue
        # Items that mix canonical and drifted addressing, carry only unknown
        # fields, or are empty cannot be mapped without guessing.
        raise _OutputInvalid(f"{path} has unsupported challenge fields")
    if mapped_drifted:
        warnings.append(
            f"cross_examination output of profile {profile_id!r} carried "
            f"{mapped_drifted} challenge(s) addressed by challenged_id; they "
            "were mapped through known statement ids without inventing "
            "targets, replies or citations"
        )
    return CrossExamination(profile_id, tuple(challenges)), tuple(warnings)


# A real provider emits the canonical six synthesis sections but drifts the
# item field names in confirmed, non-critical ways.  The canonical item
# schema stays authoritative and unchanged; the confirmed aliases below only
# map onto the same structured fields, and every item must follow exactly one
# naming scheme (an item mixing canonical and alias names is ambiguous and
# rejected, never guessed):
#   * consensus/disagreements/sources_of_disagreement points name the point
#     "summary", "finding" or the latest confirmed "statement" instead of
#     "text";
#   * key_evidence names its rationale "summary", "finding" or the latest
#     confirmed "text", and may use "evidence_ids" for exactly one citation,
#     with an optional "implication" explanation field;
#   * unresolved_questions uses "question" and "relevant_evidence_ids";
#   * falsification_conditions uses "condition" and "relevant_evidence_ids"
#     (the canonical "evidence_ids" field is always accepted).
# "implication" is explanation metadata only, like statement "reasoning": it
# is ignored with a field-level warning that names the field and index but
# never carries the model's implication text, and the text never becomes a
# structured fact.  The same holds for the confirmed per-point "confidence"
# metadata on the point-family sections: it is audited by item index and
# field name only, never read into a fact and never a substitute for a
# citation.  The last confirmed metadata, a per-item "status" on
# unresolved_questions, is handled the same way with one extra rule: its
# value must be a non-empty string, or the item is invalid like any other
# malformed shape.  The string content itself is never read or recorded;
# status is not tolerated on any other section.
_SYNTHESIS_TEXT_ALIASES = {
    "consensus": ("summary", "finding", "statement"),
    "disagreements": ("summary", "finding", "statement"),
    "sources_of_disagreement": ("summary", "finding", "statement"),
    "unresolved_questions": ("question",),
    "falsification_conditions": ("condition",),
}
# Per-section ignored metadata fields (confirmed provider shapes only): the
# point-family sections may carry an overall "confidence" beside the point
# text and citations.  The question/condition sections and key_evidence do
# not, so confidence stays rejected there.
_SYNTHESIS_IGNORED_FIELDS = {
    "consensus": frozenset({"confidence"}),
    "disagreements": frozenset({"confidence"}),
    "sources_of_disagreement": frozenset({"confidence"}),
}
# Confirmed ignored metadata whose value must be a non-empty string, unlike
# the any-value point-family "confidence": an unresolved_questions item may
# carry "status" beside its question and citations.  The value is validated
# like any text field but its content is never read into a fact, and status
# is confined to this section.
_SYNTHESIS_IGNORED_TEXT_FIELDS = {
    "unresolved_questions": frozenset({"status"}),
}
_SYNTHESIS_EVIDENCE_ALIASES = {
    "unresolved_questions": ("relevant_evidence_ids",),
    "falsification_conditions": ("relevant_evidence_ids",),
}


def _one_named_field(
    path: str, raw: dict[str, Any], choices: tuple[str, ...]
) -> str:
    present = tuple(name for name in choices if name in raw)
    if len(present) != 1:
        raise _OutputInvalid(
            f"{path} requires exactly one of " + " or ".join(choices)
        )
    return present[0]


def _synthesis_point(
    path: str,
    raw: object,
    allowed: frozenset[str],
    *,
    text_aliases: tuple[str, ...],
    evidence_aliases: tuple[str, ...] = (),
    ignored_fields: frozenset[str] = frozenset(),
    ignored_text_fields: frozenset[str] = frozenset(),
) -> tuple[SynthesisPoint, tuple[str, ...]]:
    """Parse one synthesis point in its canonical or confirmed alias shape.

    The canonical item shape (``text`` plus ``evidence_ids``) is unchanged
    and stays authoritative.  The only tolerated deviations are the confirmed
    per-section aliases: the text may be named by ``text_aliases`` and, for
    the sections that drifted, citations may be named by ``evidence_aliases``.
    An item must follow exactly one naming scheme per field; mixing or
    omitting schemes is ambiguous and invalid, and any other field is
    rejected.  Every point still requires at least one allowed evidence id:
    alias text can never substitute for a citation.  Confirmed metadata
    (``ignored_fields``, e.g. point-family ``confidence``) is audited by
    field name and item index only, never read, and can never substitute for
    a citation.  ``ignored_text_fields`` is ignored metadata whose value is
    additionally validated as a non-empty string (e.g.
    unresolved_questions ``status``); the content is still never read.
    """
    if not isinstance(raw, dict):
        raise _OutputInvalid(f"{path} must be a JSON object")
    text_choices = ("text", *text_aliases)
    evidence_choices = ("evidence_ids", *evidence_aliases)
    text_key = _one_named_field(path, raw, text_choices)
    ids_key = _one_named_field(path, raw, evidence_choices)
    allowed_fields = set(text_choices) | set(evidence_choices)
    unknown = sorted(set(raw) - allowed_fields - ignored_fields)
    if unknown:
        raise _OutputInvalid(
            f"{path} has invalid fields (unknown " + ", ".join(unknown) + ")"
        )
    evidence_ids = _citations(f"{path}.{ids_key}", raw[ids_key], allowed)
    if not evidence_ids:
        raise _OutputInvalid(f"{path} requires evidence ids")
    warnings: list[str] = []
    ignored_present = sorted(set(raw) & ignored_fields)
    if ignored_present:
        for name in ignored_present:
            if name in ignored_text_fields:
                # The metadata value must look like a non-empty string; its
                # content is validated only and never read or recorded.
                _text(f"{path}.{name}", raw[name])
        warnings.append(
            f"{path} carried ignored metadata field(s) "
            + ", ".join(repr(name) for name in ignored_present)
            + "; the value is not a fact and was not recorded"
        )
    return (
        SynthesisPoint(_text(f"{path}.{text_key}", raw[text_key]), evidence_ids),
        tuple(warnings),
    )


def _key_evidence_item(
    path: str, raw: object, allowed: frozenset[str]
) -> tuple[KeyEvidence, tuple[str, ...]]:
    """Parse one key evidence item in its canonical or confirmed alias shape.

    The canonical item shape (``evidence_id`` plus ``rationale``) is
    unchanged.  Confirmed provider shapes may name the rationale ``summary``,
    ``finding`` or ``text`` and may name a single-item citation list
    ``evidence_ids``; the citation is exactly one allowed id and nothing
    else (no confidence or other metadata) can substitute for it.  An
    optional ``implication`` explanation field is ignored and audited by
    field name and index only; it never enters the structured rationale or
    any other fact.
    """
    if not isinstance(raw, dict):
        raise _OutputInvalid(f"{path} must be a JSON object")
    rationale_choices = ("rationale", "summary", "finding", "text")
    evidence_choices = ("evidence_id", "evidence_ids")
    rationale_key = _one_named_field(path, raw, rationale_choices)
    evidence_key = _one_named_field(path, raw, evidence_choices)
    allowed_fields = set(rationale_choices) | set(evidence_choices) | {"implication"}
    unknown = sorted(set(raw) - allowed_fields)
    if unknown:
        raise _OutputInvalid(
            f"{path} has invalid fields (unknown " + ", ".join(unknown) + ")"
        )
    if evidence_key == "evidence_id":
        evidence_id = _identifier(f"{path}.evidence_id", raw["evidence_id"])
    else:
        evidence_ids = _citations(
            f"{path}.evidence_ids", raw["evidence_ids"], allowed
        )
        if len(evidence_ids) != 1:
            raise _OutputInvalid(
                f"{path}.evidence_ids must contain exactly one evidence id"
            )
        evidence_id = evidence_ids[0]
    if evidence_id not in allowed:
        raise _OutputInvalid(f"{path} references unknown evidence id")
    warnings: list[str] = []
    if "implication" in raw:
        warnings.append(
            f"{path} carried ignored explanation field 'implication'; "
            "explanation metadata is not evidence and was not recorded"
        )
    return (
        KeyEvidence(
            evidence_id, _text(f"{path}.{rationale_key}", raw[rationale_key])
        ),
        tuple(warnings),
    )


def _parse_synthesis(
    payload: dict[str, Any], allowed: frozenset[str]
) -> tuple[DebateSynthesis, tuple[str, ...]]:
    required = {
        "consensus",
        "disagreements",
        "sources_of_disagreement",
        "key_evidence",
        "unresolved_questions",
        "falsification_conditions",
    }
    _fields("synthesis", payload, required)
    warnings: list[str] = []

    def points(name: str) -> tuple[SynthesisPoint, ...]:
        evidence_aliases = _SYNTHESIS_EVIDENCE_ALIASES.get(name, ())
        ignored_text_fields = _SYNTHESIS_IGNORED_TEXT_FIELDS.get(
            name, frozenset()
        )
        parsed: list[SynthesisPoint] = []
        for index, item in enumerate(_array(f"synthesis.{name}", payload[name])):
            point, point_warnings = _synthesis_point(
                f"synthesis.{name}[{index}]",
                item,
                allowed,
                text_aliases=_SYNTHESIS_TEXT_ALIASES[name],
                evidence_aliases=evidence_aliases,
                ignored_fields=(
                    _SYNTHESIS_IGNORED_FIELDS.get(name, frozenset())
                    | ignored_text_fields
                ),
                ignored_text_fields=ignored_text_fields,
            )
            parsed.append(point)
            warnings.extend(point_warnings)
        return tuple(parsed)

    key_evidence: list[KeyEvidence] = []
    for index, raw in enumerate(
        _array("synthesis.key_evidence", payload["key_evidence"])
    ):
        item, item_warnings = _key_evidence_item(
            f"synthesis.key_evidence[{index}]", raw, allowed
        )
        key_evidence.append(item)
        warnings.extend(item_warnings)

    return (
        DebateSynthesis(
            consensus=points("consensus"),
            disagreements=points("disagreements"),
            sources_of_disagreement=points("sources_of_disagreement"),
            key_evidence=tuple(key_evidence),
            unresolved_questions=points("unresolved_questions"),
            falsification_conditions=points("falsification_conditions"),
        ),
        tuple(warnings),
    )


def _request(
    phase: str,
    question: str,
    as_of: datetime,
    perspective_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "phase": phase,
        "question": question,
        "as_of": as_of.isoformat(),
    }
    if perspective_id is not None:
        payload["perspective_id"] = perspective_id
    return (
        "BEGIN REQUEST\n"
        + _canonical_json(payload)
        + "\nEND REQUEST\n\n"
        + "Return exactly one JSON object. No prose, no extra fields, no "
        "NaN/Infinity, and no duplicate keys.\n"
    )


def _independent_prompt(
    profile: PerspectiveProfile, question: str, evidence: dict[str, Any]
) -> str:
    as_of = datetime.fromisoformat(evidence["as_of"])
    return (
        _request("independent", question, as_of, profile.id)
        + "Interpret this one case from your observation position. Each statement "
        "object must contain exactly id, classification, text and evidence_ids. "
        "Classify every statement as fact, interpretation, value_judgment, "
        "prediction or unresolved: classification must be a single string "
        "naming one class, never a classifications string or a JSON array "
        "containing exactly one string. Do not use a classifications object, "
        "do not use a classifications list, do not attach reasoning, and do "
        "not output both text and statement. Facts, causal judgments, "
        "interpretations, predictions and value judgments require "
        "evidence_ids. Use unresolved when evidence is insufficient. Do not "
        "invent citations or balance the debate with unsupported opposition.\n\n"
        + "BEGIN EVIDENCE BUNDLE\n"
        + _canonical_json(evidence)
        + "\nEND EVIDENCE BUNDLE"
    )


def _cross_prompt(
    profile: PerspectiveProfile,
    question: str,
    as_of: datetime,
    evidence: dict[str, Any],
    independent: dict[str, Any],
) -> str:
    return (
        _request("cross_examination", question, as_of, profile.id)
        + "Challenge the other structured interpretations using only the same "
        "evidence bundle. You may challenge, reply or withdraw, but you must "
        "not invent citations or create opposition merely for balance.\n\n"
        "Return exactly one JSON object whose only field is challenges, an "
        "array of challenge objects. Each challenge object must contain "
        "exactly challenge_id, target_profile_id, target_statement_id, "
        "challenge, reply, withdrawn and evidence_ids, citing only existing "
        "statement ids from BEGIN INDEPENDENT OUTPUTS. Do not use a "
        "challenged_id/challenged_text/grounds shape and do not attach a "
        "top-level reply, answer or response field.\n\n"
        + "BEGIN EVIDENCE BUNDLE\n"
        + _canonical_json(evidence)
        + "\nEND EVIDENCE BUNDLE\n\n"
        + "BEGIN INDEPENDENT OUTPUTS\n"
        + _canonical_json(independent)
        + "\nEND INDEPENDENT OUTPUTS"
    )


def _synthesis_prompt(
    question: str,
    as_of: datetime,
    evidence: dict[str, Any],
    independent: dict[str, Any],
    cross: dict[str, Any],
) -> str:
    return (
        _request("synthesis", question, as_of)
        + "Synthesize the already-validated structured outputs. Separate "
        "consensus, disagreements, sources of disagreement, key evidence, "
        "unresolved questions and falsification conditions. Every item must "
        "cite existing evidence ids. Do not turn model reasoning into a fact.\n\n"
        "Return exactly those six top-level fields. consensus, disagreements, "
        "and sources_of_disagreement items must contain exactly text and "
        "evidence_ids; key_evidence items must contain exactly evidence_id and "
        "rationale; unresolved_questions and falsification_conditions items must "
        "contain exactly text and evidence_ids. Do not use summary, finding, "
        "statement, question, condition, relevant_evidence_ids, implication, "
        "confidence, or status fields. Evidence id arrays must not repeat an id.\n\n"
        + "BEGIN EVIDENCE BUNDLE\n"
        + _canonical_json(evidence)
        + "\nEND EVIDENCE BUNDLE\n\n"
        + "BEGIN INDEPENDENT OUTPUTS\n"
        + _canonical_json(independent)
        + "\nEND INDEPENDENT OUTPUTS\n\n"
        + "BEGIN CROSS-EXAMINATION OUTPUTS\n"
        + _canonical_json(cross)
        + "\nEND CROSS-EXAMINATION OUTPUTS"
    )


def _conservative_synthesis(allowed: frozenset[str]) -> DebateSynthesis:
    evidence_id = min(allowed) if allowed else None
    unresolved: tuple[SynthesisPoint, ...] = ()
    key_evidence: tuple[KeyEvidence, ...] = ()
    if evidence_id is not None:
        unresolved = (
            SynthesisPoint(
                "Automatic synthesis is unavailable; no debate conclusion is asserted.",
                (evidence_id,),
            ),
        )
        key_evidence = (
            KeyEvidence(
                evidence_id,
                "Recorded evidence was available; no model synthesis is asserted.",
            ),
        )
    return DebateSynthesis(
        consensus=(),
        disagreements=(),
        sources_of_disagreement=(),
        key_evidence=key_evidence,
        unresolved_questions=unresolved,
        falsification_conditions=(),
    )


def _failure_code(error: BaseException) -> str:
    return "llm_unavailable" if isinstance(error, MissingRoleError) else "llm_failure"


def _round(
    phase: str,
    profile_id: str | None,
    output: object | None,
    failure: DebateFailure | None = None,
    fallback: bool = False,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "profile_id": profile_id,
        "output": None if output is None else asdict(output),
        "failure": None if failure is None else asdict(failure),
        "fallback": fallback,
    }


class DebateService:
    """Run a bounded three-phase automatic debate over one analysis snapshot."""

    def __init__(
        self,
        analyzer: _AnalyzerLike,
        router: _RouterLike | None,
        *,
        ledger: _LedgerLike | None = None,
        profiles: Iterable[PerspectiveProfile | str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if analyzer is None or not callable(getattr(analyzer, "analyze", None)):
            raise TypeError("analyzer must provide analyze()")
        if router is not None and not callable(getattr(router, "complete", None)):
            raise TypeError("router must provide complete()")
        if ledger is not None and not callable(getattr(ledger, "find", None)):
            raise TypeError("ledger must provide find()")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._analyzer = analyzer
        self._router = router
        self._ledger = ledger
        self._configured = _configured_profiles(profiles)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def evidence_bundle_hash(self, case_id: str, as_of: datetime) -> str:
        """Hash the current evidence bundle at ``as_of`` without any LLM call.

        Uses exactly the analyzer projection and hashing the debate phases
        use, so a caller can compare a recorded run's
        ``evidence_bundle_hash`` against the present historical snapshot.
        The router is never contacted: staleness detection is deterministic.
        """

        _require_text("case_id", case_id)
        _require_aware("as_of", as_of)
        analysis = await self._analyzer.analyze(case_id, as_of)
        if not isinstance(analysis, EvolutionAnalysis):
            raise TypeError("analyzer must return an EvolutionAnalysis")
        if analysis.case_id != case_id:
            raise ValueError("analyzer returned another case")
        return _hash(_analysis_payload(analysis))

    async def follow_up(
        self,
        parent_run_id: str,
        question: str,
        perspective: str,
    ) -> FollowUpResult:
        """Ask one named perspective a follow-up over the parent's snapshot."""
        _require_text("parent_run_id", parent_run_id)
        question = _sanitize_question(_require_text("question", question))
        perspective = _require_text("perspective", perspective)
        if self._ledger is None or not callable(
            getattr(self._ledger, "result_by_run_id", None)
        ):
            raise ValueError("a debate ledger with result_by_run_id() is required")
        parent = self._ledger.result_by_run_id(parent_run_id)
        if parent is None:
            raise LookupError(f"no parent debate run {parent_run_id!r}")
        profiles = _selected_profiles(self._configured, None, (perspective,))
        profile = profiles[0]
        analysis = await self._analyzer.analyze(parent.case_id, parent.as_of)
        if not isinstance(analysis, EvolutionAnalysis):
            raise TypeError("analyzer must return an EvolutionAnalysis")
        evidence = _analysis_payload(analysis)
        evidence_hash = _hash(evidence)
        if evidence_hash != parent.evidence_bundle_hash:
            raise ValueError(
                "parent debate evidence bundle is no longer the current historical snapshot"
            )
        input_hash = _hash({
            "parent_run_id": parent_run_id,
            "question": question,
            "perspective_id": profile.id,
            "as_of": parent.as_of.isoformat(),
            "evidence_bundle_hash": parent.evidence_bundle_hash,
        })
        find = getattr(self._ledger, "find_follow_up", None)
        if callable(find):
            replayed = find(input_hash)
            if replayed is not None:
                return replayed
        if self._router is None:
            failure = DebateFailure(
                profile.id, "follow_up", "llm_unavailable",
                "debate LLM role is unavailable",
            )
            result = FollowUpResult(
                follow_up_id=input_hash, parent_run_id=parent_run_id,
                case_id=parent.case_id, question=question, as_of=parent.as_of,
                perspective_id=profile.id, evidence_bundle_hash=parent.evidence_bundle_hash,
                interpretation=None, status="failed", errors=(failure,),
                completed_at=self._clock(),
            )
        else:
            try:
                completion = await self._router.complete(
                    _DEBATE_ROLE, _independent_prompt(profile, question, evidence)
                )
                interpretation, warnings = _parse_independent(
                    _load_payload(getattr(completion, "text", None)),
                    profile.id, _evidence_ids(analysis),
                )
                result = FollowUpResult(
                    follow_up_id=input_hash, parent_run_id=parent_run_id,
                    case_id=parent.case_id, question=question, as_of=parent.as_of,
                    perspective_id=profile.id, evidence_bundle_hash=parent.evidence_bundle_hash,
                    interpretation=interpretation, status="completed",
                    warnings=tuple(warnings), completed_at=self._clock(),
                )
            except Exception as error:
                failure = DebateFailure(
                    profile.id, "follow_up",
                    "invalid_output" if isinstance(error, _OutputInvalid) else _failure_code(error),
                    "follow-up rejected invalid model output"
                    if isinstance(error, _OutputInvalid)
                    else f"follow-up failed: {type(error).__name__}",
                )
                result = FollowUpResult(
                    follow_up_id=input_hash, parent_run_id=parent_run_id,
                    case_id=parent.case_id, question=question, as_of=parent.as_of,
                    perspective_id=profile.id, evidence_bundle_hash=parent.evidence_bundle_hash,
                    interpretation=None, status="failed", errors=(failure,),
                    completed_at=self._clock(),
                )
        record = getattr(self._ledger, "record_follow_up", None)
        return record(result, input_hash) if callable(record) else result

    async def debate(
        self,
        case_id: str,
        question: str,
        as_of: datetime | None = None,
        perspectives: Iterable[str] | None = None,
    ) -> DebateResult:
        """Debate one read-only evidence snapshot without user arbitration."""

        _require_text("case_id", case_id)
        question = _sanitize_question(_require_text("question", question))
        if as_of is not None:
            _require_aware("as_of", as_of)

        analysis = await self._analyzer.analyze(case_id, as_of)
        if not isinstance(analysis, EvolutionAnalysis):
            raise TypeError("analyzer must return an EvolutionAnalysis")
        if analysis.case_id != case_id:
            raise ValueError("analyzer returned another case")

        profiles = _selected_profiles(
            self._configured, analysis.case_type, perspectives
        )
        evidence = _analysis_payload(analysis)
        evidence_hash = _hash(evidence)
        profile_ids = tuple(profile.id for profile in profiles)
        input_hash = _hash(
            {
                "case_id": case_id,
                "question": question,
                "as_of": analysis.as_of.isoformat(),
                "profiles": list(profile_ids),
                "evidence_bundle_hash": evidence_hash,
            }
        )
        if self._ledger is not None:
            replayed = self._ledger.find(input_hash)
            if replayed is not None:
                return replayed

        allowed = _evidence_ids(analysis)
        rounds: list[dict[str, Any]] = []
        failures: list[DebateFailure] = []
        warnings: list[str] = []
        interpretations: dict[str, IndependentInterpretation] = {}
        results: list[PerspectiveResult] = []

        for profile in profiles:
            if self._router is None:
                failure = DebateFailure(
                    profile.id,
                    "independent",
                    "llm_unavailable",
                    "debate LLM role is unavailable",
                )
                failures.append(failure)
                rounds.append(_round("independent", profile.id, None, failure))
                results.append(
                    PerspectiveResult(profile.id, "unavailable", failure=failure)
                )
                continue
            try:
                completion = await self._router.complete(
                    _DEBATE_ROLE,
                    _independent_prompt(profile, question, evidence),
                )
                interpretation, deviations = _parse_independent(
                    _load_payload(getattr(completion, "text", None)),
                    profile.id,
                    allowed,
                )
                warnings.extend(deviations)
            except Exception as error:
                invalid = isinstance(error, _OutputInvalid)
                failure = DebateFailure(
                    profile.id,
                    "independent",
                    "invalid_output" if invalid else _failure_code(error),
                    "debate independent phase rejected invalid model output"
                    if invalid
                    else f"debate independent phase failed: {type(error).__name__}",
                )
                failures.append(failure)
                rounds.append(_round("independent", profile.id, None, failure))
                results.append(
                    PerspectiveResult(profile.id, "unavailable", failure=failure)
                )
                continue
            interpretations[profile.id] = interpretation
            results.append(PerspectiveResult(profile.id, "available", interpretation))
            rounds.append(_round("independent", profile.id, interpretation))

        independent_payload = {
            profile_id: asdict(interpretation)
            for profile_id, interpretation in interpretations.items()
        }
        targets = {
            profile_id: {statement.id for statement in interpretation.statements}
            for profile_id, interpretation in interpretations.items()
        }
        cross_payload: dict[str, Any] = {}

        for index, result in enumerate(results):
            if result.status != "available":
                continue
            profile = profiles[index]
            try:
                completion = await self._router.complete(
                    _DEBATE_ROLE,
                    _cross_prompt(
                        profile,
                        question,
                        analysis.as_of,
                        evidence,
                        independent_payload,
                    ),
                )
                cross, deviations = _parse_cross(
                    _load_payload(getattr(completion, "text", None)),
                    profile.id,
                    allowed,
                    targets,
                )
                warnings.extend(deviations)
            except Exception as error:
                invalid = isinstance(error, _OutputInvalid)
                failure = DebateFailure(
                    profile.id,
                    "cross_examination",
                    "invalid_output" if invalid else _failure_code(error),
                    "debate cross-examination phase rejected invalid model output"
                    if invalid
                    else "debate cross-examination phase failed: "
                    + type(error).__name__,
                )
                failures.append(failure)
                rounds.append(_round("cross_examination", profile.id, None, failure))
                results[index] = PerspectiveResult(
                    profile.id,
                    "unavailable",
                    interpretation=result.interpretation,
                    failure=failure,
                )
                continue
            cross_payload[profile.id] = asdict(cross)
            rounds.append(_round("cross_examination", profile.id, cross))
            results[index] = PerspectiveResult(
                profile.id,
                "available",
                interpretation=result.interpretation,
                cross_examination=cross,
            )

        synthesis: DebateSynthesis | None = None
        fallback_reason: str | None = None
        if not interpretations:
            fallback_reason = (
                "debate LLM role is unavailable"
                if self._router is None
                else "no debate conclusion: all perspective LLM calls unavailable"
            )
        else:
            try:
                completion = await self._router.complete(
                    _DEBATE_ROLE,
                    _synthesis_prompt(
                        question,
                        analysis.as_of,
                        evidence,
                        independent_payload,
                        cross_payload,
                    ),
                )
                synthesis, deviations = _parse_synthesis(
                    _load_payload(getattr(completion, "text", None)), allowed
                )
                warnings.extend(deviations)
                rounds.append(_round("synthesis", None, synthesis))
            except Exception as error:
                failure = DebateFailure(
                    None,
                    "synthesis",
                    "invalid_output"
                    if isinstance(error, _OutputInvalid)
                    else _failure_code(error),
                    "debate synthesis failed safely; deterministic summary used",
                )
                failures.append(failure)
                synthesis = _conservative_synthesis(allowed)
                fallback_reason = (
                    "synthesis invalid; deterministic conservative summary used"
                )
                rounds.append(_round("synthesis", None, synthesis, failure, True))

        if not interpretations:
            status = "no_conclusion"
        elif fallback_reason is not None:
            status = "degraded"
        elif any(result.status == "unavailable" for result in results):
            status = "completed_with_unavailable_perspectives"
        else:
            status = "completed"

        completed_at = self._clock()
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            raise RuntimeError("clock must return timezone-aware datetimes")
        result = DebateResult(
            case_id=case_id,
            question=question,
            as_of=analysis.as_of,
            profiles=profile_ids,
            results=tuple(results),
            synthesis=synthesis,
            status=status,
            fallback_reason=fallback_reason,
            evidence_bundle_hash=evidence_hash,
            errors=tuple(failures),
            completed_at=completed_at,
            warnings=tuple(warnings),
        )
        if self._ledger is not None:
            result = self._ledger.record(result, rounds, input_hash)
        return result
