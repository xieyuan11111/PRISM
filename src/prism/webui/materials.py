"""Dependency-free controller/view-model seam for the PRISM material intake.

The controller is the WebUI's only write boundary for appended materials
(FR-8.7): one explicit, user-provided Markdown/PDF path is appended to one
explicitly declared target case through the existing
``PrismAPI.add_material`` facade — the automatic pipeline stays the only
write path.  The controller never guesses a case from a title or tag, never
writes the corpus itself, and never calls an LLM (``use_llm`` defaults to
``False`` and is forwarded only when the caller explicitly opts in).  The
returned view reports the pipeline status and stage audit, the saved report
version, and the debate-link prior/current evidence-bundle hashes with the
stale flag; a facade failure propagates and is never rewritten into a fake
success.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Any, Protocol

from .evidence import parse_time_bound
from .status import outcome_status, safe_error_text

MATERIAL_SUFFIXES = (".md", ".markdown", ".pdf")


class MaterialEntryFacade(Protocol):
    """The facade operation used by the material intake (FR-8.7)."""

    async def add_material(
        self,
        source: str | os.PathLike[str],
        target_case: object,
        metadata: dict[str, Any] | None = None,
        *,
        as_of: datetime | None = None,
        use_llm: bool = True,
        parent_debate_run_id: str | None = None,
    ) -> object: ...


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _validated_path(source: str | os.PathLike[str]) -> Path:
    """Validate one explicit user-supplied MD/PDF path before any write.

    The path must be a non-empty string or path-like object with a Markdown
    or PDF suffix, and must name an existing file — the controller never
    searches the project for candidates and never accepts other formats.
    """
    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    if not isinstance(source, str):
        raise TypeError("path must be a non-empty string or path")
    if not source.strip():
        raise ValueError("path must be a non-empty string or path")
    path = Path(source)
    if path.suffix.lower() not in MATERIAL_SUFFIXES:
        allowed = ", ".join(MATERIAL_SUFFIXES)
        raise ValueError(
            f"path must be a Markdown or PDF document ({allowed}): {path.name}"
        )
    if not path.is_file():
        raise FileNotFoundError(f"material file not found: {source}")
    return path


def _validated_target_case(target_case: object) -> object:
    """Require the caller-declared case; PRISM never guesses one."""
    if isinstance(target_case, str):
        target_case = target_case.strip()
        if not target_case:
            raise ValueError(
                "target_case is required; PRISM never guesses a case"
            )
    if target_case is None:
        raise ValueError("target_case is required; PRISM never guesses a case")
    return target_case


def pipeline_view(run: object) -> dict[str, Any]:
    """Project one pipeline run into JSON-safe view data.

    Stage audit records keep their name/status/detail but never their
    ``result`` payloads — an index outcome carries the material's full
    document content, which does not belong in a status view.
    """
    return {
        "material_id": getattr(run, "material_id"),
        "status": getattr(run, "status"),
        "detail": getattr(run, "detail", None),
        "correlation_id": getattr(run, "correlation_id", None),
        "started_at": _iso(getattr(run, "started_at", None)),
        "finished_at": _iso(getattr(run, "finished_at", None)),
        "stages": [
            {
                "name": getattr(stage, "name"),
                "status": getattr(stage, "status"),
                "detail": getattr(stage, "detail", None),
            }
            for stage in tuple(getattr(run, "stages", ()) or ())
        ],
    }


def report_version_view(version: object) -> dict[str, Any] | None:
    """Project one report version into JSON-safe view data.

    The rendered ``markdown`` body is deliberately omitted: the intake view
    reports the version identity and reproducibility metadata, not the
    document itself.
    """
    if version is None:
        return None
    return {
        "version_id": getattr(version, "version_id"),
        "case_id": getattr(version, "case_id"),
        "as_of": _iso(getattr(version, "as_of", None)),
        "created_at": _iso(getattr(version, "created_at", None)),
        "input_hash": getattr(version, "input_hash"),
        "markdown_hash": getattr(version, "markdown_hash"),
        "summary_origin": getattr(version, "summary_origin", None),
        "debate_input_hash": getattr(version, "debate_input_hash", None),
        "parent_version_id": getattr(version, "parent_version_id", None),
        "trigger": getattr(version, "trigger", None),
    }


def debate_link_view(link: object) -> dict[str, Any] | None:
    """Project one material-debate link into JSON-safe view data."""
    if link is None:
        return None
    return {
        "parent_run_id": getattr(link, "parent_run_id"),
        "case_id": getattr(link, "case_id"),
        "as_of": _iso(getattr(link, "as_of", None)),
        "prior_evidence_bundle_hash": getattr(link, "prior_evidence_bundle_hash"),
        "current_evidence_bundle_hash": getattr(
            link, "current_evidence_bundle_hash"
        ),
        "affected": bool(getattr(link, "affected")),
        "stale": bool(getattr(link, "stale")),
    }


def outcome_view(result: object) -> dict[str, Any]:
    """Project one ``ProcessMaterialResult`` into JSON-safe view data."""
    return {
        **outcome_status(result),
        "material_id": getattr(result, "material_id"),
        "status": getattr(getattr(result, "pipeline"), "status"),
        "pipeline": pipeline_view(getattr(result, "pipeline")),
        "case_id": getattr(result, "case_id"),
        "warnings": list(getattr(result, "warnings", ()) or ()),
        "replayed": bool(getattr(result, "replayed", False)),
        "report_version": report_version_view(
            getattr(result, "report_version", None)
        ),
        "debate_link": debate_link_view(getattr(result, "debate_link", None)),
    }


class MaterialEntryController:
    """View-model adapter over the shared PrismAPI add_material facade.

    All operations are async because the facade is async; all results are
    JSON-safe view data for the page.  Invalid inputs — a blank or missing
    path, a non-Markdown/PDF suffix, a missing file, a missing target case,
    a naive ``as_of``, a non-boolean ``use_llm``, a blank parent run id or a
    non-dict metadata mapping — raise explicit exceptions BEFORE any facade
    call, and a pipeline failure propagates unchanged: this controller never
    returns a fake success.
    """

    def __init__(self, api: MaterialEntryFacade) -> None:
        if not callable(getattr(api, "add_material", None)):
            raise TypeError("api must provide add_material()")
        self._api = api

    async def submit(
        self,
        path: str | os.PathLike[str],
        target_case: object,
        metadata: dict[str, Any] | None = None,
        *,
        as_of: datetime | str | None = None,
        use_llm: bool = False,
        parent_debate_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one explicit material to one declared case, end to end.

        Delegates to ``PrismAPI.add_material`` so the existing automatic
        pipeline stays the only write path: the controller validates its own
        arguments, then forwards them (path, target case, metadata, cutoff,
        ``use_llm``, optional parent debate run) unchanged.  ``use_llm``
        defaults to ``False`` — this WebUI slice never triggers an LLM report
        unless the caller explicitly opts in.
        """
        material_path = _validated_path(path)
        resolved_case = _validated_target_case(target_case)
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a dict")
        cutoff = (
            parse_time_bound("as_of", as_of) if as_of is not None else None
        )
        if not isinstance(use_llm, bool):
            raise TypeError("use_llm must be a bool")
        if parent_debate_run_id is not None:
            if (
                not isinstance(parent_debate_run_id, str)
                or not parent_debate_run_id.strip()
            ):
                raise ValueError(
                    "parent_debate_run_id must be a non-empty string "
                    "when supplied"
                )
            parent_debate_run_id = parent_debate_run_id.strip()
        result = await self._api.add_material(
            material_path,
            resolved_case,
            metadata,
            as_of=cutoff,
            use_llm=use_llm,
            parent_debate_run_id=parent_debate_run_id,
        )
        return outcome_view(result)


def _status_markdown(view: dict[str, Any]) -> str:
    """Render the append outcome as a compact, auditable status summary."""
    lines = [
        f"### Appended `{view['material_id']}`",
        f"- Pipeline: **{view['status']}** (replayed: {view['replayed']})",
        f"- Mechanism: **{view['mechanism_status']}**",
        f"- Semantic: **{view['semantic_status']}**",
        f"- Evidence gaps: **{view['evidence_gap_summary']}**",
        f"- Case: {view['case_id']}",
    ]
    for stage in view["pipeline"]["stages"]:
        detail = f" — {stage['detail']}" if stage["detail"] else ""
        lines.append(f"  - stage {stage['name']}: {stage['status']}{detail}")
    version = view["report_version"]
    if version is not None:
        lines.append(
            f"- Report version: `{version['version_id']}` "
            f"(trigger {version['trigger']}, as of {version['as_of']})"
        )
    else:
        lines.append("- Report version: _none_")
    link = view["debate_link"]
    if link is not None:
        lines.append(
            f"- Debate link: parent `{link['parent_run_id']}` — "
            f"stale: **{link['stale']}**, affected: {link['affected']}"
        )
        lines.append(f"  - prior hash: `{link['prior_evidence_bundle_hash']}`")
        lines.append(
            f"  - current hash: `{link['current_evidence_bundle_hash']}`"
        )
    if view["warnings"]:
        lines.append("- Warnings:")
        lines.extend(f"  - {warning}" for warning in view["warnings"])
    else:
        lines.append("- Warnings: _none_")
    return "\n".join(lines)


def build_material_entry_page(
    controller: MaterialEntryController, ui: Any, *,
    title: str = "PRISM Material Entry",
) -> Any:
    """Register the ``/materials`` intake page on the given ``ui`` module.

    The ``ui`` module is injected so the page construction — the explicit
    path/case inputs and the append handler — is a seam testable without
    NiceGUI installed; the handler delegates to the controller and reports
    explicit errors in the message label instead of swallowing them.  The
    page performs no remote upload, no file copying and no permission
    management: the caller types a project-local path they explicitly
    provide (FR-8.9 keeps data modifications audited through the facade).
    """
    @ui.page("/materials")
    def material_entry_page() -> None:
        message = ui.label("Provide an explicit path and target case.")
        status_md = ui.markdown("_No material appended in this session._")

        with ui.card().classes("w-full"):
            ui.label("Append one material").classes("text-bold")
            path_input = ui.input(
                label="Path (MD/PDF, project-local)",
                placeholder="materials/note.md",
            )
            case_input = ui.input(
                label="Target case (id, never guessed)",
                placeholder="case-rates",
            )
            as_of_input = ui.input(
                label="as of (optional, ISO 8601, timezone-aware)",
                placeholder="2026-09-01T00:00:00+00:00",
            )
            parent_input = ui.input(
                label="Parent debate run (optional)",
                placeholder="run-1",
            )
            use_llm_switch = ui.switch("Use LLM for report", value=False)

        def _report(text: str) -> None:
            message.text = text
            message.update()

        async def _append(event: Any = None) -> None:
            try:
                view = await controller.submit(
                    path_input.value or "",
                    case_input.value or "",
                    as_of=as_of_input.value or None,
                    use_llm=bool(use_llm_switch.value),
                    parent_debate_run_id=parent_input.value or None,
                )
            except Exception as error:
                _report(safe_error_text("append", error))
                return
            status_md.content = _status_markdown(view)
            status_md.update()
            _report(
                f"appended {view['material_id']} "
                f"({view['status']}, report {view['report_version'] and view['report_version']['version_id']})"
            )

        with ui.card().classes("w-full"):
            ui.button("Append material", on_click=_append)

    return material_entry_page


__all__ = [
    "MATERIAL_SUFFIXES",
    "MaterialEntryController",
    "MaterialEntryFacade",
    "build_material_entry_page",
    "debate_link_view",
    "outcome_view",
    "pipeline_view",
    "report_version_view",
]
