# Changelog

All notable changes to PRISM are documented here.

## Unreleased v1.0.0-rc.1

### Added

- Local NiceGUI pages for case history, debate, evidence search and material entry.
- Immutable report versions and PDF export.
- Historical snapshots, deterministic stage filtering and time-point comparison.
- Official OpenAI Python SDK transport as PRISM's own LLM transport.
- Experimental prompt profiles (`protocol-v1`, `protocol-v2`), prompt benchmark and `split-v1` two-stage extraction.
- Native Neo4j / Graphiti real mechanism acceptance runner and quality gate.
- v1 scope and release-status documentation.
- v1.0.0-rc.1 local WebUI smoke and acceptance report.

### Changed

- PRISM documents the native Neo4j launcher route; Docker and Docker Compose are not installation, CI or acceptance prerequisites.
- Real LLM results distinguish `mechanism_status` from `semantic_status`.

### Known boundaries

- Real Graphiti/provider mechanism paths have been exercised; real LLM semantic quality remains partial.
- `protocol-v1`, `protocol-v2` and `split-v1` remain experimental; baseline is the default extraction path.
- Full lifecycle and multi-case product workflows remain future work.

## Earlier v0.x work

- Domain models, ingestion, SQLite/FTS5, pipeline, graph adapter, LLM router, debate, reports and WebUI foundations were implemented with offline test coverage.
