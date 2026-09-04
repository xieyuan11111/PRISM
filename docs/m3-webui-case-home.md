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
- historical state panels for nodes, facts, interpretations, relations,
  invalidated facts and evidence gaps;
- source and evidence locator data in the JSON-safe view model.

The controller/view-model is dependency-free and is tested with an injected
facade. This keeps the CLI and WebUI on the same API contract and makes core
behavior testable without starting NiceGUI or a browser.

## Explicit non-goals

The current slice does not include the debate theater, live streaming or
pause/continue controls, drag-and-drop ingestion, the corpus evidence browser,
model settings, multi-user authentication, or remote binding.
