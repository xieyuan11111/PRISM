# PRISM v1.0.0-rc.1 Acceptance Report

**Date:** 2026-09-06
**Release candidate scope:** local, single-user, loopback-only WebUI product

## Product definition

PRISM RC is an auditable policy and academic-discourse evolution tracker. It is not a final-decision engine. It displays pipeline status, mechanism status, semantic status and evidence gaps rather than converting partial LLM output into a success claim.

## Offline release gates

```text
pytest: 1605 passed, 5 skipped
ruff check .: passed
memory-only Python compile: passed
git diff --check: passed
uv.lock: present
```

The skips are opt-in live Graphiti tests without the required local environment variables, plus dependency-state skips documented by their tests.

## Documentation and governance

```text
v1 scope: present
release status: present
WebUI getting-started guide: present
CONTRIBUTING.md: present
CHANGELOG.md: present
CODE_OF_CONDUCT.md: present
SECURITY.md: present
GitHub Actions offline CI: present
```

The project route uses native Neo4j when Graphiti is enabled. Docker and Docker Compose are not installation, CI, runtime or acceptance prerequisites.

## Real mechanism evidence

Project-external real runs have established:

```text
real provider + real materials + real Graphiti mechanism path: pass
Graphiti write / restart-readback / historical cutoff / report / PDF: pass
real LLM semantic quality: partial
```

Flash `protocol-v2 + split-v1` achieved a stable core node/fact intersection on one narrow policy material, but second policy and academic cross-case experiments remained unstable. Accordingly:

```text
baseline remains the default extraction path
protocol-v1, protocol-v2 and split-v1 remain experimental
```

The housing-fund lifecycle candidate formed a draft/implementation/revision middle chain but did not verify proposal/publication/expiry/replacement as a complete lifecycle. The missing expiry evidence is recorded, not inferred.

## WebUI smoke

The local NiceGUI server was started with a real project-external Phase 4 `PRISM_HOME` and bound only to `127.0.0.1:8765`.

```text
/           HTTP 200
/debate     HTTP 200
/evidence   HTTP 200
/materials  HTTP 200
```

The runtime facade loaded the real case list and evidence search results. Its offline graph backend returned an explicit historical gap rather than inventing a timeline. The temporary WebUI process was stopped and port 8765 was verified released.

A browser automation daemon failed to start in the host environment; this is recorded as a tooling limitation. Route HTTP responses, the actual runtime facade and dedicated WebUI tests were used for the smoke evidence. No remote binding, authentication, multi-user workflow or Docker deployment was exercised.

## Release decision

```text
RC mechanism/product readiness: pass
LLM semantic generalization: partial
release label: v1.0.0-rc.1
```

The RC is suitable as a local preliminary product. It must continue to present experimental extraction profiles and semantic gaps honestly. The next release decision should follow user feedback from local WebUI use; v1.1 candidates include multi-case interaction, Graphiti pagination, source automation and richer task controls.
