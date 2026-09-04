"""Derived report PDFs rendered by Edge/Chromium and verified with pypdf.

Markdown remains the source of truth. This module only adapts mature
components: Python-Markdown renders HTML, a headless Chromium renderer prints
it, and pypdf reads the result back before it can become a project artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any, Protocol

from prism.config import PathConfig

from .models import ReportDocument

if TYPE_CHECKING:
    from .ledger import ReportVersion


_PDF_RENDERER_ENV = "PRISM_PDF_RENDERER"
_MARKDOWN_EXTENSIONS = ("tables", "fenced_code", "sane_lists")
_REQUIRED_SECTIONS = ("Executive Summary", "Timeline Stages", "Citations")
# BMP Han ranges only: enough to prove Chinese report text printed as real
# glyphs instead of tofu, and cheap to scan over the Markdown source.
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_AS_OF_LINE = re.compile(r"^[-*]\s+As of:\s*(\S+)\s*$", re.MULTILINE)
_PDF_INSTALL_HINT = "install with 'pip install -e \".[pdf]\"'"
_PDF_CSS = """\
:root { color-scheme: light; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
    "Noto Sans CJK", "Source Han Sans SC", sans-serif;
  font-size: 10pt;
  line-height: 1.45;
  color: #111;
  margin: 0;
}
main { padding: 18mm 16mm; }
h1, h2, h3 { line-height: 1.25; break-after: avoid; }
h1 { font-size: 20pt; }
h2 { font-size: 14pt; margin-top: 1.2em; }
h3 { font-size: 11.5pt; }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.75em 0;
  font-size: 8.5pt;
}
th, td { border: 0.5pt solid #666; padding: 3pt 4pt; text-align: left; }
th { background: #eee; }
code, pre {
  font-family: Consolas, "Courier New", "Noto Sans Mono CJK SC", monospace;
}
code { font-size: 8.5pt; }
pre {
  border: 0.5pt solid #999;
  margin: 0.75em 0;
  padding: 4pt;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
blockquote { border-left: 2pt solid #999; margin-left: 0; padding-left: 6pt; }
"""


class ReportPdfPathError(ValueError):
    """The requested output path is not a safe project-relative PDF path."""


class ReportPdfConflictError(FileExistsError):
    """The output exists with different content; PRISM refuses to overwrite."""


class ReportPdfRendererError(RuntimeError):
    """No renderer is available, or the configured renderer failed."""


class ReportPdfValidationError(RuntimeError):
    """The rendered PDF did not pass read-back validation."""


@dataclass(frozen=True, slots=True)
class ReportPdfExportResult:
    """Auditable metadata for one derived PDF."""

    path: Path
    version_id: str | None
    case_id: str
    as_of: datetime
    markdown_hash: str
    pdf_hash: str
    page_count: int


class PdfRenderer(Protocol):
    """One injected HTML-to-PDF rendering seam."""

    def render(self, html: str, output_path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class _SourceDetails:
    version_id: str | None
    case_id: str
    as_of: datetime
    markdown: str
    markdown_hash: str


def find_chromium_executable() -> Path | None:
    """Find an explicit or commonly installed Edge/Chromium executable."""

    configured = os.environ.get(_PDF_RENDERER_ENV)
    if configured and configured.strip():
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
        on_path = shutil.which(configured)
        if on_path is not None:
            return Path(on_path)
        return candidate

    if os.name == "nt":
        roots: list[Path] = []
        for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            value = os.environ.get(variable)
            if value:
                roots.append(Path(value))
        candidates = [
            root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            for root in roots
        ] + [
            root / "Google" / "Chrome" / "Application" / "chrome.exe"
            for root in roots
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        commands = ("msedge.exe", "chrome.exe")
    else:
        commands = (
            "microsoft-edge",
            "google-chrome",
            "chromium",
            "chromium-browser",
        )
    for command in commands:
        found = shutil.which(command)
        if found is not None:
            return Path(found)
    return None


class EdgePdfRenderer:
    """Print local HTML with a headless Edge or compatible Chromium build."""

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        candidate = (
            Path(executable).expanduser()
            if executable is not None
            else find_chromium_executable()
        )
        if candidate is None:
            raise ReportPdfRendererError(
                "no PDF renderer is available; set "
                f"{_PDF_RENDERER_ENV} to Microsoft Edge or a compatible "
                "Chromium executable"
            )
        if not candidate.is_file():
            configured = candidate if executable is not None else None
            detail = (
                f"configured PDF renderer does not exist: {configured}"
                if configured is not None
                else "no PDF renderer is available; set "
                f"{_PDF_RENDERER_ENV} to Microsoft Edge or a compatible "
                "Chromium executable"
            )
            raise ReportPdfRendererError(detail)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.executable = candidate.resolve()
        self.timeout = float(timeout)

    def render(self, html: str, output_path: Path) -> None:
        """Render one in-memory HTML document to ``output_path``."""

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="prism-report-pdf-") as temporary:
            html_path = Path(temporary) / "report.html"
            html_path.write_text(html, encoding="utf-8")
            command = [
                str(self.executable),
                "--headless",
                # A throwaway profile avoids contending with a running
                # desktop browser's default-profile lock and never touches
                # the user's real profile data.
                f"--user-data-dir={Path(temporary) / 'profile'}",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--no-first-run",
                "--no-pdf-header-footer",
                f"--print-to-pdf={output_path}",
                html_path.as_uri(),
            ]
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.timeout,
                    check=True,
                )
            except subprocess.TimeoutExpired as error:
                raise ReportPdfRendererError(
                    f"PDF renderer {self.executable.name} timed out after "
                    f"{self.timeout:g} seconds"
                ) from error
            except subprocess.CalledProcessError as error:
                raise ReportPdfRendererError(
                    f"PDF renderer {self.executable.name} failed with exit "
                    f"code {error.returncode}"
                ) from error
            except OSError as error:
                raise ReportPdfRendererError(
                    f"could not start PDF renderer {self.executable.name}: "
                    f"{type(error).__name__}"
                ) from error
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ReportPdfRendererError(
                f"PDF renderer {self.executable.name} did not create a PDF"
            )


def _markdown_renderer():
    try:
        from markdown import markdown
    except ImportError as error:
        raise ReportPdfRendererError(
            "PDF export requires the optional 'markdown' package; "
            f"{_PDF_INSTALL_HINT}"
        ) from error
    return markdown


def _pypdf_classes() -> tuple[type[Any], type[Any]]:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as error:
        raise ReportPdfRendererError(
            "PDF export requires the optional 'pypdf' package; "
            f"{_PDF_INSTALL_HINT}"
        ) from error
    return PdfReader, PdfWriter


def render_report_html(markdown: str, case_id: str) -> str:
    """Render report Markdown to self-contained, path-free HTML."""

    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("markdown must be a non-empty string")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id must be a non-empty string")
    render = _markdown_renderer()
    escaped_markdown = html.escape(markdown, quote=False)
    body = render(
        escaped_markdown,
        extensions=list(_MARKDOWN_EXTENSIONS),
        output_format="html5",
    )
    title = html.escape(f"Evolution Report: {case_id}", quote=True)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        # Report Markdown is untrusted input: forbid every subresource fetch
        # (remote beacons, local files via ![](file:///...)) so printing stays
        # offline and cannot embed local file content into the PDF. Inline
        # styles are the one allowance; scripts stay blocked by default-src.
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src "
        "'none'; style-src 'unsafe-inline'\">\n"
        f"<title>{title}</title>\n"
        f"<style>{_PDF_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"<main>{body}</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _resolve_output_path(paths: PathConfig, output_path: str | Path) -> Path:
    if not isinstance(output_path, (str, os.PathLike)):
        raise TypeError("output_path must be a path-like value or string")
    candidate = Path(output_path)
    if (
        candidate.is_absolute()
        or candidate.drive
        or candidate.root
        or any(part == ".." for part in candidate.parts)
    ):
        raise ReportPdfPathError(
            "output_path must be relative to the output directory and must "
            "not contain '..'"
        )
    if candidate.suffix.lower() != ".pdf":
        raise ReportPdfPathError("output_path must end with .pdf")

    base = paths.output_dir.resolve()
    target = (base / candidate).resolve()
    if not target.is_relative_to(base) or target == base:
        raise ReportPdfPathError(
            "output_path must resolve to a PDF file inside the output directory"
        )
    return target


def _normalize_pdf_text(value: str) -> str:
    return "".join(value.split())


def _cjk_probe(markdown: str) -> str | None:
    """Longest Han run whose glyphs must survive printing without tofu."""

    runs = _CJK_RUN.findall(markdown)
    return max(runs, key=len) if runs else None


def _inspect_pdf(source: str | os.PathLike[str] | bytes) -> tuple[Any, int, str]:
    reader_class, _ = _pypdf_classes()
    try:
        reader = (
            reader_class(io.BytesIO(source))
            if isinstance(source, bytes)
            else reader_class(str(source))
        )
        page_count = len(reader.pages)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:
        raise ReportPdfValidationError(
            "renderer did not produce a valid PDF readable by pypdf"
        ) from error
    normalized = _normalize_pdf_text(text)
    if page_count < 1 or not normalized:
        raise ReportPdfValidationError(
            "renderer produced an empty PDF or a PDF without extractable text"
        )
    return reader, page_count, normalized


def _as_of_alternatives(markdown: str, as_of: datetime) -> tuple[str, ...]:
    """Every spelling of the cutoff the PDF may legitimately contain.

    The ledger persists as_of UTC-normalized while the report Markdown keeps
    the writer's original offset, so a reloaded version and its Markdown spell
    the same instant differently. The Markdown's own ``As of`` value is always
    accepted; if it parses as a different instant the document itself is
    inconsistent and must not export.
    """

    spellings = {
        as_of.isoformat(),
        as_of.astimezone(timezone.utc).isoformat(),
    }
    rendered = _AS_OF_LINE.search(markdown)
    if rendered is not None:
        spellings.add(rendered.group(1))
        try:
            parsed = datetime.fromisoformat(rendered.group(1))
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.utcoffset() is not None
            and parsed.astimezone(timezone.utc) != as_of.astimezone(timezone.utc)
        ):
            raise ReportPdfValidationError(
                "markdown 'As of' does not match the version cutoff"
            )
    return tuple(sorted(spellings))


def _required_pdf_groups(
    markdown: str, case_id: str, as_of: datetime
) -> tuple[tuple[str, ...], ...]:
    """Required PDF strings, each as a group of acceptable spellings."""

    groups: list[tuple[str, ...]] = [
        (f"Evolution Report: {case_id}",),
        (case_id,),
        ("As of:",),
        _as_of_alternatives(markdown, as_of),
    ]
    groups.extend((section,) for section in _REQUIRED_SECTIONS)
    if "## Debate Interpretation" in markdown:
        groups.append(("Debate Interpretation",))
    cjk = _cjk_probe(markdown)
    if cjk is not None:
        groups.append((cjk,))
    return tuple(groups)


def _validate_pdf_text(
    normalized_text: str,
    markdown: str,
    case_id: str,
    as_of: datetime,
) -> None:
    missing = [
        alternatives[0]
        for alternatives in _required_pdf_groups(markdown, case_id, as_of)
        if not any(
            _normalize_pdf_text(spelling) in normalized_text
            for spelling in alternatives
        )
    ]
    if missing:
        joined = ", ".join(repr(value) for value in missing)
        raise ReportPdfValidationError(
            f"rendered PDF failed text validation; missing {joined}"
        )


def _finalize_pdf(
    payload: bytes,
    *,
    case_id: str,
    markdown_hash: str,
    version_id: str | None,
) -> tuple[bytes, int, str]:
    _, writer_class = _pypdf_classes()
    _, _, normalized_text = _inspect_pdf(payload)
    text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    try:
        writer = writer_class(clone_from=io.BytesIO(payload))
        writer.add_metadata(
            {
                "/Creator": "PRISM",
                "/Producer": "PRISM",
                "/Title": f"Evolution Report: {case_id}",
                "/PRISMCaseID": case_id,
                "/PRISMVersionID": version_id or "",
                "/PRISMMarkdownHash": markdown_hash,
                "/PRISMTextHash": text_hash,
            }
        )
        output = io.BytesIO()
        writer.write(output)
        final_payload = output.getvalue()
    except Exception as error:
        raise ReportPdfValidationError(
            "could not finalize the rendered PDF with pypdf"
        ) from error
    _, page_count, final_text = _inspect_pdf(final_payload)
    if final_text != normalized_text:
        raise ReportPdfValidationError("PDF text changed while adding metadata")
    return final_payload, page_count, text_hash


def _source_details(source: object) -> _SourceDetails:
    if isinstance(source, ReportDocument):
        return _SourceDetails(
            version_id=None,
            case_id=source.case_id,
            as_of=source.as_of,
            markdown=source.markdown,
            markdown_hash=hashlib.sha256(
                source.markdown.encode("utf-8")
            ).hexdigest(),
        )

    required = ("version_id", "case_id", "as_of", "markdown", "markdown_hash")
    if all(hasattr(source, name) for name in required):
        # ReportVersion is duck-typed so this adapter never imports the ledger
        # and recreates the old circular dependency.
        return _SourceDetails(
            version_id=getattr(source, "version_id"),
            case_id=getattr(source, "case_id"),
            as_of=getattr(source, "as_of"),
            markdown=getattr(source, "markdown"),
            markdown_hash=getattr(source, "markdown_hash"),
        )
    raise TypeError("source must be a ReportDocument or ReportVersion")


def _atomic_create(target: Path, payload: bytes) -> None:
    """Create one target without ever replacing an existing file."""

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                # On Windows this is atomic and fails when target exists.
                os.rename(temporary_name, target)
            else:
                os.link(temporary_name, target)
                os.unlink(temporary_name)
        except FileExistsError as error:
            raise ReportPdfConflictError(
                "output path already contains different content; refusing to "
                "overwrite"
            ) from error
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


class ReportPdfExporter:
    """Export report Markdown as validated, project-local PDF files."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        renderer: PdfRenderer | str | Path | None = None,
    ) -> None:
        if not isinstance(paths, PathConfig):
            raise TypeError("paths must be a PathConfig")
        self._paths = paths
        if isinstance(renderer, (str, os.PathLike)):
            self._renderer: PdfRenderer = EdgePdfRenderer(renderer)
        elif renderer is None:
            self._renderer = None
        else:
            if not callable(getattr(renderer, "render", None)):
                raise TypeError("renderer must provide render()")
            self._renderer = renderer

    def export_pdf(
        self,
        source: ReportDocument | ReportVersion,
        output_path: str | Path,
    ) -> ReportPdfExportResult:
        details = _source_details(source)
        target = _resolve_output_path(self._paths, output_path)
        if target.exists():
            if target.is_dir():
                raise ReportPdfConflictError(
                    f"output path is a directory: {target.name}"
                )
            return self._existing_result(target, details)

        payload, page_count, _ = self._render(details)
        _atomic_create(target, payload)
        return ReportPdfExportResult(
            path=target,
            version_id=details.version_id,
            case_id=details.case_id,
            as_of=details.as_of,
            markdown_hash=details.markdown_hash,
            pdf_hash=hashlib.sha256(payload).hexdigest(),
            page_count=page_count,
        )

    def export_document(
        self, document: ReportDocument, output_path: str | Path
    ) -> ReportPdfExportResult:
        return self.export_pdf(document, output_path)

    def export_version(
        self, version: ReportVersion, output_path: str | Path
    ) -> ReportPdfExportResult:
        return self.export_pdf(version, output_path)

    def _renderer_for_export(self) -> PdfRenderer:
        return self._renderer or EdgePdfRenderer()

    def _render(self, details: _SourceDetails) -> tuple[bytes, int, str]:
        html_document = render_report_html(details.markdown, details.case_id)
        with tempfile.TemporaryDirectory(prefix="prism-report-pdf-") as temporary:
            rendered_path = Path(temporary) / "report.pdf"
            self._renderer_for_export().render(html_document, rendered_path)
            if not rendered_path.is_file():
                raise ReportPdfRendererError("renderer did not create a PDF")
            payload = rendered_path.read_bytes()

        _, _, normalized_text = _inspect_pdf(payload)
        _validate_pdf_text(
            normalized_text, details.markdown, details.case_id, details.as_of
        )
        return _finalize_pdf(
            payload,
            case_id=details.case_id,
            markdown_hash=details.markdown_hash,
            version_id=details.version_id,
        )

    def _existing_result(
        self, target: Path, details: _SourceDetails
    ) -> ReportPdfExportResult:
        try:
            reader, page_count, normalized_text = _inspect_pdf(target)
        except ReportPdfValidationError as error:
            raise ReportPdfConflictError(
                "output path already contains different content; refusing to "
                "overwrite"
            ) from error
        metadata = reader.metadata or {}
        existing_hash = metadata.get("/PRISMMarkdownHash")
        text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        if (
            existing_hash != details.markdown_hash
            or metadata.get("/PRISMTextHash") != text_hash
        ):
            raise ReportPdfConflictError(
                "output path already contains different content; refusing to "
                "overwrite"
            )
        _validate_pdf_text(
            normalized_text, details.markdown, details.case_id, details.as_of
        )
        return ReportPdfExportResult(
            path=target,
            version_id=details.version_id,
            case_id=details.case_id,
            as_of=details.as_of,
            markdown_hash=details.markdown_hash,
            pdf_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
            page_count=page_count,
        )


__all__ = [
    "EdgePdfRenderer",
    "PdfRenderer",
    "ReportPdfConflictError",
    "ReportPdfExportResult",
    "ReportPdfExporter",
    "ReportPdfPathError",
    "ReportPdfRendererError",
    "ReportPdfValidationError",
    "find_chromium_executable",
    "render_report_html",
]
