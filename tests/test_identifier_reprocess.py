"""Offline tests for the timestamped scholarly reprocess runner."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from prism.sources import HttpResponse, run_identifier_reprocess

UTC = timezone.utc
START = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)
PMID = "40212345"
PMCID = "PMC8880123"
EUROPEPMC_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    f"?query=EXT_ID%3A{PMID}%20AND%20SRC%3AMED&format=json&resultType=core"
)


class NoCallGetter:
    async def get(self, url: str, *, timeout: float):
        raise AssertionError("dry-run must not perform HTTP")


class FakeGetter:
    def __init__(self, response: HttpResponse | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, float]] = []

    async def get(self, url: str, *, timeout: float):
        self.calls.append((url, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def sequence_clock(*values: datetime):
    pending = iter(values)
    return lambda: next(pending)


def test_dry_run_writes_isolated_manifest_and_summary_derived_from_rows(tmp_path):
    candidates = [
        {"id": "a", "title": "Abstract", "access_level": "abstract_only"},
        {"id": "m", "title": "Metadata", "access_level": "metadata_only"},
        {"id": "f", "title": "Full text", "access_level": "fulltext"},
        {"id": "b", "title": "Blocked", "access_level": "blocked"},
        {"id": "u", "title": "Needs lookup"},
    ]
    source = tmp_path / "candidates.json"
    source.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
    run = asyncio.run(
        run_identifier_reprocess(
            source,
            tmp_path / "runs",
            dry_run=True,
            getter=NoCallGetter(),
            clock=sequence_clock(START, START + timedelta(seconds=1)),
        )
    )

    assert run.output_dir.name == "20260902T050000.000000Z"
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(run.summary_path.read_text(encoding="utf-8"))
    assert manifest["records"] and manifest["total"] == len(manifest["records"])
    derived = {
        name: sum(row["classification"] == name for row in manifest["records"])
        for name in summary["counts"]
    }
    assert summary["counts"] == derived
    assert summary["counts"] == {
        "abstract_only": 1,
        "metadata_only": 1,
        "fulltext": 1,
        "blocked": 1,
        "unresolved": 1,
    }
    assert not (tmp_path / "manifest.json").exists()

    with pytest.raises(FileExistsError):
        asyncio.run(
            run_identifier_reprocess(
                source,
                tmp_path / "runs",
                clock=sequence_clock(START),
            )
        )
    assert json.loads(run.manifest_path.read_text(encoding="utf-8")) == manifest


def test_execute_resolves_no_doi_pubmed_record_and_redacts_audit_url(tmp_path):
    source = tmp_path / "candidates.json"
    secret = "runner-test-secret"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "pubmed-1",
                    "url": (
                        f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/"
                        f"?token={secret}#api_key={secret}"
                    ),
                    "title": "Public evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    payload = {
        "resultList": {
            "result": [
                {
                    "pmid": PMID,
                    "pmcid": PMCID,
                    "title": "Public evidence",
                    "authorString": "Lovelace A.",
                    "pubYear": "2024",
                    "abstractText": "A public abstract.",
                }
            ]
        }
    }
    getter = FakeGetter(
        HttpResponse(EUROPEPMC_URL, 200, json.dumps(payload), "application/json")
    )
    run = asyncio.run(
        run_identifier_reprocess(
            source,
            tmp_path / "runs",
            dry_run=False,
            getter=getter,
            clock=sequence_clock(START, START, START + timedelta(seconds=1)),
        )
    )
    manifest_text = run.manifest_path.read_text(encoding="utf-8")
    assert secret not in manifest_text
    record = json.loads(manifest_text)["records"][0]
    assert record["classification"] == "abstract_only"
    assert record["doi"] is None
    assert record["pmid"] == PMID
    assert record["pmcid"] == PMCID
    assert "token=<redacted>" in record["input_link"]
    assert "api_key=<redacted>" in record["input_link"]
    assert getter.calls == [(EUROPEPMC_URL, 10.0)]


def test_execute_requires_explicit_getter_and_redacts_transport_failures(tmp_path):
    source = tmp_path / "api_key=filename-secret.json"
    source.write_text(json.dumps([{"id": "x", "pmid": PMID}]), encoding="utf-8")
    with pytest.raises(ValueError, match="explicitly injected HttpGetter"):
        asyncio.run(
            run_identifier_reprocess(
                source, tmp_path / "runs-no-getter", dry_run=False
            )
        )

    getter = FakeGetter(RuntimeError("token=transport-secret"))
    run = asyncio.run(
        run_identifier_reprocess(
            source,
            tmp_path / "runs",
            dry_run=False,
            getter=getter,
            clock=sequence_clock(START, START, START + timedelta(seconds=1)),
        )
    )
    manifest_text = run.manifest_path.read_text(encoding="utf-8")
    assert "filename-secret" not in manifest_text
    assert "transport-secret" not in manifest_text
    record = json.loads(manifest_text)["records"][0]
    assert record["classification"] == "unresolved"
    assert record["detail"] == "transport: metadata transport failed (RuntimeError); source=" + EUROPEPMC_URL
