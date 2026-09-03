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
    IndependentInterpretation,
    KeyEvidence,
    PerspectiveProfile,
    PerspectiveResult,
    SynthesisPoint,
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


def _fields(path: str, value: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise _OutputInvalid(f"{path} has invalid fields ({'; '.join(details)})")


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
    return result


def _parse_independent(
    payload: dict[str, Any], profile_id: str, allowed: frozenset[str]
) -> IndependentInterpretation:
    _fields("independent", payload, {"statements"})
    raw_statements = _array("independent.statements", payload["statements"])
    if not raw_statements:
        raise _OutputInvalid("independent.statements must not be empty")
    statements: list[DebateStatement] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_statements):
        path = f"independent.statements[{index}]"
        if not isinstance(raw, dict):
            raise _OutputInvalid(f"{path} must be a JSON object")
        _fields(path, raw, {"id", "classification", "text", "evidence_ids"})
        statement_id = _identifier(f"{path}.id", raw["id"])
        if statement_id in seen:
            raise _OutputInvalid(f"duplicate statement id {statement_id!r}")
        seen.add(statement_id)
        classification = raw["classification"]
        if classification not in {
            "fact",
            "interpretation",
            "value_judgment",
            "prediction",
            "unresolved",
        }:
            raise _OutputInvalid(f"{path}.classification is not supported")
        evidence_ids = _citations(f"{path}.evidence_ids", raw["evidence_ids"], allowed)
        if classification != "unresolved" and not evidence_ids:
            raise _OutputInvalid(f"{path} requires evidence ids")
        statements.append(
            DebateStatement(
                id=statement_id,
                classification=classification,
                text=_text(f"{path}.text", raw["text"]),
                evidence_ids=evidence_ids,
            )
        )
    return IndependentInterpretation(profile_id, tuple(statements))


def _parse_cross(
    payload: dict[str, Any],
    profile_id: str,
    allowed: frozenset[str],
    targets: dict[str, set[str]],
) -> CrossExamination:
    _fields("cross_examination", payload, {"challenges"})
    challenges: list[CrossChallenge] = []
    seen: set[str] = set()
    raw_challenges = _array("cross_examination.challenges", payload["challenges"])
    for index, raw in enumerate(raw_challenges):
        path = f"cross_examination.challenges[{index}]"
        if not isinstance(raw, dict):
            raise _OutputInvalid(f"{path} must be a JSON object")
        _fields(
            path,
            raw,
            {
                "challenge_id",
                "target_profile_id",
                "target_statement_id",
                "challenge",
                "reply",
                "withdrawn",
                "evidence_ids",
            },
        )
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
    return CrossExamination(profile_id, tuple(challenges))


def _synthesis_point(
    path: str, raw: object, allowed: frozenset[str]
) -> SynthesisPoint:
    if not isinstance(raw, dict):
        raise _OutputInvalid(f"{path} must be a JSON object")
    _fields(path, raw, {"text", "evidence_ids"})
    evidence_ids = _citations(f"{path}.evidence_ids", raw["evidence_ids"], allowed)
    if not evidence_ids:
        raise _OutputInvalid(f"{path} requires evidence ids")
    return SynthesisPoint(_text(f"{path}.text", raw["text"]), evidence_ids)


def _parse_synthesis(
    payload: dict[str, Any], allowed: frozenset[str]
) -> DebateSynthesis:
    required = {
        "consensus",
        "disagreements",
        "sources_of_disagreement",
        "key_evidence",
        "unresolved_questions",
        "falsification_conditions",
    }
    _fields("synthesis", payload, required)
    key_evidence: list[KeyEvidence] = []
    raw_key_evidence = _array("synthesis.key_evidence", payload["key_evidence"])
    for index, raw in enumerate(raw_key_evidence):
        path = f"synthesis.key_evidence[{index}]"
        if not isinstance(raw, dict):
            raise _OutputInvalid(f"{path} must be a JSON object")
        _fields(path, raw, {"evidence_id", "rationale"})
        evidence_id = _identifier(f"{path}.evidence_id", raw["evidence_id"])
        if evidence_id not in allowed:
            raise _OutputInvalid(f"{path} references unknown evidence id")
        key_evidence.append(
            KeyEvidence(evidence_id, _text(f"{path}.rationale", raw["rationale"]))
        )

    def points(name: str) -> tuple[SynthesisPoint, ...]:
        return tuple(
            _synthesis_point(f"synthesis.{name}[{index}]", item, allowed)
            for index, item in enumerate(_array(f"synthesis.{name}", payload[name]))
        )

    return DebateSynthesis(
        consensus=points("consensus"),
        disagreements=points("disagreements"),
        sources_of_disagreement=points("sources_of_disagreement"),
        key_evidence=tuple(key_evidence),
        unresolved_questions=points("unresolved_questions"),
        falsification_conditions=points("falsification_conditions"),
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
        + "Interpret this one case from your observation position. Classify every "
        "statement as fact, interpretation, value_judgment, prediction or "
        "unresolved. Facts, causal judgments, interpretations, predictions and "
        "value judgments require evidence_ids. Use unresolved when evidence is "
        "insufficient. Do not invent citations or balance the debate with "
        "unsupported opposition.\n\n"
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
                interpretation = _parse_independent(
                    _load_payload(getattr(completion, "text", None)),
                    profile.id,
                    allowed,
                )
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
                cross = _parse_cross(
                    _load_payload(getattr(completion, "text", None)),
                    profile.id,
                    allowed,
                    targets,
                )
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
                synthesis = _parse_synthesis(
                    _load_payload(getattr(completion, "text", None)), allowed
                )
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
        )
        if self._ledger is not None:
            result = self._ledger.record(result, rounds, input_hash)
        return result
