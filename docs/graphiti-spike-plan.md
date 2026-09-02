# PRISM Graphiti/Neo4j Spike — Phase Plan

> **Status**: Phase A (code + offline verification) is implemented.  Phase B
> (live spike against a real PRISM-owned Graphiti/Neo4j instance) is **NOT
> implemented** by this document or by the Phase A code — it is a plan with
> explicit side effects, acceptance criteria and rollback so an operator can
> run it later without surprises.
>
> **Honesty note**: as of Phase A, the real Graphiti integration has **not**
> been validated against a live server.  Every "PHASE B VERIFY" item below is
> an assumption that the live spike must confirm or correct.

---

## 1. Objective

Validate PRISM's temporal-graph adapter (episodes with `valid_at/invalid_at`,
incremental writes, historical cutoffs) against a **PRISM-owned** Graphiti +
Neo4j instance — never against any pre-existing GTI/Neo4j installation,
configuration, credentials or data.

| Phase | What | Where | Implemented? |
|---|---|---|---|
| A | Code, config, deploy template, docs, offline tests | this repository | ✅ yes (offline only) |
| B | Live spike: start PRISM-owned services, run opt-in integration tests, record results, pin versions | operator machine | ❌ no — planned below |

---

## 2. Phase A artifacts (this repository)

| File | Purpose |
|---|---|
| `pyproject.toml` | `[project.optional-dependencies] graphiti = ["graphiti-core", "neo4j"]` — opt-in extra; default `dependencies` stays empty so the default runtime is fully offline. Version bounds are deliberately **unpinned**: Phase A cannot verify real minimums offline; Phase B pins them after the first live install. |
| `src/prism/config/models.py` | `GraphitiConfig` — `enabled` (default false), `uri`, `database`, `group_id`, `username_env`, `password_env`, `timeout`. URI rejects embedded credentials and empty hosts; connections require an explicit non-default port (the standard 7474/7687 are never applied, so an enabled config cannot silently reach a default local Neo4j); env fields store variable **names**, never values; portable JSON; old config files without a `graphiti` section still load. |
| `src/prism/graph/backend.py` | `GraphitiBackend` — explicit `group_id`; injected `GraphEpisodeRegistry` gives write-before existence lookup for real persistent idempotency; the in-process cache is documented as non-persistent; `search` maps only episodes positively attributable to PRISM (registry/cache uuid knowledge or PRISM schema marker in the returned body); `close()` closes the client exactly once. |
| `src/prism/graph/graphiti_client.py` | Lazy real-client construction: credentials are resolved from env **before** any graphiti-core/neo4j import; construction site is isolated and marked PHASE B VERIFY. |
| `src/prism/runtime/composition.py` | Default path never imports graphiti-core/neo4j, never probes dependencies, never builds a client. `graphiti.enabled=true` attempts the real path only with an injected `graphiti_client_factory` or the optional dependencies installed; missing env/dependency fails with explicit errors; `PrismRuntime.close()` closes resources the runtime created. |
| `deploy/graphiti-spike/` | Public deployment template (compose + env template + port preflight) — relative paths and placeholders only. |
| `docs/graphiti-spike-plan.md` | This plan. |
| `tests/test_graphiti_*.py` | Offline unit/contract tests + opt-in live integration tests (skipped unless env vars are set). |

---

## 3. Naming, ports and preflight (Phase B uses these)

- **Service name**: `prism-graphiti-spike` (compose project/service and
  container name).
- **Database**: the PRISM-owned container runs Neo4j Community Edition,
  which serves a **single built-in database named `neo4j`**.  The compose
  template sets no custom default database (a custom default database name
  is not created on the single-database edition and can prevent first
  start), so config `graphiti.database` must be `neo4j` — or left empty for
  the server default — unless a live Phase B run verifies the server's actual
  capabilities.  Database isolation comes from this **separate** PRISM-owned
  container (its own Neo4j home, service and data volume), never from
  multiple database names on a shared instance.
- **Ports**: host **7475** (HTTP) and **7688** (Bolt), mapped to the
  container's standard 7474/7687.  These avoid the default local Neo4j ports
  but are only **suggested values**.
- **Preflight**: run `python deploy/graphiti-spike/check_ports.py` before
  `docker compose up`.  A successful preflight does **not guarantee** the
  ports remain free until the container binds (a concurrent process can race
  it); treat it as a reduction of risk, not a reservation.
- **Config the operator writes** (in `PRISM_HOME/config.json` or an explicit
  config):

```json
{
  "graphiti": {
    "enabled": true,
    "uri": "bolt://localhost:7688",
    "database": "neo4j",
    "group_id": "prism-spike",
    "username_env": "",
    "password_env": "PRISM_GRAPHITI_PASSWORD",
    "timeout": 30.0
  }
}
```

Values above are examples/placeholders.  `PRISM_GRAPHITI_PASSWORD` is never
written into a config file.

---

## 4. Environment variables (names only)

The live spike reads credentials and connection settings from the process
environment; config files store only the **names** of the credential
variables.

| Variable | Required by | Purpose |
|---|---|---|
| `PRISM_GRAPHITI_URI` | opt-in integration tests | `bolt://host:port` of the PRISM-owned Neo4j |
| `PRISM_GRAPHITI_PASSWORD` | runtime + tests + compose | password for the PRISM-owned container user |
| `PRISM_GRAPHITI_USERNAME` | optional | only when `graphiti.username_env` is set; otherwise PRISM uses Neo4j's standard user `neo4j` |
| `PRISM_GRAPHITI_DATABASE` | optional | database name for the opt-in tests and the runtime config example — the Community container's single built-in database is `neo4j` |
| `PRISM_GRAPHITI_HTTP_PORT` / `PRISM_GRAPHITI_BOLT_PORT` | optional | host ports published by compose (defaults 7475/7688) |
| `PRISM_HOME` | PRISM generally | local data/config directory |

---

## 5. Unverified assumptions (Phase B verify list)

Phase A could not check these offline; each is a candidate for correction on
the first live run, isolated so fixes stay small:

1. `graphiti-core` / `neo4j` minimum versions (pyproject extra is unpinned on
   purpose) — pin after the first live install.
2. `graphiti_core.Graphiti(...)` constructor keyword surface used in
   `src/prism/graph/graphiti_client.py` (`neo4j_uri`, `neo4j_user`,
   `neo4j_password`, `neo4j_database`) and that construction performs no
   eager network I/O.
3. `client.add_episode(...)` / `client.search(...)` accept a `group_id`
   keyword (the adapter introspects and negotiates it, but a live run must
   confirm the real signature and semantics).
4. `graphiti_core.nodes.EpisodeType.json` exists and is the right `source`
   value for JSON episodes.
5. Whether `add_episode` and/or `search` trigger Graphiti's extraction or
   embedding pipeline (LLM/embedding API calls and cost) and what
   configuration that requires.
6. Whether search results return `episode_body` and `group_id` (the adapter
   maps bodies with the PRISM schema marker and filters by group when
   present).
7. The pinned image's default database name and Community single-database
   behavior (the template assumes the built-in `neo4j` database; confirm at
   the first live start).
8. Compose healthcheck syntax for the pinned image tag.

---

## 6. Phase B procedure (planned — NOT executed by Phase A)

1. Copy `deploy/graphiti-spike/.env.example` to
   `deploy/graphiti-spike/.env` and set `PRISM_GRAPHITI_PASSWORD` to a strong
   random value.
2. Run `python deploy/graphiti-spike/check_ports.py` (preflight; see
   section 3 — no guarantee of lasting availability).
3. Start the PRISM-owned services:
   `docker compose -f deploy/graphiti-spike/compose.yaml up -d`.
4. Export `PRISM_GRAPHITI_URI=bolt://localhost:7688` and
   `PRISM_GRAPHITI_PASSWORD` (and optionally `PRISM_GRAPHITI_USERNAME` /
   `PRISM_GRAPHITI_DATABASE`).
5. Install the optional extra: `pip install -e ".[graphiti]"` and pin the
   versions in `pyproject.toml` once the real minimums are known.
6. Run the opt-in integration tests:
   `python -m pytest tests/test_graphiti_integration.py -v`.
7. Reconcile every PHASE B VERIFY item in section 5 with what the live run
   shows, and update code/tests/docs accordingly.
8. Run the full offline suite again to prove the live spike did not break the
   default offline behavior.

## 7. Phase B side effects (explicit)

Running Phase B on a machine:

- downloads the Neo4j container image and starts a container named
  `prism-graphiti-spike`;
- creates a PRISM-owned Docker volume
  (`prism_graphiti_neo4j_data`) holding real graph data;
- publishes host ports 7475/7688 (or the configured overrides) — this is a
  real, visible service bind;
- writes **real episodes** (PRISM domain data written through the adapter)
  into the PRISM-owned container's default `neo4j` database under group
  `prism-spike`;
- may invoke Graphiti's embedding/extraction pipeline during `add_episode` /
  `search`, which can call external LLM/embedding APIs and incur cost or rate
  limits (verify item 5 in section 5 before assuming none);
- leaves all of the above running/data present until explicitly torn down
  (section 8).

Phase A deliberately performs none of these: no containers, no database, no
venv/package installs, no network, no service start/stop, no config or
credential reads of any existing GTI/Neo4j setup.

## 8. Rollback (planned)

1. Stop and remove the PRISM-owned container **and its data volume**:
   `docker compose -f deploy/graphiti-spike/compose.yaml down -v`.
2. Delete `deploy/graphiti-spike/.env` (it holds the real password).
3. Unset the environment variables from section 4 in the shell/profile.
4. Remove the `graphiti` block from the PRISM config (or set
   `"enabled": false`) — the runtime then returns to the fully offline
   default with no code change required.
5. If the live spike forced code corrections, review and keep only the
   corrections that the acceptance criteria below justify; the spike never
   changes default offline behavior.

## 9. Acceptance criteria (Phase B)

- [ ] Live double-write of the same case is idempotent (second write adds
      nothing) with a registry injected, and without a registry the restart
      case is understood and documented.
- [ ] Restart simulation: after closing and recreating the runtime, timeline
      queries return the same episode keys written before the restart.
- [ ] Group isolation: two groups on the shared PRISM-owned instance never
      see each other's episodes through the adapter.
- [ ] A fact revision with `invalid_at` is excluded after its invalidation;
      the replacing fact is included.
- [ ] Conflicting facts (same subject/predicate/object, different sources)
      coexist at the same cutoff with distinct keys.
- [ ] Two different historical cutoffs return different, correct states
      (material publication boundaries respected).
- [ ] `PrismRuntime.close()` closes the client/driver exactly once without
      error, and the offline suite (`python -m pytest -q`) still passes with
      the optional extra installed.
- [ ] Port preflight ran before startup; occupied ports were handled by
      changing `.env`, not by touching other services.
