"""Native Chinese (zh-CN) report generation with an explicit ``--lang``.

Real-run finding (``docs/case-driven-auto-research-loop.md`` §12.8): the
earlier zh-CN branch broke 20 existing tests by flipping the default
language and shipped a half-wired chain.  The settled contract is:

* default language stays ``en`` — every existing behavior is unchanged;
* ``language="zh-CN"`` is an explicit choice on
  ``report_case``/``save_report_version``/``rebuild_report`` and
  ``--lang zh-CN`` on ``prism report``/``prism rebuild-report``;
* fallback template, LLM summarize prompt, ``ReportVersion.language`` and
  ``input_hash`` all agree; zh and en versions of the same analysis are
  independent immutable versions;
* old ledgers migrate additively and legacy rows read back as ``en``;
* identifiers (case_id/source_id/episode_key), enum values and verbatim
  quotes are never translated; citation/time/source/case validation is not
  relaxed for Chinese.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pytest

from prism.analyzer import (
    ChangeReason,
    EvidenceGap,
    EvolutionAnalysis,
    OpenQuestion,
    TimelineStage,
    TurningPoint,
)
from prism.api import PrismAPI
from prism.cli import build_parser, handle_report, handle_rebuild_report
from prism.config import PathConfig
from prism.llm import Completion
from prism.report import ReportService
from prism.report.ledger import TABLE, ReportVersionLedger

UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
CASE_ID = "stress-tolerant-hnad-strains-2026"
CASE_EPISODE = "case-hnad-2026"
NODE_EPISODE = "node-w30-fulltext"


def make_analysis(**overrides):
    stage = TimelineStage(
        episode_key=NODE_EPISODE,
        kind="evolution_node",
        layer="fact",
        summary="Pannonibacter sp. W30 full text was ingested.",
        valid_at=datetime(2026, 8, 20, tzinfo=UTC),
        invalid_at=None,
        reference_time=datetime(2026, 8, 20, tzinfo=UTC),
        source_ids=("mat_w30",),
        node_type="interpretation",
        confidence=0.9,
        provenance_type="explicit",
        evidence_role="primary_observation",
    )
    values = {
        "case_id": CASE_ID,
        "as_of": AS_OF,
        "case_type": "academic_discourse",
        "stages": (
            TimelineStage(
                episode_key=CASE_EPISODE,
                kind="evolution_case",
                layer="fact",
                summary="Case tracks stress-tolerant HN-AD strains.",
                valid_at=datetime(2026, 8, 1, tzinfo=UTC),
                invalid_at=None,
                reference_time=datetime(2026, 8, 1, tzinfo=UTC),
                source_ids=("mat_review",),
            ),
            stage,
        ),
        "turning_points": (
            TurningPoint(
                NODE_EPISODE,
                "publication",
                datetime(2026, 8, 20, tzinfo=UTC),
                "W30 mechanism evidence entered the graph.",
                ("mat_w30",),
            ),
        ),
        "change_reasons": (
            ChangeReason(
                NODE_EPISODE,
                "new_evidence",
                "fact_change",
                datetime(2026, 8, 20, tzinfo=UTC),
                "New full-text evidence arrived.",
                ("mat_w30",),
            ),
        ),
        "evidence_gaps": (
            EvidenceGap("missing_primary_source", "HA2 remains abstract-only."),
        ),
        "open_questions": (
            OpenQuestion(
                NODE_EPISODE,
                "uncertain_claim",
                "Is the reactor result reproducible at scale?",
                "Reviewer",
                datetime(2026, 8, 20, tzinfo=UTC),
                ("mat_w30",),
            ),
        ),
    }
    values.update(overrides)
    return EvolutionAnalysis(**values)


class FakeRouter:
    def __init__(self, payload=None, *, text=None, error=None):
        self._payload = payload
        self._text = text
        self._error = error
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        if self._error is not None:
            raise self._error
        text = self._text if self._text is not None else json.dumps(self._payload)
        return Completion(text=text, provider="fake", model="fake-model")


def zh_payload():
    return {
        "summary": "该案例在 2026 年 8 月新增了 W30 机制的全文证据。",
        "key_findings": ["W30 机制证据已入图。"],
        "turning_points": ["W30 全文证据进入时间线。"],
        "causal_chain": ["新全文证据到达后时间线更新。"],
        "uncertainties": ["HA2 仍只有摘要。"],
        "citations": [
            {"episode_keys": [NODE_EPISODE], "source_ids": ["mat_w30"]},
        ],
    }


def run_report(analysis, router=None, **kwargs):
    return asyncio.run(ReportService(router).report(analysis, **kwargs))


# --- defaults and validation --------------------------------------------------


def test_default_language_stays_english_and_output_is_unchanged():
    doc = run_report(make_analysis())
    assert doc.language == "en"
    markdown = doc.markdown
    assert f"# Evolution Report: {CASE_ID}" in markdown
    assert "## Executive Summary" in markdown
    assert "## Timeline Stages" in markdown
    assert "## Citations" in markdown
    assert "deterministic fallback" in markdown
    assert "演变报告" not in markdown


def test_invalid_language_is_rejected():
    with pytest.raises(ValueError, match="language"):
        run_report(make_analysis(), language="zh")
    with pytest.raises(ValueError, match="language"):
        run_report(make_analysis(), language="fr-FR")


# --- zh-CN deterministic template ----------------------------------------------


def test_chinese_report_renders_native_template_sections():
    doc = run_report(make_analysis(), language="zh-CN")

    assert doc.language == "zh-CN"
    markdown = doc.markdown
    for expected in (
        f"# 演变报告：{CASE_ID}",
        "- 案例 ID:",
        "- 案例类型:",
        "- 记录状态:",
        "- 截至时间:",
        "## 执行摘要",
        "确定性回退",
        "## 时间线阶段",
        "## 已失效事实",
        "## 修订与冲突关系",
        "## 关键转折点",
        "## 变化原因",
        "## 证据缺口",
        "## 待解问题",
        "## 引用与证据定位",
    ):
        assert expected in markdown
    # No English template sections remain.
    for forbidden in (
        "# Evolution Report:",
        "## Executive Summary",
        "## Timeline Stages",
        "## Citations",
    ):
        assert forbidden not in markdown


def test_chinese_report_keeps_identifiers_enums_and_records_verbatim():
    doc = run_report(make_analysis(), language="zh-CN")
    markdown = doc.markdown

    for identifier in (CASE_ID, CASE_EPISODE, NODE_EPISODE, "mat_w30", "mat_review"):
        assert identifier in markdown
    # Enum values stay untranslated (documented boundary).
    assert "primary_observation" in markdown
    assert "new_evidence" in markdown
    assert "missing_primary_source" in markdown
    assert "uncertain_claim" in markdown
    # Fallback summary is Chinese but never claims a non-fallback origin.
    assert "确定性回退" in markdown
    assert doc.summary.origin == "fallback"
    assert "W30" in doc.summary.summary or "案例" in doc.summary.summary


def test_chinese_fallback_summary_counts_are_chinese_but_honest():
    empty = make_analysis(stages=(), turning_points=(), change_reasons=())
    doc = run_report(empty, language="zh-CN")
    # The empty-timeline state is stated honestly in Chinese, never dressed
    # up as a completed analysis.
    assert "没有时间线条目" in doc.summary.summary
    assert "确定性回退" in doc.markdown


def test_chinese_llm_prompt_requests_chinese_without_relaxing_validation():
    router = FakeRouter(zh_payload())
    doc = run_report(make_analysis(), router, language="zh-CN")

    (role, prompt) = router.calls[0]
    assert role == "summarize_report"
    assert "zh-CN" in prompt
    assert "episode_key" in prompt  # identifiers must stay untranslated
    assert doc.summary.origin == "llm"
    assert doc.summary.summary == "该案例在 2026 年 8 月新增了 W30 机制的全文证据。"
    assert doc.language == "zh-CN"
    assert "模型提炼" in doc.markdown


def test_chinese_llm_prompt_is_not_sent_for_english_reports():
    router = FakeRouter(zh_payload())
    run_report(make_analysis(), router)
    prompt = router.calls[0][1]
    assert "zh-CN" not in prompt


def test_chinese_invalid_llm_payload_still_falls_back_deterministically():
    router = FakeRouter({"summary": "只有 summary，不完整"})
    doc = run_report(make_analysis(), router, language="zh-CN")
    assert doc.summary.origin == "fallback"
    assert "## 时间线阶段" in doc.markdown
    assert "## 引用与证据定位" in doc.markdown


def test_chinese_document_is_immutable_and_distinct_from_english():
    zh = run_report(make_analysis(), language="zh-CN")
    en = run_report(make_analysis())
    assert zh != en
    assert zh.markdown != en.markdown
    with pytest.raises(Exception):
        zh.language = "en"


# --- ledger: language in input_hash and additive migration ---------------------


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


def test_input_hash_separates_languages_but_is_stable_within_one(tmp_path: Path):
    ledger = ReportVersionLedger(make_paths(tmp_path))
    try:
        analysis = make_analysis()
        en_hash = ledger.input_hash(analysis)
        en_again = ledger.input_hash(analysis)
        zh_hash = ledger.input_hash(analysis, language="zh-CN")
        assert en_hash == en_again
        assert zh_hash != en_hash
    finally:
        ledger.close()


def test_ledger_saves_independent_versions_per_language(tmp_path: Path):
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    try:
        analysis = make_analysis()
        en_doc = run_report(analysis)
        zh_doc = run_report(analysis, language="zh-CN")

        en_version = ledger.save(en_doc, analysis, trigger="initial")
        zh_version = ledger.save(zh_doc, analysis, trigger="initial")

        assert en_version.version_id != zh_version.version_id
        assert en_version.language == "en"
        assert zh_version.language == "zh-CN"
        # Same-language re-save stays idempotent.
        assert ledger.save(en_doc, analysis, trigger="initial") is en_version or (
            ledger.save(en_doc, analysis, trigger="initial").version_id
            == en_version.version_id
        )
        assert {v.language for v in ledger.versions(CASE_ID)} == {"en", "zh-CN"}
    finally:
        ledger.close()


def test_legacy_ledger_without_language_column_migrates_additively(tmp_path: Path):
    """An old PRISM_HOME database (no ``language`` column, English rows)
    keeps working: the column is added in place and legacy rows read back
    as ``en``, never mislabelled as Chinese."""
    paths = make_paths(tmp_path)
    database = paths.data_dir / "index.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database))
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            version_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            as_of TEXT NOT NULL,
            created_at TEXT NOT NULL,
            input_hash TEXT NOT NULL UNIQUE,
            markdown_hash TEXT NOT NULL,
            summary_origin TEXT NOT NULL,
            debate_input_hash TEXT,
            markdown TEXT NOT NULL,
            parent_version_id TEXT,
            trigger_type TEXT NOT NULL
        );
        """
    )
    legacy_markdown = "# Evolution Report: legacy\n\n- As of: 2026-08-01T00:00:00+00:00\n"
    connection.execute(
        f"INSERT INTO {TABLE} (version_id, case_id, as_of, created_at, "
        "input_hash, markdown_hash, summary_origin, debate_input_hash, "
        "markdown, parent_version_id, trigger_type) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "rv_legacy",
            CASE_ID,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
            "legacy-hash",
            "legacy-md-hash",
            "fallback",
            None,
            legacy_markdown,
            None,
            "initial",
        ),
    )
    connection.commit()
    connection.close()

    ledger = ReportVersionLedger(paths)
    try:
        version = ledger.get("rv_legacy")
        assert version is not None
        assert version.language == "en"
        assert version.markdown == legacy_markdown
        # New saves keep working after the additive migration.
        analysis = make_analysis()
        saved = ledger.save(run_report(analysis), analysis, trigger="initial")
        assert saved.language == "en"
        assert len(ledger.versions(CASE_ID)) == 2
    finally:
        ledger.close()


# --- facade and CLI forwarding --------------------------------------------------


class DummyIngestion:
    def ingest(self, path, metadata=None):
        return None


class DummyStore:
    def index_file(self, path):
        return None

    def get(self, source_id):
        return None

    def search(self, criteria, *, limit, offset):
        return ()


class DummyGraph:
    async def timeline(self, case_id, as_of):
        return None

    async def add_case(self, case, **bundle):
        return None


class DummyBus:
    async def publish(self, event):
        return None


class StubAnalyzer:
    async def analyze(self, case_id, as_of=None, *, kinds=None):
        return make_analysis()


def make_api(tmp_path: Path, router=None) -> PrismAPI:
    paths = make_paths(tmp_path)
    return PrismAPI(
        DummyIngestion(),
        DummyStore(),
        DummyGraph(),
        DummyBus(),
        analyzer_service=StubAnalyzer(),
        report_service=ReportService(router, paths=paths),
        report_version_service=ReportVersionLedger(paths),
    )


def test_api_report_case_and_save_forward_language(tmp_path: Path):
    api = make_api(tmp_path)
    doc = asyncio.run(api.report_case(CASE_ID, language="zh-CN"))
    assert doc.language == "zh-CN"
    assert "演变报告" in doc.markdown

    en_doc = asyncio.run(api.report_case(CASE_ID))
    assert en_doc.language == "en"


def test_api_save_report_version_language_versions_are_independent(tmp_path: Path):
    api = make_api(tmp_path)

    zh_version = asyncio.run(
        api.save_report_version(CASE_ID, use_llm=False, trigger="initial", language="zh-CN")
    )
    en_version = asyncio.run(
        api.save_report_version(CASE_ID, use_llm=False, trigger="initial")
    )
    assert zh_version.version_id != en_version.version_id
    assert zh_version.language == "zh-CN"
    assert en_version.language == "en"
    # Same-language replay is the idempotent existing version.
    replay = asyncio.run(
        api.save_report_version(CASE_ID, use_llm=False, trigger="initial", language="zh-CN")
    )
    assert replay.version_id == zh_version.version_id

    rebuilt = asyncio.run(
        api.rebuild_report(CASE_ID, use_llm=False, language="zh-CN")
    )
    assert rebuilt.version_id.startswith("rv_")
    assert rebuilt.language == "zh-CN"


def test_api_save_rejects_non_english_for_languageless_report_service(
    tmp_path: Path,
):
    """An injected report service without a ``language`` capability must fail
    closed for zh-CN instead of silently rendering English."""

    class LegacyReportService:
        async def report(self, analysis, *, debate_result=None):
            return run_report(analysis)

    api = PrismAPI(
        DummyIngestion(),
        DummyStore(),
        DummyGraph(),
        DummyBus(),
        analyzer_service=StubAnalyzer(),
        report_service=LegacyReportService(),
        report_version_service=ReportVersionLedger(make_paths(tmp_path)),
    )
    with pytest.raises(TypeError, match="language"):
        asyncio.run(api.report_case(CASE_ID, language="zh-CN"))


def test_cli_report_and_rebuild_forward_lang(tmp_path: Path):
    class LangAPI:
        def __init__(self):
            self.calls = []

        async def report_case(self, case_id, as_of=None, use_llm=True, *, language="en"):
            self.calls.append(("report_case", case_id, use_llm, language))
            return {"markdown": ""}

        async def save_report_version(self, case_id, as_of=None, use_llm=True, *, language="en"):
            self.calls.append(("save", case_id, use_llm, language))
            return {"version_id": "rv_x"}

        async def rebuild_report(self, case_id, as_of=None, use_llm=True, *, language="en"):
            self.calls.append(("rebuild", case_id, use_llm, language))
            return {"version_id": "rv_y"}

    args = build_parser().parse_args(["report", CASE_ID, "--lang", "zh-CN"])
    assert args.lang == "zh-CN"
    api = LangAPI()
    assert args.handler is handle_report
    asyncio.run(args.handler(args, api))
    assert api.calls == [("report_case", CASE_ID, True, "zh-CN")]

    rebuild_args = build_parser().parse_args(
        ["rebuild-report", CASE_ID, "--no-llm", "--lang", "zh-CN"]
    )
    assert rebuild_args.handler is handle_rebuild_report
    asyncio.run(rebuild_args.handler(rebuild_args, api))
    assert api.calls[-1] == ("rebuild", CASE_ID, False, "zh-CN")

    # Invalid languages are usage errors before any API call (exit status 2).
    import importlib

    cli_main = importlib.import_module("prism.cli.main")

    status = asyncio.run(
        cli_main.main(
            ["report", CASE_ID, "--lang", "zh"],
            api=LangAPI(),
            stdout=StringIO(),
            stderr=StringIO(),
        )
    )
    assert status == 2


def test_cli_report_default_lang_is_english():
    args = build_parser().parse_args(["report", CASE_ID])
    assert args.lang == "en"
    args = build_parser().parse_args(["rebuild-report", CASE_ID])
    assert args.lang == "en"
