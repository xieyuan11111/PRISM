"""Dependency-free controller/view-model seam for the report center (WB-4).

This module is the WebUI's report boundary: every read goes through the
injected async facade's existing report operations —
``PrismAPI.report_versions()`` (list), ``PrismAPI.report_version()`` (one
immutable version) and ``PrismAPI.export_report_pdf()`` (derived PDF) —
never the report ledger, SQLite or an LLM.  Versions are immutable: the
pages only read saved Markdown and metadata, the PDF is a derived artifact,
and the export output path is ALWAYS generated server-side under the
facade's controlled output directory (``reports/exports/<version_id>.pdf``);
no caller-supplied path ever reaches the exporter.

Semantic honesty invariants (H-4): the ledger's ``ReportVersion`` carries no
mechanism/semantic/gap fields, so those layers project as ``unknown`` /
``not provided`` — never as pass; ``partial`` stays partial; an empty list
and a facade failure are distinct states; and versions without persisted
structured citations say the citation structure is incomplete instead of
guessing locators from body text.  Everything here is NiceGUI-free; tests
inject fake facades returning real ``ReportVersion`` objects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
import re
from typing import Any, Protocol
from urllib.parse import quote

from prism.report.pdf import (
    ReportPdfConflictError,
    ReportPdfPathError,
    ReportPdfRendererError,
    ReportPdfValidationError,
)

from .status import quality_ui_status, safe_error_text

#: The ledger's version triggers (WB-4.1).
REPORT_TRIGGERS = ("initial", "material_added", "rebuild", "debate_updated")

#: How many leading characters of a hash are shown in list/detail tables.
HASH_DISPLAY_LENGTH = 12

#: Server-controlled export subdirectory (relative to the facade's output
#: directory, which lives under ``PRISM_HOME``); the filename is derived
#: only from the server-validated version id.
EXPORT_SUBDIR = "reports/exports"

#: The case home's route since the unified dashboard took over ``/``: the
#: dashboard's navigation card, the page registration and this center's
#: back-link all resolve through this one constant.
CASE_HOME_ROUTE = "/cases"

#: One rendered citation line of the report's ``## Citations`` section.
_CITATION_LINE = re.compile(r"^- `([^`]+)` — cited by episodes:")

#: A version id usable inside a server-generated filename: short, no path
#: separators, no traversal.
_SAFE_VERSION_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_MIN_INSTANT = datetime.min.replace(tzinfo=timezone.utc)


def _iso(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else None


def short_hash(value: object, *, length: int = HASH_DISPLAY_LENGTH) -> str:
    """Truncate one hash for display; the full value never enters a URL."""
    text = str(value or "")
    if len(text) <= length:
        return text
    return text[:length] + "\u2026"


def _validated_version_ref(value: object) -> str:
    """Require one version id that is safe to embed in a server filename."""
    if not isinstance(value, str):
        raise TypeError("version_id 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError("version_id 必须是非空字符串")
    if not _SAFE_VERSION_REF.fullmatch(normalized):
        raise ValueError(
            "version_id 必须是不含路径分隔符的短标识符"
        )
    return normalized


def report_detail_url(version_id: object) -> str:
    """The deep link to one report version's detail page (WB-4.6).

    The same short-identifier rule as the export filename applies — a
    path-like or blank id never becomes a URL — and the id is percent-
    encoded so the link carries exactly one route segment.
    """
    normalized = _validated_version_ref(version_id)
    return "/reports/" + quote(normalized, safe="")


def report_row(version: object) -> dict[str, Any]:
    """Project one report version into a JSON-safe list row."""
    input_hash = getattr(version, "input_hash")
    markdown_hash = getattr(version, "markdown_hash")
    return {
        "version_id": getattr(version, "version_id"),
        "case_id": getattr(version, "case_id"),
        "as_of": _iso(getattr(version, "as_of", None)),
        "created_at": _iso(getattr(version, "created_at", None)),
        "trigger": getattr(version, "trigger", None),
        "parent_version_id": getattr(version, "parent_version_id", None),
        "summary_origin": getattr(version, "summary_origin", None),
        "input_hash": input_hash,
        "markdown_hash": markdown_hash,
        "input_hash_short": short_hash(input_hash),
        "markdown_hash_short": short_hash(markdown_hash),
    }


# ------------------------------------------------------ evidence backtracking


def cited_source_ids(markdown: str) -> tuple[str, ...]:
    """Read the source ids cited by the report's own Citations section.

    The ``## Citations`` section is machine-rendered from validated
    structured citations, so its ``- `source_id` — cited by episodes:``
    lines are recorded references — unlike backticked tokens elsewhere in
    the body, which stay plain text and are never treated as citations.
    """
    if not isinstance(markdown, str):
        return ()
    cited: list[str] = []
    seen: set[str] = set()
    in_section = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Citations"
            continue
        if not in_section:
            continue
        match = _CITATION_LINE.match(line)
        if match is None:
            continue
        source_id = match.group(1).strip()
        if source_id and source_id not in seen:
            seen.add(source_id)
            cited.append(source_id)
    return tuple(cited)


def evidence_query_url(source_id: str) -> str:
    """The deep link to the evidence browser for one source id."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be a non-empty string")
    return "/evidence?query=" + quote(source_id.strip(), safe="")


def _structured_locators(
    citations: tuple[object, ...],
) -> dict[str, list[dict[str, Any]]]:
    locators: dict[str, list[dict[str, Any]]] = {}
    for citation in citations:
        source_id = getattr(citation, "source_id", None)
        if not source_id:
            continue
        rows = locators.setdefault(str(source_id), [])
        for locator in tuple(getattr(citation, "evidence", ()) or ()):
            rows.append({
                "corpus_path": getattr(locator, "corpus_path", None),
                "paragraph": getattr(locator, "paragraph", None),
                "page": getattr(locator, "page", None),
                "quote": getattr(locator, "quote", None),
            })
    return locators


def _citations_view(
    version: object, markdown: str
) -> dict[str, Any]:
    """Project the version's citations without ever guessing locators."""
    structured = tuple(getattr(version, "citations", ()) or ())
    if structured:
        source_ids = [
            str(item) for item in (
                getattr(citation, "source_id", None) for citation in structured
            ) if item
        ]
        note = "该版本记录了结构化引用"
        locators = _structured_locators(structured)
    else:
        source_ids = list(cited_source_ids(markdown))
        note = (
            "引用结构不完整:该版本未持久化结构化引用;下列来源 ID 来自"
            "渲染出的 Citations 小节,不是经过校验的定位符"
        )
        locators = {}
    return {
        "structured": bool(structured),
        "source_ids": source_ids,
        "note": note,
        "query_urls": {
            source_id: evidence_query_url(source_id)
            for source_id in source_ids
        },
        "locators": locators,
    }


def locator_rows(hits: object, source_id: str) -> list[dict[str, Any]]:
    """Project search hits into locator rows for one exact source id.

    Only hits whose ``source_id`` equals the requested id are kept — a
    fuzzy neighbour in the results never masquerades as the locator.
    """
    rows: list[dict[str, Any]] = []
    for hit in tuple(hits or ()):
        if getattr(hit, "source_id", None) != source_id:
            continue
        rows.append({
            "source_id": source_id,
            "title": getattr(hit, "title", None),
            "corpus_path": getattr(hit, "path", None),
            "paragraph": getattr(hit, "paragraph", None),
            "page": getattr(hit, "page", None),
            "quote": getattr(hit, "quote", None),
        })
    return rows


# ------------------------------------------------------- detail projection


def lineage_rows(version: object, all_versions: object) -> dict[str, Any]:
    """Walk the parent chain from the current version to its root.

    The walk stops at the first parent id that is not in the ledger list
    (or a corrupt cycle) and reports ``truncated`` instead of inventing a
    shorter chain.
    """
    by_id = {
        getattr(item, "version_id", None): item
        for item in tuple(all_versions or ())
    }
    chain: list[dict[str, Any]] = []
    seen: set[object] = set()
    current_id = getattr(version, "version_id", None)
    node: object = version
    truncated = False
    while node is not None:
        node_id = getattr(node, "version_id", None)
        if node_id in seen:
            truncated = True
            break
        seen.add(node_id)
        chain.append({
            "version_id": node_id,
            "trigger": getattr(node, "trigger", None),
            "created_at": _iso(getattr(node, "created_at", None)),
            "current": node_id == current_id,
        })
        parent_id = getattr(node, "parent_version_id", None)
        if parent_id is None:
            break
        parent = by_id.get(parent_id)
        if parent is None:
            truncated = True
            break
        node = parent
    return {"chain": chain, "truncated": truncated}


def _raw_markdown_block(markdown: str) -> str:
    """The verbatim body inside a code fence (fences render as text)."""
    fence = "````" if "```" in markdown else "```"
    return f"{fence}markdown\n{markdown}\n{fence}"


def report_detail_view(
    version: object,
    *,
    lineage: dict[str, Any] | None = None,
    evidence_lookup_available: bool = False,
) -> dict[str, Any]:
    """Project one report version into JSON-safe detail page data.

    The rendered body is HTML-escaped so embedded markup can never execute;
    the raw body stays verbatim inside a code fence.  Quality layers the
    version model does not carry project as ``unknown``/``not provided`` —
    never as pass (H-4).
    """
    markdown = str(getattr(version, "markdown", "") or "")
    mechanism = getattr(version, "mechanism_status", None)
    semantic = getattr(version, "semantic_status", None)
    gap_count = getattr(version, "evidence_gap_count", None)
    if isinstance(gap_count, bool) or not isinstance(gap_count, int):
        gap_count = None
    mechanism_status = str(mechanism) if mechanism else "unknown"
    semantic_status = str(semantic) if semantic else "unknown"
    return {
        "version_id": getattr(version, "version_id"),
        "case_id": getattr(version, "case_id"),
        "as_of": _iso(getattr(version, "as_of", None)),
        "created_at": _iso(getattr(version, "created_at", None)),
        "trigger": getattr(version, "trigger", None),
        "parent_version_id": getattr(version, "parent_version_id", None),
        "summary_origin": getattr(version, "summary_origin", None),
        "input_hash": getattr(version, "input_hash"),
        "markdown_hash": getattr(version, "markdown_hash"),
        "input_hash_short": short_hash(getattr(version, "input_hash")),
        "markdown_hash_short": short_hash(getattr(version, "markdown_hash")),
        "debate_input_hash": getattr(version, "debate_input_hash", None),
        "markdown": markdown,
        "render_markdown": escape(markdown, quote=False),
        "raw_markdown_block": _raw_markdown_block(markdown),
        # The Phase C linkage back to the case home (constant route; the
        # case itself is identified by the view's case_id metadata).
        "case_home_url": CASE_HOME_ROUTE,
        "mechanism_status": mechanism_status,
        "semantic_status": semantic_status,
        "mechanism_ui": quality_ui_status(mechanism_status),
        "semantic_ui": quality_ui_status(semantic_status),
        "evidence_gap_count": gap_count,
        "evidence_gap_summary": (
            f"{gap_count} 个证据缺口"
            if gap_count is not None
            else "未提供"
        ),
        "evidence_gaps": list(getattr(version, "evidence_gaps", ()) or ()),
        "citations": _citations_view(version, markdown),
        "lineage": lineage or {"chain": [], "truncated": False},
        "evidence_lookup_available": bool(evidence_lookup_available),
    }


# ---------------------------------------------------------- PDF export


def _display_path(path: Path) -> str:
    """A display path relative to ``PRISM_HOME``, or just the filename."""
    try:
        from prism.config import PathConfig

        home = PathConfig.prism_home().resolve()
        return path.resolve().relative_to(home).as_posix()
    except (ValueError, OSError, RuntimeError):
        return path.name


def export_result_view(
    result: object, output_path: str
) -> dict[str, Any]:
    """Project one ``ReportPdfExportResult`` into JSON-safe page data."""
    path = Path(getattr(result, "path"))
    return {
        "state": "exported",
        "version_id": getattr(result, "version_id", None),
        "case_id": getattr(result, "case_id"),
        "as_of": _iso(getattr(result, "as_of", None)),
        "page_count": getattr(result, "page_count"),
        "markdown_hash": getattr(result, "markdown_hash"),
        "pdf_hash": getattr(result, "pdf_hash"),
        "output_path": str(output_path),
        "display_path": _display_path(path),
        "filename": path.name,
    }


def export_failure_view(error: BaseException) -> dict[str, Any]:
    """Map an export error to a safe failure view (CLI-consistent text).

    The typed PDF errors carry user-facing guidance (install hints,
    renderer setup, conflict refusal) and are shown verbatim — exactly what
    the CLI prints.  Anything unexpected collapses to the operation +
    exception class name: no paths, keys or exception text leak through.
    """
    if isinstance(error, ReportPdfRendererError):
        message = str(error)
        return {
            "state": "failure",
            "error_type": (
                "pdf_dependencies_missing"
                if "install with" in message
                else "pdf_render_failed"
            ),
            "message": message,
        }
    if isinstance(error, ReportPdfPathError):
        return {
            "state": "failure",
            "error_type": "pdf_path_rejected",
            "message": str(error),
        }
    if isinstance(error, ReportPdfConflictError):
        return {
            "state": "failure",
            "error_type": "pdf_conflict",
            "message": str(error),
        }
    if isinstance(error, ReportPdfValidationError):
        return {
            "state": "failure",
            "error_type": "pdf_validation_failed",
            "message": str(error),
        }
    if isinstance(error, LookupError):
        return {
            "state": "failure",
            "error_type": "version_not_found",
            "message": str(error) or "未找到报告版本",
        }
    return {
        "state": "failure",
        "error_type": type(error).__name__,
        "message": safe_error_text("导出 PDF", error),
    }


# --------------------------------------------------------------- controller


class ReportsFacade(Protocol):
    """The existing facade report operations used by the report center."""

    async def report_versions(
        self, case_id: str | None = None, *, as_of: datetime | None = None
    ) -> tuple: ...

    async def report_version(self, version_id: str) -> object: ...

    async def export_report_pdf(
        self, version_id: str, output_path: str | Path
    ) -> object: ...


class ReportCenterController:
    """View-model adapter over the facade's report operations.

    All operations are async because the facade is async; all results are
    JSON-safe view data.  Invalid inputs (blank/path-like version ids,
    blank case filters, unknown triggers, blank source ids) raise explicit
    exceptions BEFORE any facade call, and a facade failure propagates
    unchanged — an unreadable ledger is never rewritten into an empty
    list.  The optional evidence lookup uses the facade's ``search`` when
    the injected object provides it (capability-gated like the pages in
    :mod:`prism.webui.app`).
    """

    def __init__(self, api: ReportsFacade) -> None:
        for name in (
            "report_versions",
            "report_version",
            "export_report_pdf",
        ):
            if not callable(getattr(api, name, None)):
                raise TypeError(f"api must provide {name}()")
        self._api = api
        search = getattr(api, "search", None)
        self._search = search if callable(search) else None

    @property
    def evidence_lookup_available(self) -> bool:
        """Whether locator lookup through facade search is possible."""
        return self._search is not None

    async def load_versions(
        self, *, case_id: str | None = None, trigger: str | None = None
    ) -> dict[str, Any]:
        """List report versions, newest first, with the case/trigger filters.

        ``case_id`` is forwarded to the facade (a ledger filter); the
        ``trigger`` filter is presentation-layer over the projected rows.
        The ledger lists creation order (oldest first) and this view
        reverses it by ``created_at``, stable for equal timestamps.
        """
        if case_id is not None:
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError(
                    "case_id must be a non-empty string or None"
                )
            case_id = case_id.strip()
        if trigger is not None and trigger not in REPORT_TRIGGERS:
            allowed = ", ".join(REPORT_TRIGGERS)
            raise ValueError(f"trigger must be one of: {allowed}")
        versions = tuple(await self._api.report_versions(case_id=case_id))

        def _created(index: int) -> datetime:
            value = getattr(versions[index], "created_at", None)
            return value if value is not None else _MIN_INSTANT

        order = sorted(
            range(len(versions)),
            key=lambda index: (_created(index), index),
            reverse=True,
        )
        rows = [report_row(versions[index]) for index in order]
        if trigger is not None:
            rows = [row for row in rows if row["trigger"] == trigger]
        return {
            "versions": rows,
            "count": len(rows),
            "empty": not rows,
            "filtered": trigger is not None,
        }

    async def load_version(self, version_id: str) -> dict[str, Any]:
        """Load one immutable version with its metadata, body and lineage."""
        normalized = _validated_version_ref(version_id)
        version = await self._api.report_version(normalized)
        siblings = await self._api.report_versions(
            case_id=getattr(version, "case_id", None)
        )
        return report_detail_view(
            version,
            lineage=lineage_rows(version, siblings),
            evidence_lookup_available=self.evidence_lookup_available,
        )

    async def export_pdf(self, version_id: str) -> dict[str, Any]:
        """Export one version as a derived PDF through the facade.

        The output path is generated here — ``reports/exports/<id>.pdf``
        inside the facade's controlled output directory — and the caller
        supplies only the version id; the deterministic filename makes
        repeated exports hit the exporter's idempotent reuse (an existing
        file with the same markdown hash is reused, a mismatching file is
        refused).  Known export errors map to a safe typed failure view;
        invalid ids raise before any facade call.
        """
        normalized = _validated_version_ref(version_id)
        output_path = f"{EXPORT_SUBDIR}/{normalized}.pdf"
        try:
            result = await self._api.export_report_pdf(
                normalized, output_path
            )
        except Exception as error:
            return export_failure_view(error)
        return export_result_view(result, output_path)

    async def locate_source(self, source_id: str) -> dict[str, Any]:
        """Look up evidence locators for one cited source id.

        Delegates to the facade's ``search`` and keeps only exact
        ``source_id`` matches; no match is reported as zero locators, never
        as a fabricated locator.
        """
        if self._search is None:
            raise RuntimeError(
                "the injected facade does not provide evidence search for "
                "locator lookup"
            )
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        source_id = source_id.strip()
        hits = await self._search(source_id)
        rows = locator_rows(hits, source_id)
        return {"source_id": source_id, "locators": rows, "count": len(rows)}


# ------------------------------------------------------------------ pages

_REPORT_COLUMNS = [
    {"name": "version_id", "label": "版本", "field": "version_id",
     "align": "left", "sortable": True},
    {"name": "case_id", "label": "案例", "field": "case_id",
     "align": "left", "sortable": True},
    {"name": "as_of", "label": "截止时间", "field": "as_of", "align": "left"},
    {"name": "created_at", "label": "创建时间", "field": "created_at",
     "align": "left", "sortable": True},
    {"name": "trigger", "label": "触发", "field": "trigger",
     "align": "left", "sortable": True},
    {"name": "parent_version_id", "label": "父版本", "field":
     "parent_version_id", "align": "left"},
    {"name": "summary_origin", "label": "摘要来源",
     "field": "summary_origin", "align": "left"},
    {"name": "input_hash_short", "label": "输入哈希",
     "field": "input_hash_short", "align": "left"},
    {"name": "markdown_hash_short", "label": "Markdown 哈希",
     "field": "markdown_hash_short", "align": "left"},
]


def _trigger_options() -> dict[str, str]:
    return {"": "全部触发类型"} | {item: item for item in REPORT_TRIGGERS}


#: UI status word -> badge color (``partial`` is a distinct warning color,
#: never the success color; H-4/WB-4.5).
_BADGE_COLORS = {
    "成功": "positive",
    "部分完成": "warning",
    "失败": "negative",
    "未知": "grey-6",
    "加载中": "grey-6",
    "就绪": "blue-grey-6",
}


def _metadata_markdown(view: dict[str, Any]) -> str:
    parent = view["parent_version_id"]
    lines = [
        f"### `{view['version_id']}`",
        f"- 案例: {view['case_id']}",
        f"- 截止时间: {view['as_of']}",
        f"- 创建时间: {view['created_at']}",
        f"- 触发: **{view['trigger']}**",
        (
            f"- 父版本: `{parent}`"
            if parent
            else "- 父版本: _无(初始)_"
        ),
        f"- 摘要来源: **{view['summary_origin']}**",
        f"- 输入哈希: `{view['input_hash']}`",
        f"- Markdown 哈希: `{view['markdown_hash']}`",
        (
            f"- 辩论输入哈希: `{view['debate_input_hash']}`"
            if view["debate_input_hash"]
            else "- 辩论输入哈希: _无_"
        ),
        "#### 版本谱系(当前 \u2192 根)",
    ]
    for row in view["lineage"]["chain"]:
        marker = " **(当前)**" if row["current"] else ""
        lines.append(
            f"- `{row['version_id']}` ({row['trigger']}, "
            f"{row['created_at']}){marker}"
        )
    if view["lineage"]["truncated"]:
        lines.append(
            "- _谱系被截断:某个父版本 ID 不在账本中_"
        )
    return "\n".join(lines)


def _quality_markdown(view: dict[str, Any]) -> str:
    return "\n".join((
        f"- 机制: **{view['mechanism_status']}**",
        f"- 语义: **{view['semantic_status']}**",
        f"- 摘要来源: **{view['summary_origin']}**",
        f"- 证据缺口: **{view['evidence_gap_summary']}**",
    ))


def _locator_label(row: dict[str, Any]) -> str:
    where = []
    if row.get("paragraph") is not None:
        where.append(f"第 {row['paragraph']} 段")
    if row.get("page") is not None:
        where.append(f"第 {row['page']} 页")
    at = f" ({'; '.join(where)})" if where else ""
    return f"{row['corpus_path']}{at}"


def _citations_markdown(view: dict[str, Any]) -> str:
    citations = view["citations"]
    lines = [f"_{citations['note']}_"]
    if not citations["source_ids"]:
        lines.append("- 该版本未发现来源 ID 引用")
    for source_id in citations["source_ids"]:
        if citations["structured"]:
            for locator in citations["locators"].get(source_id, ()):
                quote = (
                    f': "{locator["quote"]}"' if locator.get("quote") else ""
                )
                lines.append(
                    f"- `{source_id}` — {_locator_label(locator)}{quote}"
                )
        else:
            lines.append(
                f"- `{source_id}` — 检索证据: "
                f"{citations['query_urls'][source_id]}"
            )
    return "\n".join(lines)


def _locators_markdown(found: dict[str, Any]) -> str:
    lines = [
        f"`{found['source_id']}` 的证据定位符: "
        f"找到 {found['count']} 个"
    ]
    for row in found["locators"]:
        lines.append(f"- `{row['source_id']}` — {_locator_label(row)}")
        if row.get("quote"):
            lines.append(f'  - 引文: "{row["quote"]}"')
    if not found["locators"]:
        lines.append(
            "- 证据库中没有与该来源 ID 匹配的定位符;"
            "请打开 /evidence 深链手动检索"
        )
    return "\n".join(lines)


def _export_success_markdown(view: dict[str, Any]) -> str:
    return "\n".join((
        "**已导出**(派生 PDF;已保存版本不变)",
        f"- 页数: {view['page_count']} 页",
        f"- 输出: `{view['display_path']}`",
        f"- 版本: `{view['version_id']}`",
        f"- Markdown 哈希: `{view['markdown_hash']}`",
        f"- PDF 哈希: `{view['pdf_hash']}`",
    ))


def _export_failure_markdown(view: dict[str, Any]) -> str:
    return "\n".join((
        f"**失败** — {view['error_type']}",
        view["message"],
    ))


def build_report_pages(
    controller: ReportCenterController, ui: Any, *,
    title: str = "PRISM 报告中心",
) -> tuple[Any, Any]:
    """Register the ``/reports`` list and detail pages on ``ui``.

    The ``ui`` module is injected so the page construction — filters, the
    versions table, the read-only detail renderer, the evidence backtrace
    panel and the export button — is a seam testable without NiceGUI
    installed.  Every handler delegates to the controller and reports
    explicit errors in the message label; a load failure never renders as
    "no report versions", and the detail body is only ever the saved,
    HTML-escaped Markdown of an immutable version.
    """

    @ui.page("/reports")
    def reports_page() -> None:
        message = ui.label("加载报告版本开始。")
        status_md = ui.markdown("_尚未加载报告版本_")

        def _report(text: str) -> None:
            message.text = text
            message.update()

        async def _refresh(event: Any = None) -> None:
            case = (case_input.value or "").strip()
            trigger = trigger_select.value or ""
            try:
                payload = await controller.load_versions(
                    case_id=case or None, trigger=trigger or None
                )
            except Exception as error:
                _report(safe_error_text("加载报告", error))
                status_md.content = (
                    "**失败** — 无法加载报告版本"
                )
                status_md.update()
                return
            reports_table.rows = payload["versions"]
            reports_table.update()
            status_md.content = (
                "_暂无报告版本_"
                if payload["empty"]
                else f"_{payload['count']} 个报告版本,最新在前_"
            )
            status_md.update()
            _report(f"已加载 {payload['count']} 个报告版本")

        async def _open_selected(event: Any = None) -> None:
            rows = list(getattr(event, "args", None) or ())
            if not rows:
                return
            row = rows[0]
            version_id = (
                row.get("version_id", "") if isinstance(row, dict) else ""
            )
            if not version_id:
                return
            route = f"/reports/{version_id}"
            navigate = getattr(ui, "navigate", None)
            opener = getattr(navigate, "to", None)
            if callable(opener):
                opener(route)
            else:
                _report(f"已选择 {version_id};打开 {route}")

        with ui.card().classes("w-full"):
            ui.label("报告筛选").classes("text-bold")
            with ui.row():
                case_input = ui.input(
                    label="案例(精确 ID,可选)",
                    placeholder="case-rates",
                )
                trigger_select = ui.select(
                    options=_trigger_options(), value="", label="触发类型"
                )
            with ui.row():
                ui.button("刷新报告", on_click=_refresh)

        with ui.card().classes("w-full"):
            reports_table = ui.table(
                columns=_REPORT_COLUMNS,
                rows=[],
                selection="single",
                on_select=_open_selected,
            )

    @ui.page("/reports/{version_id}")
    async def report_detail_page(version_id: str) -> None:
        message = ui.label(f"正在加载报告版本 {version_id} \u2026")
        status_md = ui.markdown("_加载中\u2026_")

        def _report(text: str) -> None:
            message.text = text
            message.update()

        async def _export(event: Any = None) -> None:
            try:
                result = await controller.export_pdf(version_id)
            except (TypeError, ValueError) as error:
                _report(f"导出被拒绝: {error}")
                return
            except Exception as error:
                _report(safe_error_text("导出 PDF", error))
                return
            if result["state"] == "exported":
                export_md.content = _export_success_markdown(result)
                export_md.update()
                _report(
                    f"PDF 已导出: {result['filename']} "
                    f"({result['page_count']} 页)"
                )
            else:
                export_md.content = _export_failure_markdown(result)
                export_md.update()
                _report(f"PDF 导出失败: {result['error_type']}")

        async def _locate(source_id: str) -> None:
            try:
                found = await controller.locate_source(source_id)
            except Exception as error:
                _report(safe_error_text("定位证据", error))
                return
            locators_md.content = _locators_markdown(found)
            locators_md.update()
            _report(f"{found['source_id']} 的 {found['count']} 个定位符")

        def _make_locate_handler(source_id: str) -> Any:
            async def _handler(event: Any = None) -> None:
                await _locate(source_id)

            return _handler

        metadata_card = ui.card().classes("w-full")
        with metadata_card:
            ui.label("版本元数据与谱系").classes("text-bold")
            metadata_md = ui.markdown("_未加载_")
            with ui.row() as quality_row:
                quality_md = ui.markdown("_未加载_")

        with ui.card().classes("w-full"):
            ui.label(
                "报告正文(只读;版本不可变)"
            ).classes("text-bold")
            body_md = ui.markdown("_未加载_")
            with ui.expansion("原始 Markdown(只读)"):
                raw_md = ui.markdown("_未加载_")

        with ui.card().classes("w-full"):
            ui.label("证据回溯").classes("text-bold")
            citations_md = ui.markdown("_未加载_")
            with ui.row() as citations_row:
                pass
            locators_md = ui.markdown(
                "_本会话尚未进行证据查询_"
            )

        with ui.card().classes("w-full"):
            ui.label(
                "导出 PDF(派生产物;版本本身不变)"
            ).classes("text-bold")
            ui.button("导出 PDF", on_click=_export)
            export_md = ui.markdown("_本会话尚未尝试导出_")

        try:
            view = await controller.load_version(version_id)
        except Exception as error:
            status_md.content = (
                "**失败** — 无法加载该报告版本"
            )
            status_md.update()
            _report(safe_error_text("加载报告版本", error))
            return

        status_md.content = (
            f"**就绪** — 报告版本 `{view['version_id']}` "
            "(不可变)"
        )
        # Phase C linkage: the case this version belongs to is explored on
        # the case home, so the detail page links back there.
        with metadata_card:
            ui.link(
                f"返回案例主页({view['case_id']})",
                view["case_home_url"],
            )
        metadata_md.content = _metadata_markdown(view)
        quality_md.content = _quality_markdown(view)
        citations_md.content = _citations_markdown(view)
        body_md.content = view["render_markdown"]
        raw_md.content = view["raw_markdown_block"]
        with quality_row:
            for label_text, value, ui_status in (
                ("机制", view["mechanism_status"],
                 view["mechanism_ui"]),
                ("语义", view["semantic_status"], view["semantic_ui"]),
            ):
                ui.badge(
                    f"{label_text}: {value}",
                    color=_BADGE_COLORS.get(ui_status, "grey-6"),
                )
        if view["citations"]["source_ids"]:
            with citations_row:
                for source_id in view["citations"]["source_ids"]:
                    if view["evidence_lookup_available"]:
                        ui.button(
                            f"定位 {source_id}",
                            on_click=_make_locate_handler(source_id),
                        )
                    ui.link(
                        f"/evidence?query={source_id}",
                        view["citations"]["query_urls"][source_id],
                    )
        for element in (
            status_md, metadata_md, quality_md, citations_md, body_md,
            raw_md,
        ):
            element.update()
        _report(f"报告版本 {view['version_id']} 已加载")

    return reports_page, report_detail_page


__all__ = [
    "EXPORT_SUBDIR",
    "HASH_DISPLAY_LENGTH",
    "REPORT_TRIGGERS",
    "ReportsFacade",
    "ReportCenterController",
    "build_report_pages",
    "cited_source_ids",
    "evidence_query_url",
    "export_failure_view",
    "export_result_view",
    "lineage_rows",
    "locator_rows",
    "report_detail_url",
    "report_detail_view",
    "report_row",
    "short_hash",
]
