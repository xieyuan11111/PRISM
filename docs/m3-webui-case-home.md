# M3 NiceGUI Case Home and Historical Timeline

## Scope

PRISM now has an optional NiceGUI shell for the case overview and historical
snapshot workflow. The shell is deliberately thin: it calls the existing
`PrismAPI.case_overviews()` and `PrismAPI.query_historical_snapshot()` methods
and does not access Graphiti, SQLite, or the LLM router directly.

Install the optional UI dependency when running the server:

```console
pip install -e ".[webui]"
python -m prism.webui
```

The default server binding is loopback (`127.0.0.1`) and browser auto-open is
disabled. A non-loopback host is rejected by the default runner in this slice;
remote exposure and authentication are intentionally not part of this
milestone.

## Current interaction

The case home provides:

- case id search and type/status/unresolved filters;
- case selection;
- a timezone-aware historical cutoff input;
- deterministic recorded-stage filtering;
- entry-kind filtering;
- a Plotly historical timeline with deterministic point ordering and distinct
  layer colors plus effective/invalidated marker symbols and labels;
- historical state panels for nodes, facts, interpretations, relations,
  invalidated facts and evidence gaps;
- point selection by stable `episode_key`, showing the full entry and its
  source ids and portable evidence locators (`corpus_path`, paragraph/page and
  quote) from the already-loaded snapshot.

The controller/view-model is dependency-free and is tested with an injected
facade. This keeps the CLI and WebUI on the same API contract and makes core
behavior testable without starting NiceGUI or a browser. It flattens the
effective and invalidated buckets only after the facade returns; it performs
no UI-side temporal filtering and never invents a stage or fact. Selecting a
point is an in-memory lookup in that same snapshot and unknown point ids are
reported explicitly without another API call.

NiceGUI and Plotly are optional and imported lazily. Core/controller imports
need neither package. If timeline rendering is requested without Plotly, the
timeline-enabled app factory reports a clear `.[webui]` installation error and
does not substitute a hand-built or fake figure.

## Explicit non-goals

The current slice does not include the debate theater, live streaming or
pause/continue controls, drag-and-drop ingestion, the corpus evidence browser,
model settings, multi-user authentication, or remote binding.
