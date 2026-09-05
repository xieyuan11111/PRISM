"""M3 WebUI slice: the evidence-library browser over the existing facade.

This module covers the dependency-free controller/view-model seam
(:class:`prism.webui.evidence.EvidenceBrowserController`) — paginated
``PrismAPI.search`` calls with query/case/source/type/time filters, the
``search_evidence`` compatibility name, explicit validation of every filter
before any facade call — plus the JSON-safe hit view contract (source id,
corpus path, snippet, URL and time retained verbatim) and the lazy-NiceGUI
page-builder seam exercised through a recording ``ui`` stand-in.

Everything is offline: a synthetic facade over real ``SearchHit`` objects, no
NiceGUI import, no SQLite, no corpus reads, no LLM, no network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys
from types import SimpleNamespace

import pytest

from prism.store import SearchHit


UTC = timezone.utc
PUBLISHED = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
CORPUS_PATH = "corpus/2026-02/example.gov/material-a.md"


def run(coro):
    return asyncio.run(coro)


def _hit(source_id="material-a", *, title="Material A", source="example.gov",
         hit_type="policy", published_at=PUBLISHED, path=CORPUS_PATH,
         snippet="The agency published the revised policy.", url=None,
         case_tags=("case-rates",)):
    return SearchHit(
        source_id=source_id,
        title=title,
        source=source,
        path=path,
        snippet=snippet,
        type=hit_type,
        published_at=published_at,
        case_tags=case_tags,
        url=url,
    )


class FakeSearchFacade:
    """Synthetic PrismAPI stand-in recording every search it receives."""

    def __init__(self, hits=()):
        self._hits = tuple(hits)
        self.calls = []

    async def search(self, query=None, *, case_tag=None, source=None,
                     type=None, published_after=None, published_before=None,
                     limit=50, offset=0):
        self.calls.append(
            dict(query=query, case_tag=case_tag, source=source, type=type,
                 published_after=published_after,
                 published_before=published_before,
                 limit=limit, offset=offset)
        )
        return self._hits[offset:offset + limit]


class LegacySearchFacade(FakeSearchFacade):
    """A facade exposing only the legacy ``search_evidence`` name."""

    def __init__(self, hits=()):
        super().__init__(hits)
        del self.calls[:]
        self.calls = []

    async def search_evidence(self, query=None, **filters):
        self.calls.append(dict(query=query, **filters))
        return self._hits[filters.get("offset", 0):
                          filters.get("offset", 0) + filters.get("limit", 50)]


def _controller(hits=()):
    from prism.webui.evidence import EvidenceBrowserController

    return EvidenceBrowserController(FakeSearchFacade(hits))


# ------------------------------------------------------------- controller seam


def test_controller_rejects_a_facade_without_any_search_operation():
    from prism.webui.evidence import EvidenceBrowserController

    class Empty:
        pass

    with pytest.raises(TypeError, match="search"):
        EvidenceBrowserController(Empty())
    with pytest.raises(TypeError, match="search_evidence"):
        EvidenceBrowserController(SimpleNamespace())


def test_controller_accepts_the_legacy_search_evidence_name():
    from prism.webui.evidence import EvidenceBrowserController

    facade = LegacySearchFacade([_hit()])
    controller = EvidenceBrowserController(facade)

    view = run(controller.browse("housing"))

    assert facade.calls == [
        dict(query="housing", case_tag=None, source=None, type=None,
             published_after=None, published_before=None,
             limit=controller.page_size + 1, offset=0)
    ]
    assert view["count"] == 1


def test_browse_forwards_every_filter_and_the_pagination_window():
    controller = _controller([_hit()])
    facade = controller._api
    after = datetime(2026, 1, 1, tzinfo=UTC)
    before = datetime(2026, 3, 1, tzinfo=UTC)

    run(controller.browse(
        "housing", case="case-rates", source="example.gov", type="policy",
        published_after=after, published_before=before, page=3, page_size=10,
    ))

    assert facade.calls == [
        dict(query="housing", case_tag="case-rates", source="example.gov",
             type="policy", published_after=after, published_before=before,
             limit=11, offset=20)
    ]


def test_browse_without_filters_passes_neutral_arguments():
    controller = _controller()

    run(controller.browse())

    assert controller._api.calls == [
        dict(query=None, case_tag=None, source=None, type=None,
             published_after=None, published_before=None,
             limit=controller.page_size + 1, offset=0)
    ]


def test_browse_parses_iso_time_bounds_into_aware_datetimes():
    controller = _controller([_hit()])

    run(controller.browse(
        published_after="2026-01-01T00:00:00+00:00",
        published_before="2026-03-01T00:00:00+05:00",
    ))

    call = controller._api.calls[0]
    assert call["published_after"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert call["published_before"].utcoffset() == timedelta(hours=5)


def test_hit_view_is_json_safe_and_retains_the_locator_fields():
    controller = _controller([
        _hit(url="https://example.gov/material-a", case_tags=("case-rates",))
    ])

    view = run(controller.browse())
    (row,) = view["results"]

    assert row["source_id"] == "material-a"
    assert row["title"] == "Material A"
    assert row["source"] == "example.gov"
    assert row["type"] == "policy"
    assert row["corpus_path"] == CORPUS_PATH
    assert row["raw_path"] is None
    assert row["url"] == "https://example.gov/material-a"
    assert row["published_at"] == PUBLISHED.isoformat()
    assert row["case_tags"] == ["case-rates"]
    assert row["snippet"] == "The agency published the revised policy."
    assert json.loads(json.dumps(view)) == view


def test_hit_view_keeps_paragraph_page_and_quote_when_the_facade_has_them():
    from prism.webui.evidence import EvidenceBrowserController

    class LocatorHit:
        source_id = "material-a"
        title = "Material A"
        source = "example.gov"
        type = "policy"
        published_at = PUBLISHED
        path = CORPUS_PATH
        snippet = "The agency published the revised policy."
        case_tags = ()
        paragraph = 2
        page = 3
        quote = "The policy was published."

    class Facade:
        async def search(self, query=None, **filters):
            return (LocatorHit(),)

    controller = EvidenceBrowserController(Facade())

    view = run(controller.browse())
    (row,) = view["results"]

    assert row["paragraph"] == 2
    assert row["page"] == 3
    assert row["quote"] == "The policy was published."


def test_pagination_reports_has_more_and_has_previous_honestly():
    hits = tuple(_hit(f"material-{index}") for index in range(26))
    controller = _controller(hits)

    first = run(controller.browse(page=1, page_size=25))
    assert first["count"] == 25
    assert first["has_more"] is True
    assert first["has_previous"] is False
    assert [row["source_id"] for row in first["results"]] == [
        f"material-{index}" for index in range(25)
    ]

    second = run(controller.browse(page=2, page_size=25))
    assert second["count"] == 1
    assert second["has_more"] is False
    assert second["has_previous"] is True
    assert [row["source_id"] for row in second["results"]] == ["material-25"]

    # The view reports the requested window; no invented totals.
    assert set(first) >= {
        "query", "filters", "page", "page_size", "offset", "count",
        "has_more", "has_previous", "results",
    }
    assert first["filters"] == {
        "case": None, "source": None, "type": None,
        "published_after": None, "published_before": None,
    }


def test_browse_reports_the_page_window_and_trims_the_probe_row():
    controller = _controller([_hit()])
    view = run(controller.browse(page=2, page_size=5))

    assert view["page"] == 2
    assert view["page_size"] == 5
    assert view["offset"] == 5
    assert view["count"] == 0


def test_browse_rejects_naive_or_unparseable_time_bounds_before_any_call():
    controller = _controller([_hit()])

    with pytest.raises(ValueError, match="published_after"):
        run(controller.browse(published_after=datetime(2026, 1, 1)))
    with pytest.raises(ValueError, match="published_after"):
        run(controller.browse(published_after="2026-01-01"))
    with pytest.raises(ValueError, match="published_before"):
        run(controller.browse(published_before="yesterday"))
    with pytest.raises(TypeError, match="published_after"):
        run(controller.browse(published_after=20260101))
    assert controller._api.calls == []


def test_browse_rejects_an_inverted_time_window_before_any_call():
    controller = _controller([_hit()])

    with pytest.raises(ValueError, match="published_after"):
        run(controller.browse(
            published_after=datetime(2026, 3, 1, tzinfo=UTC),
            published_before=datetime(2026, 1, 1, tzinfo=UTC),
        ))
    assert controller._api.calls == []


def test_browse_rejects_invalid_pagination_and_filter_types_before_any_call():
    controller = _controller([_hit()])

    for kwargs in (
        {"page": 0}, {"page": -1}, {"page": True}, {"page": "1"},
        {"page_size": 0}, {"page_size": 10_000}, {"page_size": 2.5},
        {"query": 42}, {"case": []}, {"source": 1}, {"type": object()},
    ):
        with pytest.raises((ValueError, TypeError)):
            run(controller.browse(**kwargs))
    assert controller._api.calls == []


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
        from prism.webui import evidence

        assert callable(evidence.EvidenceBrowserController)
        assert callable(evidence.build_evidence_page)
        assert not any(
            name == "nicegui" or name.startswith("nicegui.")
            for name in sys.modules
        )
    finally:
        sys.modules.update(saved)


class _FakeElement:
    def __init__(self, ui, name, *args, **kwargs):
        self._ui = ui
        self.name = name
        self.args = args
        self.kwargs = kwargs
        self.value = kwargs.get("value")
        self.rows = list(kwargs.get("rows") or ())
        self.text = args[0] if args and isinstance(args[0], str) else ""
        self.content = args[0] if args and isinstance(args[0], str) else ""
        self.children = []

    def __enter__(self):
        self._ui._stack.append(self)
        return self

    def __exit__(self, *exc_info):
        self._ui._stack.pop()
        return False

    def update(self):
        self._ui.updates.append(self.name)

    def classes(self, *args, **kwargs):
        return self

    def on(self, event, handler):
        self.events = getattr(self, "events", {})
        self.events[event] = handler
        return self


class _FakeUI:
    """Recording stand-in for the NiceGUI ``ui`` module surface."""

    def __init__(self):
        self.elements = []
        self.pages = {}
        self.updates = []
        self._stack = []

    def __getattr__(self, name):
        def factory(*args, **kwargs):
            element = _FakeElement(self, name, *args, **kwargs)
            if self._stack:
                self._stack[-1].children.append(element)
            self.elements.append(element)
            return element

        return factory

    def page(self, route):
        def register(fn):
            self.pages[route] = fn
            return fn

        return register


def _element(ui, name, *, label=None, text=None):
    for element in ui.elements:
        if element.name != name:
            continue
        element_label = str(element.kwargs.get("label", ""))
        if label is not None and label.lower() not in element_label.lower():
            continue
        element_text = element.args[0] if element.args else ""
        if text is not None and text.lower() not in str(element_text).lower():
            continue
        return element
    raise AssertionError(f"no {name} element matching label={label!r} text={text!r}")


def _labels(ui):
    return [element for element in ui.elements if element.name == "label"]


def _build_page(controller):
    from prism.webui.evidence import build_evidence_page

    ui = _FakeUI()
    build_evidence_page(controller, ui)
    page = ui.pages["/evidence"]
    page()
    return ui


def test_page_seam_lists_filter_controls_and_the_results_table():
    ui = _build_page(_controller([_hit()]))

    for label in ("Query", "Case", "Source", "Type",
                  "Published after", "Published before"):
        assert _element(ui, "input", label=label) is not None
    _element(ui, "button", text="Search")
    _element(ui, "button", text="Previous")
    _element(ui, "button", text="Next")
    table = _element(ui, "table")
    assert table.rows == []
    assert {column["field"] for column in table.kwargs["columns"]} >= {
        "source_id", "title", "type", "source", "published_at"
    }


def test_page_seam_search_fills_the_table_through_the_controller():
    hits = tuple(_hit(f"material-{index}") for index in range(3))
    controller = _controller(hits)
    ui = _build_page(controller)

    _element(ui, "input", label="Query").value = "housing"
    _element(ui, "input", label="Case").value = "case-rates"
    run(_element(ui, "button", text="Search").kwargs["on_click"](None))

    table = _element(ui, "table")
    assert [row["source_id"] for row in table.rows] == [
        "material-0", "material-1", "material-2"
    ]
    assert table.rows[0]["corpus_path"] == CORPUS_PATH
    assert any("3 result" in label.text for label in _labels(ui))
    call = controller._api.calls[0]
    assert call["query"] == "housing"
    assert call["case_tag"] == "case-rates"


def test_page_seam_next_and_previous_move_the_pagination_window():
    hits = tuple(_hit(f"material-{index}") for index in range(26))
    controller = _controller(hits)
    ui = _build_page(controller)

    run(_element(ui, "button", text="Search").kwargs["on_click"](None))
    assert controller._api.calls[-1]["limit"] == 26
    assert controller._api.calls[-1]["offset"] == 0

    run(_element(ui, "button", text="Next").kwargs["on_click"](None))
    assert controller._api.calls[-1]["offset"] == 25
    assert any("page 2" in label.text for label in _labels(ui))

    run(_element(ui, "button", text="Previous").kwargs["on_click"](None))
    assert controller._api.calls[-1]["offset"] == 0


def test_page_seam_previous_is_refused_on_the_first_page():
    controller = _controller([_hit()])
    ui = _build_page(controller)

    run(_element(ui, "button", text="Search").kwargs["on_click"](None))
    del controller._api.calls[:]
    run(_element(ui, "button", text="Previous").kwargs["on_click"](None))

    assert controller._api.calls == []
    assert any("first page" in label.text for label in _labels(ui))


def test_page_seam_reports_explicit_validation_errors_without_a_facade_call():
    controller = _controller([_hit()])
    ui = _build_page(controller)

    _element(ui, "input", label="Published after").value = "2026-01-01"
    run(_element(ui, "button", text="Search").kwargs["on_click"](None))

    assert controller._api.calls == []
    assert any("evidence search failed (ValueError)" == label.text for label in _labels(ui))
