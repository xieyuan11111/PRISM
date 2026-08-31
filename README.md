# PRISM

PRISM is an open-source system for tracing how policies, academic arguments, and public issues evolve over time.

Current foundation modules:

- immutable, validated domain models;
- portable configuration with `PRISM_HOME` support;
- Markdown/PDF/OCR ingestion with raw-file retention;
- SQLite/FTS5 evidence indexing and filtered search;
- dependency-optional Graphiti/GTI temporal graph adapter and historical timeline contract;
- offline tests for every completed module.

Run the offline test suite with:

```console
python -m pytest -q
```

The corpus Markdown files are the readable source of truth. SQLite is a rebuildable text index, while Graphiti/GTI is an optional temporal graph backend. Real provider credentials and external services are not required for the test suite.
