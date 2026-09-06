"""WebUI workbench Phase A: the browser-upload staging seam (WB-1).

These tests cover the dependency-free upload/staging controller
(:mod:`prism.webui.upload`): client bytes are validated (suffix whitelist,
non-empty, size cap), spooled into a PRISM-controlled staging directory
under a server-generated unguessable name — never the client filename —
then handed to the one and only write path, ``PrismAPI.add_material``, with
the client filename preserved purely as metadata (WB-1.3).  Validation
failures happen before any facade call and staging files are cleaned up
once the pipeline has taken over.  Everything is offline: synthetic facade,
temporary staging root, no NiceGUI import, no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism.api.facade import ProcessMaterialResult
from prism.pipeline import PipelineError
from prism.pipeline.service import PipelineRun, PipelineStage


UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
STARTED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def staging_root():
    directory = Path(tempfile.mkdtemp(prefix="prism-webui-upload-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _pipeline_run():
    return PipelineRun(
        material_id="mat-1",
        status="completed",
        stages=(
            PipelineStage(
                "index", "skipped", None, detail="already indexed"
            ),
            PipelineStage(
                "extract", "skipped", None, detail="idempotent replay"
            ),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
    )


def _result():
    return ProcessMaterialResult(
        material_id="mat-1",
        pipeline=_pipeline_run(),
        case_id="case-b",
        case_outcome=None,
        warnings=(),
        replayed=False,
        report_version=None,
        debate_link=None,
    )


class FakeUploadFacade:
    """Synthetic PrismAPI stand-in recording every append it receives."""

    def __init__(self, result=None, error=None, cases=()):
        self._result = result if result is not None else _result()
        self._error = error
        self._cases = tuple(cases)
        self.calls = []
        self.overviews_calls = 0

    async def add_material(self, source, target_case, metadata=None, *,
                           as_of=None, use_llm=True,
                           parent_debate_run_id=None):
        self.calls.append(
            dict(source=source, target_case=target_case, metadata=metadata,
                 as_of=as_of, use_llm=use_llm,
                 parent_debate_run_id=parent_debate_run_id)
        )
        if self._error is not None:
            raise self._error
        return self._result

    async def case_overviews(self, **filters):
        self.overviews_calls += 1
        return self._cases


def _overview(case_id, name):
    return SimpleNamespace(case_id=case_id, name=name, case_type="policy")


def _staging(root, **kwargs):
    from prism.webui.upload import UploadStagingService

    # The staging root must be a PROPER subdirectory of its controlled
    # root, so the behaviour tests declare the temp directory's parent as
    # the controlled root; the control itself is covered by dedicated
    # tests below.
    return UploadStagingService(
        root, controlled_root=root.parent, **kwargs
    )


def _controller(root, facade=None, **kwargs):
    from prism.webui.upload import UploadController

    return UploadController(
        facade if facade is not None else FakeUploadFacade(),
        _staging(root, **kwargs),
    )


# ------------------------------------------------------- staging validations


def test_stage_writes_bytes_under_a_server_generated_name(staging_root):
    staging = _staging(staging_root)
    payload = "# Note\n\nThe agency revised the policy.".encode("utf-8")

    staged = staging.stage("policy note.md", payload)

    assert staged.path.parent.parent == staging_root.resolve()
    assert staged.path.name == "source.md"
    assert staged.path.read_bytes() == payload
    assert staged.original_name == "policy note.md"
    assert staged.suffix == ".md"
    assert staged.size_bytes == len(payload)
    assert staged.sha256 == hashlib.sha256(payload).hexdigest()
    assert staged.upload_id != "policy note.md"
    # No stray temporary files remain next to the finalized upload.
    assert [item.name for item in staged.path.parent.iterdir()] == ["source.md"]


def test_stage_accepts_the_full_suffix_whitelist(staging_root):
    staging = _staging(staging_root)

    for name in ("a.md", "b.markdown", "c.pdf", "D.MARKDOWN", "e.PDF"):
        staged = staging.stage(name, b"x")
        assert staged.path.is_file()


def test_stage_rejects_forbidden_suffixes_and_empty_payload(staging_root):
    staging = _staging(staging_root)

    with pytest.raises(ValueError, match="\\.md|markdown|pdf"):
        staging.stage("note.txt", b"plain text")
    with pytest.raises(ValueError, match="\\.md|markdown|pdf"):
        staging.stage("archive.pdf.exe", b"binary")
    with pytest.raises(ValueError, match="unsupported"):
        staging.stage("no-extension", b"content")
    with pytest.raises(ValueError, match="empty"):
        staging.stage("note.md", b"")
    with pytest.raises(ValueError, match="empty"):
        staging.stage("note.md", b"   ")
    assert list(staging_root.rglob("source.*")) == []


def test_stage_rejects_non_bytes_payloads_and_bad_names(staging_root):
    staging = _staging(staging_root)

    with pytest.raises(TypeError):
        staging.stage("note.md", "not bytes")
    with pytest.raises(TypeError):
        staging.stage("note.md", 1234)
    with pytest.raises(TypeError):
        staging.stage(42, b"bytes")
    with pytest.raises(ValueError):
        staging.stage("  ", b"bytes")


def test_stage_enforces_the_configurable_size_cap(staging_root):
    staging = _staging(staging_root, max_upload_bytes=8)

    staged = staging.stage("small.md", b"12345678")
    assert staged.size_bytes == 8

    with pytest.raises(ValueError, match="8"):
        staging.stage("large.md", b"123456789")


def test_stage_never_lets_the_client_name_reach_the_filesystem(staging_root):
    staging = _staging(staging_root)

    traversal = staging.stage("../../../etc/evil.md", b"body")
    windows = staging.stage("..\\..\\escaped.md", b"body")

    for staged, expected_name in ((traversal, "evil.md"), (windows, "escaped.md")):
        assert staged.original_name == expected_name
        assert staged.path.parent.parent == staging_root.resolve()
        assert staged.path.is_file()
    # Nothing escaped the staging root.
    assert not (staging_root.parent / "etc").exists()


def test_stage_accepts_unicode_filenames_as_metadata_only(staging_root):
    staging = _staging(staging_root)

    staged = staging.stage("政策说明.md", "正文".encode("utf-8"))

    assert staged.original_name == "政策说明.md"
    assert staged.path.name == "source.md"
    assert staged.path.read_bytes() == "正文".encode("utf-8")


def test_staging_root_may_be_supplied_lazily_by_a_provider(staging_root):
    from prism.webui.upload import UploadStagingService

    holder = {"root": staging_root}
    staging = UploadStagingService(
        lambda: holder["root"], controlled_root=lambda: staging_root.parent
    )

    staged = staging.stage("note.md", b"body")

    assert staged.path.parent.parent == staging_root.resolve()


def test_staging_service_validates_its_configuration(staging_root):
    from prism.webui.upload import UploadStagingService

    with pytest.raises(TypeError):
        UploadStagingService(42)
    with pytest.raises(ValueError):
        UploadStagingService(staging_root, max_upload_bytes=0)
    with pytest.raises(TypeError):
        UploadStagingService(staging_root, max_upload_bytes="big")


def test_discard_removes_the_staged_upload(staging_root):
    staging = _staging(staging_root)
    staged = staging.stage("note.md", b"body")
    upload_dir = staged.path.parent

    staging.discard(staged)

    assert not staged.path.exists()
    assert not upload_dir.exists()
    staging.discard(staged)  # idempotent


# ------------------------------------------- controlled staging root (review)

# The staging area must live in PRISM_HOME (or an explicitly declared
# controlled root) with fixed directory names, and only files this service
# generated inside it may ever be deleted.


def test_stage_rejects_a_staging_root_outside_the_controlled_root(
    staging_root,
):
    from prism.webui.upload import UploadStagingService

    staging = UploadStagingService(
        staging_root / "staging",
        controlled_root=staging_root / "elsewhere",
    )

    with pytest.raises(ValueError, match="controlled root"):
        staging.stage("note.md", b"body")

    # The refusal happened before anything was created on disk.
    assert not (staging_root / "staging").exists()
    assert list(staging_root.rglob("source.*")) == []


def test_stage_rejects_the_controlled_root_itself_as_the_staging_root(
    staging_root,
):
    from prism.webui.upload import UploadStagingService

    staging = UploadStagingService(
        staging_root, controlled_root=staging_root
    )

    with pytest.raises(ValueError, match="controlled root"):
        staging.stage("note.md", b"body")


def test_the_controlled_root_defaults_to_prism_home(
    staging_root, monkeypatch
):
    from prism.webui.upload import UploadStagingService

    home = staging_root / "home"
    monkeypatch.setenv("PRISM_HOME", str(home))

    inside = UploadStagingService(home / "staging" / "uploads")
    staged = inside.stage("note.md", b"body")
    assert staged.path.is_file()

    outside = UploadStagingService(staging_root / "outside")
    with pytest.raises(ValueError, match="controlled root"):
        outside.stage("note.md", b"body")
    assert not (staging_root / "outside").exists()


def test_the_controlled_root_may_be_supplied_lazily(staging_root):
    from prism.webui.upload import UploadStagingService

    holder = {"root": staging_root}
    staging = UploadStagingService(
        staging_root / "uploads",
        controlled_root=lambda: holder["root"],
    )

    staged = staging.stage("note.md", b"body")

    assert staged.path.parent.parent == (staging_root / "uploads").resolve()


def _forged_upload(path, upload_id="f" * 32):
    from prism.webui.upload import StagedUpload

    return StagedUpload(
        upload_id=upload_id,
        path=path,
        original_name="note.md",
        suffix=path.suffix.lower(),
        size_bytes=4,
        sha256=hashlib.sha256(b"body").hexdigest(),
    )


def test_discard_refuses_files_this_service_did_not_stage(staging_root):
    staging = _staging(staging_root)
    victim = staging_root / "manual" / "source.md"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"keep me")

    with pytest.raises(ValueError, match="did not stage"):
        staging.discard(_forged_upload(victim))

    assert victim.read_bytes() == b"keep me"
    assert victim.parent.is_dir()


def test_discard_refuses_a_tampered_path_for_an_issued_upload(
    staging_root,
):
    staging = _staging(staging_root)
    staged = staging.stage("note.md", b"body")
    decoy = staging_root / "evil.md"
    decoy.write_bytes(b"keep")

    with pytest.raises(ValueError, match="did not stage"):
        staging.discard(_forged_upload(decoy, upload_id=staged.upload_id))

    assert decoy.read_bytes() == b"keep"
    assert staged.path.is_file()  # the real upload is untouched


def test_discard_rejects_non_staged_upload_values(staging_root):
    staging = _staging(staging_root)

    with pytest.raises(TypeError):
        staging.discard("source.md")


# -------------------------------------------- submit provenance gate (review)

# submit() must verify — before any facade call — that the StagedUpload it
# received was really issued by THIS staging service, that its path still
# lives inside the validated staging root, and that the bytes on disk still
# match the recorded size/digest: a hand-crafted or tampered object may
# never reach ingestion.


def test_submit_refuses_a_forged_staged_upload(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)
    # A file that genuinely exists inside the staging root, but was never
    # staged by this service (no issuance record).
    victim = staging_root / "manual" / "source.md"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"keep me")

    with pytest.raises(ValueError, match="did not stage"):
        run(controller.submit(_forged_upload(victim), "case-b"))

    assert facade.calls == []
    assert victim.read_bytes() == b"keep me"


def test_submit_refuses_tampered_staged_content(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)
    staged = controller.stage("note.md", b"body")
    staged.path.write_bytes(b"evil")  # same size, different bytes

    with pytest.raises(ValueError, match="content"):
        run(controller.submit(staged, "case-b"))

    assert facade.calls == []


def test_submit_refuses_tampered_staged_size(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)
    staged = controller.stage("note.md", b"body")
    staged.path.write_bytes(b"bo")  # shorter than the recorded size

    with pytest.raises(ValueError, match="size"):
        run(controller.submit(staged, "case-b"))

    assert facade.calls == []


def test_submit_refuses_forged_metadata_for_an_issued_upload(staging_root):
    from prism.webui.upload import StagedUpload

    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)
    staged = controller.stage("policy note.md", b"body")
    forged = StagedUpload(
        upload_id=staged.upload_id,
        path=staged.path,
        original_name="innocent.md",  # re-labels the audited upload
        suffix=staged.suffix,
        size_bytes=staged.size_bytes,
        sha256=staged.sha256,
    )

    with pytest.raises(ValueError, match="did not stage"):
        run(controller.submit(forged, "case-b"))

    assert facade.calls == []


def test_submit_refuses_uploads_outside_the_current_staging_root(
    staging_root,
):
    from prism.webui.upload import UploadController, UploadStagingService

    holder = {"root": staging_root}
    staging = UploadStagingService(
        lambda: holder["root"], controlled_root=lambda: staging_root.parent
    )
    controller = UploadController(FakeUploadFacade(), staging)
    staged = controller.stage("note.md", b"body")

    # The staging root resolves elsewhere now: the issued path no longer
    # sits inside the validated root, so the upload is refused.
    elsewhere = Path(tempfile.mkdtemp(prefix="prism-webui-moved-"))
    try:
        holder["root"] = elsewhere / "uploads"
        (elsewhere / "uploads").mkdir()

        with pytest.raises(ValueError, match="staging root"):
            run(controller.submit(staged, "case-b"))
    finally:
        shutil.rmtree(elsewhere, ignore_errors=True)


# ---------------------------------------------------------- controller seam


def test_controller_rejects_a_facade_without_add_material(staging_root):
    from prism.webui.upload import UploadController

    class Empty:
        pass

    with pytest.raises(TypeError, match="add_material"):
        UploadController(Empty(), _staging(staging_root))


def test_upload_stages_then_delegates_to_add_material(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)

    view = run(controller.upload(
        "policy note.md", "policy note body".encode("utf-8"), "case-b",
        as_of="2026-09-01T00:00:00+00:00",
        parent_debate_run_id="run-1",
    ))

    (call,) = facade.calls
    staged_path = Path(call["source"])
    assert staged_path.parent.parent == staging_root.resolve()
    assert staged_path.suffix == ".md"
    assert staged_path.is_file() is False  # cleaned after the append
    assert call["target_case"] == "case-b"
    assert call["use_llm"] is False
    assert call["as_of"] == AS_OF
    assert call["parent_debate_run_id"] == "run-1"
    metadata = call["metadata"]
    assert metadata["upload_original_name"] == "policy note.md"
    assert metadata["upload_sha256"] == hashlib.sha256(
        "policy note body".encode("utf-8")
    ).hexdigest()
    assert metadata["upload_size_bytes"] == len("policy note body")
    assert metadata["upload_origin"] == "prism-webui"

    assert view["material_id"] == "mat-1"
    assert view["upload"]["original_name"] == "policy note.md"
    assert view["upload"]["upload_id"]
    assert list(staging_root.iterdir()) == []


def test_upload_validates_the_target_case_before_any_write(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)

    with pytest.raises(ValueError, match="target_case"):
        run(controller.upload("note.md", b"body", "  "))

    assert facade.calls == []


def test_upload_option_validation_mirrors_the_path_intake(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)

    with pytest.raises(ValueError, match="as_of"):
        run(controller.upload("note.md", b"body", "case-b",
                              as_of=datetime(2026, 9, 1)))
    with pytest.raises(TypeError, match="use_llm"):
        run(controller.upload("note.md", b"body", "case-b", use_llm="no"))
    with pytest.raises(ValueError, match="parent_debate_run_id"):
        run(controller.upload("note.md", b"body", "case-b",
                              parent_debate_run_id=" "))
    assert facade.calls == []


def test_submit_keeps_the_staged_file_when_validation_fails(staging_root):
    controller = _controller(staging_root)
    staged = controller.stage("note.md", b"body")

    with pytest.raises(ValueError, match="target_case"):
        run(controller.submit(staged, "  "))

    assert staged.path.is_file()


def test_submit_rejects_stale_staged_uploads(staging_root):
    controller = _controller(staging_root)
    staged = controller.stage("note.md", b"body")
    staged.path.unlink()

    with pytest.raises(FileNotFoundError):
        run(controller.submit(staged, "case-b"))


def test_upload_propagates_pipeline_failures_and_cleans_staging(staging_root):
    error = PipelineError("graph stage failed", stage="graph", material_id="m")
    facade = FakeUploadFacade(error=error)
    controller = _controller(staging_root, facade=facade)

    with pytest.raises(PipelineError):
        run(controller.upload("note.md", b"body", "case-b"))

    assert len(facade.calls) == 1
    assert list(staging_root.iterdir()) == []


def test_submit_takes_an_already_staged_upload(staging_root):
    facade = FakeUploadFacade()
    controller = _controller(staging_root, facade=facade)
    staged = controller.stage("note.md", b"body")

    view = run(controller.submit(staged, "case-b"))

    (call,) = facade.calls
    assert Path(call["source"]) == staged.path
    assert call["target_case"] == "case-b"
    assert view["material_id"] == "mat-1"
    assert view["upload"]["original_name"] == "note.md"


def test_controller_stage_delegates_to_the_staging_service(staging_root):
    controller = _controller(staging_root)

    staged = controller.stage("note.md", b"body")

    assert staged.path.is_file()
    assert staged.size_bytes == 4


def test_load_case_options_projects_the_recorded_cases(staging_root):
    facade = FakeUploadFacade(cases=(
        _overview("case-b", "Rate policy"),
        _overview("case-a", "Housing"),
    ))
    controller = _controller(staging_root, facade=facade)

    options = run(controller.load_case_options())

    assert options == {
        "case-b": "case-b — Rate policy",
        "case-a": "case-a — Housing",
    }


def test_load_case_options_requires_the_overviews_facade(staging_root):
    class NoOverviews:
        async def add_material(self, *args, **kwargs):
            raise AssertionError("not called")

    from prism.webui.upload import UploadController

    controller = UploadController(NoOverviews(), _staging(staging_root))

    with pytest.raises(ValueError, match="case_overviews"):
        run(controller.load_case_options())


# ------------------------------------------------------ import / page seam


def test_importing_the_module_never_imports_nicegui():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "nicegui" or name.startswith("nicegui.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        from prism.webui import upload

        assert callable(upload.UploadController)
        assert callable(upload.UploadStagingService)
        assert not any(
            name == "nicegui" or name.startswith("nicegui.")
            for name in sys.modules
        )
    finally:
        sys.modules.update(saved)


def test_the_upload_event_adapter_reads_name_and_bytes(staging_root):
    from prism.webui.upload import read_upload_event

    event = SimpleNamespace(
        name="note.md", content=io.BytesIO("body".encode("utf-8"))
    )

    assert read_upload_event(event) == ("note.md", b"body")

    with pytest.raises(ValueError, match="upload event"):
        read_upload_event(SimpleNamespace(name="", content=io.BytesIO(b"x")))
    with pytest.raises(TypeError, match="upload event"):
        read_upload_event(object())


# ------------------------------------------- bounded upload-event reads

# The size cap must bind while the bytes are being read, not only after a
# full content.read() has already materialized the whole upload in memory.


class _HugeStream:
    """Readable stream whose unbounded ``read()`` is the memory bomb."""

    def __init__(self, size: int):
        self.size = size
        self.requested_sizes: list[object] = []
        self.returned = 0

    def read(self, size: object = -1) -> bytes:
        self.requested_sizes.append(size)
        if size is None or not isinstance(size, int) or size < 0:
            count = self.size  # the unbounded read
        else:
            count = min(size, self.size)
        self.returned += count
        return b"\0" * count


def test_read_upload_event_is_bounded_by_max_bytes():
    from prism.webui.upload import _READ_BLOCK_BYTES, read_upload_event

    stream = _HugeStream(10 * 1024 * 1024)
    event = SimpleNamespace(name="big.md", content=stream)

    with pytest.raises(ValueError, match="1000 byte limit"):
        read_upload_event(event, max_bytes=1000)

    # The reader never issued an unbounded read and stopped within one
    # block of the cap instead of materializing the full payload.
    assert all(
        isinstance(size, int) and size > 0 for size in stream.requested_sizes
    )
    assert stream.returned <= 1000 + _READ_BLOCK_BYTES


def test_read_upload_event_validates_max_bytes():
    from prism.webui.upload import read_upload_event

    event = SimpleNamespace(name="note.md", content=io.BytesIO(b"body"))

    with pytest.raises(TypeError, match="max_bytes"):
        read_upload_event(event, max_bytes="big")
    with pytest.raises(TypeError, match="max_bytes"):
        read_upload_event(event, max_bytes=True)
    with pytest.raises(ValueError, match="max_bytes"):
        read_upload_event(event, max_bytes=0)

    assert read_upload_event(event, max_bytes=4) == ("note.md", b"body")


def test_read_upload_event_rejects_non_byte_content():
    from prism.webui.upload import read_upload_event

    class TextStream:
        def read(self, size: object = -1) -> str:
            return "text"

    event = SimpleNamespace(name="note.md", content=TextStream())

    with pytest.raises(TypeError, match="bytes"):
        read_upload_event(event)


def test_controller_read_event_enforces_the_staging_cap(staging_root):
    controller = _controller(staging_root, max_upload_bytes=8)

    ok = SimpleNamespace(name="note.md", content=io.BytesIO(b"12345678"))
    assert controller.read_event(ok) == ("note.md", b"12345678")

    oversized = SimpleNamespace(
        name="big.md", content=io.BytesIO(b"123456789")
    )
    with pytest.raises(ValueError, match="8"):
        controller.read_event(oversized)
