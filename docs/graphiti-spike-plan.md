# PRISM Graphiti/Neo4j Spike — Phase Plan

> **Status**: Phase A (code + offline verification) is implemented. Phase B
> (live spike against a real PRISM-owned Graphiti/Neo4j instance) was executed
> and passed its three opt-in integration tests on 2026-09-03 against the
> isolated, loopback-only PRISM-owned Neo4j Community 5.26 server (HTTP
> 127.0.0.1:7475 / Bolt 127.0.0.1:7688) with graphiti-core 0.29.3, the neo4j
> Python driver 6.3.0 and httpx 0.28.1 (pinned in pyproject.toml; run record
> in section 6). The Phase B
> **persistence prerequisite** —
> a project-owned SQLite registry that durably maps PRISM `episode_key` to
> the real Graphiti-assigned uuid across process restarts — is implemented
> and verified on the live path (section 2); the live restart test confirmed
> the recorded uuid really matches the server's nodes.
>
> **Honesty note**: the live result covers deterministic model injection,
> persistence, search attribution, cutoff filtering, relation episodes and
> restart idempotency with synthetic cases. It does not validate
> real-provider extraction (real LLM/embedding/rerank calls), reruns over
> real case corpus material, or production-scale search pagination; those
> remain explicit remaining items in section 5.
>
> **Group/database semantics (live-validated for this spike)**:
> graphiti-core 0.29.3's `add_episode` source and live behavior confirm that
> an explicit `group_id` is treated as the Neo4j database
> selection: when `group_id` differs from `driver._database`, the client
> clones its driver with `database=group_id` and writes there.  Neo4j
> Community serves ONE built-in database, so a live Community config must use
> `group_id == database == "neo4j"`, and PRISM's `GraphitiConfig` rejects any
> enabled config where they differ. This result is limited to the isolated
> Community instance used by the spike.

---

## 1. Objective

Validate PRISM's temporal-graph adapter (episodes with `valid_at/invalid_at`,
incremental writes, historical cutoffs) against a **PRISM-owned** Graphiti +
Neo4j instance — never against any pre-existing GTI/Neo4j installation,
configuration, credentials or data.

| Phase | What | Where | Implemented? |
|---|---|---|---|
| A | Code, config, deploy template, docs, offline tests | this repository | ✅ yes (offline only) |
| A.5 | Persistent episode registry (PRISM key ↔ real Graphiti uuid), restart idempotency and body-less search attribution | this repository | ✅ yes (offline only — the Phase B persistence prerequisite) |
| B | Live spike: start PRISM-owned services, run opt-in integration tests, record results, pin versions | operator machine | ✅ yes — 3 tests passed; see section 6 run record |

---

## 2. Phase A artifacts (this repository)

| File | Purpose |
|---|---|
| `pyproject.toml` | `[project.optional-dependencies] graphiti` is pinned to the versions exercised by the live spike (`graphiti-core==0.29.3`, `neo4j==6.3.0`, `httpx==0.28.1`) — opt-in extra; default `dependencies` stays empty so the default runtime is fully offline. `httpx` is pinned because graphiti-core 0.29.3 imports it unconditionally at package import (its LLM client base) without declaring it in its own metadata; pytest is deliberately not part of the extra. |
| `src/prism/config/models.py` | `GraphitiConfig` — `enabled` (default false), `uri`, `database`, `group_id`, `username_env`, `password_env`, `timeout`. URI rejects embedded credentials and empty hosts; connections require an explicit non-default port (the standard 7474/7687 are never applied, so an enabled config cannot silently reach a default local Neo4j); `enabled` configs require an explicit `database` AND an explicit `group_id` **equal to it** (graphiti-core 0.29.3 realises a Neo4j group as a database — see the header note); env fields store variable **names**, never values; portable JSON; old config files without a `graphiti` section still load. |
| `src/prism/graph/backend.py` | `GraphitiBackend` + `GraphEpisodeRegistry` protocol — explicit `group_id`; first creation never passes a `uuid` (0.29.3: `uuid=None` creates, an explicit uuid is a `get_by_uuid` lookup that raises `NodeNotFoundError` on a miss — live-probe verified), the PRISM `episode_key` lives in the episode body (injected defensively when missing), and the Graphiti-assigned uuid returned by the client is recorded in an in-process auditable cache AND persisted through the injected registry (never treated as a PRISM key; a client echoing the key back records no uuid). The registry protocol adds group-scoped reverse lookup by the real Graphiti uuid, so a restarted process maps body-less `EntityEdge` search results through durable knowledge instead of in-process state. `search` passes only group parameters the injected client actually declares (`group_ids` plural on 0.29.3, else singular `group_id`, else none) and maps only episodes positively attributable to PRISM (graphiti-uuid audit cache, persistent registry reverse lookup by referenced uuid, registry knowledge of a referenced PRISM key, or PRISM schema marker in the returned body); group filtering is a **defensive adapter contract** for group-aware/multi-database servers, not a Community isolation mechanism; `close()` closes the client exactly once. |
| `src/prism/graph/registry.py` | `SQLiteEpisodeRegistry` — the project-owned persistent registry (Phase B prerequisite). It shares the EvidenceStore SQLite file (`index.db` under the data dir) and creates its `graphiti_episode_registry` table **additively** (`CREATE TABLE IF NOT EXISTS`), so databases created by older PRISM versions migrate in place with no change to existing rows. Rows persist the PRISM key, the real Graphiti uuid (NULL when none was extractable — never fabricated), group/database labels, the canonical episode body and UTC audit timestamps; close + reopen reads everything back. Never stores credentials, hosts or absolute paths. |
| `src/prism/graph/graphiti_client.py` | Lazy real-client construction: credentials are resolved from env **before** any graphiti-core/neo4j import; the keyword surface matches graphiti-core 0.29.3 and construction was live-verified to perform no eager database I/O (the Neo4j driver is lazy). Graphiti 0.29.3 fires its own anonymous-usage telemetry event at construction by default (outside pytest); the builder opts PRISM out (`GRAPHITI_TELEMETRY_ENABLED=false`) unless the operator explicitly exported true. The builder also wraps real clients so timeline `search` never silently inherits Graphiti's 10-result default window (forced to 100 — a bounded spike safeguard, not pagination; see section 5). |
| `src/prism/runtime/composition.py` | Default path never imports graphiti-core/neo4j, never probes dependencies, never builds a client and never creates a registry. `graphiti.enabled=true` attempts the real path only with an injected `graphiti_client_factory` or the optional dependencies installed; missing env/dependency fails with explicit errors. When the real backend is created the composition root also creates the SQLite registry (bound to the configured group/database) and injects it into the backend, so restart idempotency and body-less search attribution are automatic on the live path; `PrismRuntime.close()` closes the client and the registry it created (a caller-injected `graph_backend` is a full override: no client and no registry). |
| `deploy/graphiti-spike/` | Public deployment template (compose + env template + port preflight) — relative paths and placeholders only. |
| `docs/graphiti-spike-plan.md` | This plan. |
| `tests/test_graphiti_*.py` | Offline unit/contract tests + opt-in live integration tests (skipped unless env vars are set). `test_graphiti_registry.py` covers the SQLite registry (close/reopen round trips, uuid reverse lookup, group scoping, old-database migration, no fabricated uuids); `test_graphiti_backend.py` covers the adapter incl. persistent-registry restart idempotency and body-less `EntityEdge` attribution; `test_graphiti_runtime.py` covers registry creation/closure by the composition root and the offline default (no registry, no registry table). The two-group isolation test in `test_graphiti_backend.py` is a **pure adapter contract** test and is annotated as such: it is NOT a Community live acceptance item. |

---

## 3. Naming, ports and preflight (Phase B uses these)

- **Service name**: `prism-graphiti-spike` (compose project/service and
  container name).
- **Database and group**: the PRISM-owned container runs Neo4j Community
  Edition, which serves a **single built-in database named `neo4j`**.  The
  compose template sets no custom default database (a custom default database
  name is not created on the single-database edition and can prevent first
  start).  graphiti-core 0.29.3 realises a Neo4j group as a database:
  `add_episode` clones the driver to `database=group_id` whenever an explicit
  `group_id` differs from the connected database, so on this container the
  config MUST set `graphiti.database = "neo4j"` **and**
  `graphiti.group_id = "neo4j"` (equal), and `GraphitiConfig` rejects an
  enabled config whose group differs from its database before any client is
  built.  Database isolation comes from this **separate** PRISM-owned
  container (its own Neo4j home, service and data volume), never from
  multiple database names on a shared instance — the Community edition cannot
  host a second group/database, and the adapter's group filtering is a
  defensive contract for future multi-database/Enterprise servers, not a
  Community isolation mechanism.  The `database` value remains PRISM adapter
  metadata: graphiti-core 0.29.3's `Graphiti(uri, user, password, ...)`
  constructor does **not** consume a database argument, and PRISM does not
  forward one to that constructor.
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
    "group_id": "neo4j",
    "username_env": "",
    "password_env": "PRISM_GRAPHITI_PASSWORD",
    "timeout": 30.0
  }
}
```

Values above are examples/placeholders.  `PRISM_GRAPHITI_PASSWORD` is never
written into a config file.  `group_id` is not a second tenant inside the
instance: on 0.29.3 + Neo4j it is the database name, and the Community
container's only database is `neo4j` — so `group_id` and `database` are both
`neo4j`, and an enabled PRISM config with any other combination is rejected
at load time.

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
| `PRISM_GRAPHITI_DATABASE` | optional | database for the opt-in tests and runtime config example — default `neo4j`, the Community container's single built-in database. Not passed to the Graphiti 0.29.3 constructor; it must equal the group because graphiti realises a Neo4j group as a database (see section 3) |
| `PRISM_GRAPHITI_GROUP` | optional | group for the opt-in integration tests — defaults to `PRISM_GRAPHITI_DATABASE` and must stay equal to it (`neo4j` on the Community container); `GraphitiConfig` rejects a mismatch when enabled |
| `PRISM_GRAPHITI_HTTP_PORT` / `PRISM_GRAPHITI_BOLT_PORT` | optional | host ports published by compose (defaults 7475/7688) |
| `PRISM_HOME` | PRISM generally | local data/config directory |

---

## 5. Phase B findings and remaining boundaries

The following items were checked during the live run unless explicitly marked
as remaining:

1. **Resolved for this environment**: `graphiti-core==0.29.3`,
   `neo4j==6.3.0`, and `httpx==0.28.1` were installed and exercised.
2. **Resolved for this environment**: `graphiti_core.Graphiti(...)` construction
   completed with the configured live client and performs no eager database
   I/O (the Neo4j driver is lazy); the deterministic provider hooks
   prevented external model calls.  Graphiti 0.29.3 fires its own anonymous
   telemetry event at construction by default (outside pytest), so PRISM's
   builder sets `GRAPHITI_TELEMETRY_ENABLED=false` unless the operator
   explicitly exported `true`.
3. `add_episode`/`search` signature semantics — **verified by the
   0.29.3 live tests (2026-09-03)**:
   Its graphiti-core 0.29.3 keyword surface (`uri`, `user`, `password`)
   has been checked offline; that constructor does not accept or consume the
   PRISM `database` metadata, and PRISM's enabled configs satisfy the
   group-as-database rule (`group_id == database`, both `neo4j` on the
   Community container) before a client is ever built.

   - `add_episode(uuid=None)` (the default; PRISM omits the argument)
     CREATES a new episode under a Graphiti-assigned uuid.  Passing an
     explicit uuid performs a `get_by_uuid` lookup first and raises
     `NodeNotFoundError` when nothing exists under it — this is what broke
     the first live probe (the adapter used to pass the PRISM episode_key
     as the uuid).  The adapter now never passes a uuid; the PRISM key
     lives in the episode body and the returned Graphiti uuid is cached
     in-process for audit/attribution AND persisted in the SQLite registry
     (Phase A.5) when one is injected, never treated as a PRISM key.
   - `search` accepts `group_ids` (plural list), not a singular
     `group_id`.  The adapter negotiates the declared keyword
     (`group_ids`, else `group_id`, else none) and still relies on PRISM
     attribution plus group-mismatch filtering.
   - **Remaining**: production-scale pagination.  graphiti 0.29.3's `search`
     default window is 10 results over ALL entity edges in the group, so the
     real-client adapter forces an explicit 100-result window (a bounded
     spike safeguard).  All three live cases (≤ 8 episodes each) came back
     complete at the exercised cumulative graph size; timelines are NOT an
     exhaustive graph scan, so a case (or cumulative group) beyond ~100
     entity edges needs a real pagination design decision.
4. **Resolved for this environment**: `graphiti_core.nodes.EpisodeType.json`
   exists and is accepted as the `source` value for JSON episodes.
5. **Resolved for this test path**: add/search exercised extraction and
   embedding hooks using deterministic injected clients. Real provider calls,
   their cost and production configuration remain unvalidated.
6. **Resolved for the exercised path**: live search results carried the
   expected episode references and were attributed through the registry after
   restart. The 0.29.3 source shows `search` returns
   `EntityEdge` objects whose fields include `group_id` and an `episodes`
   list of episode uuid references (Graphiti-assigned, never PRISM keys) —
   not the episode body itself.  PRISM therefore maps results through the
   in-process graphiti-uuid audit cache, then through the persistent
   registry's group-scoped reverse lookup of the referenced uuid (the
   Phase A.5 restart path — a restarted process has no in-process cache),
   then registry knowledge of a referenced PRISM key, and bodies (with the
   PRISM schema marker) only when a result carries one.  The offline suite
   covers every one of these paths (incl. body-less attribution after a
   registry close/reopen); the live run confirmed which results are
   actually attributable after a restart and that the registry's recorded
   uuid really matches the server's nodes — for the exercised synthetic
   episodes.
7. **Resolved for this environment**: the isolated PRISM-owned Neo4j
   Community 5.26 server uses the built-in `neo4j` database; HTTP and Bolt
   stayed bound to 127.0.0.1 (ports 7475/7688), loopback-only.
8. **Not exercised**: the Docker Compose variant (compose/healthcheck
   syntax); Docker was not installed on the spike machine, so the
   standalone PRISM-owned native Neo4j launcher (own JDK, data dir and
   venv under the spike area) was started and used instead.  The deploy
   template remains the documented way to reproduce the spike elsewhere.

---

## 6. Phase B procedure and executed variant

1. Copy `deploy/graphiti-spike/.env.example` to
   `deploy/graphiti-spike/.env` and set `PRISM_GRAPHITI_PASSWORD` to a strong
   random value.
2. Run `python deploy/graphiti-spike/check_ports.py` (preflight; see
   section 3 — no guarantee of lasting availability).
3. Start the PRISM-owned services:
   `docker compose -f deploy/graphiti-spike/compose.yaml up -d`.
4. Export `PRISM_GRAPHITI_URI=bolt://localhost:7688` and
   `PRISM_GRAPHITI_PASSWORD` (and optionally `PRISM_GRAPHITI_USERNAME`;
   `PRISM_GRAPHITI_DATABASE`/`PRISM_GRAPHITI_GROUP` default to `neo4j` and
   must stay equal to each other).
5. Install the pinned optional extra: `pip install -e ".[graphiti]"`.
   The versions in `pyproject.toml` are the ones exercised by this spike.
6. Run the opt-in integration tests:
   `python -m pytest tests/test_graphiti_integration.py -v`.
7. Reconcile every Phase B finding in section 5 with what the live run shows,
   and update code/tests/docs accordingly.
8. Run the full offline suite again to prove the live spike did not break the
   default offline behavior.

**Executed variant and run record (2026-09-03)**. Docker was unavailable on
the spike machine, so the steps above were executed against the standalone
PRISM-owned native Neo4j Community 5.26 server under the spike area (its own
JDK, data directory and venv), started for this run; the service stayed bound
to 127.0.0.1 only (HTTP 7475 / Bolt 7688).  The spike connected exclusively
to that server and never to any other Neo4j on the machine.  Environment:
Python 3.12, `graphiti-core==0.29.3`, `neo4j==6.3.0`, `httpx==0.28.1`,
pytest in the isolated venv; credentials supplied by the environment (names
only — see section 4), `OPENAI_API_KEY` absent, Graphiti telemetry disabled.

Result: all three opt-in integration tests in
`tests/test_graphiti_integration.py` passed (live write/read/restart
idempotent rewrite; historical cutoffs excluding invalidated/unobserved;
M1 facts + relations + portable evidence surviving cutoffs and a registry
restart).  Every case/material/fact id in those tests carries a random
per-run suffix, so reruns never collide with data left by earlier runs in
the persistent spike database.  The tests injected deterministic in-process
Graphiti LLM/embedder/reranker clients (see
`tests/graphiti_live_deterministic.py`), so no real model provider was
called; the full offline suite was re-run afterwards and stayed green with
the pinned extra installed.

## 7. Phase B side effects (explicit)

Running Phase B on a machine:

- downloads the Neo4j container image and starts a container named
  `prism-graphiti-spike`;
- creates a PRISM-owned Docker volume
  (`prism_graphiti_neo4j_data`) holding real graph data;
- publishes host ports 7475/7688 (or the configured overrides) — this is a
  real, visible service bind;
- writes **real episodes** (PRISM domain data written through the adapter)
  into the PRISM-owned container's single `neo4j` database under
  group/database `neo4j` — the only group the Community edition can serve
  (graphiti 0.29.3 realises a group as a database; two groups on one
  Community instance are not possible, so no second group is ever written);
- may invoke Graphiti's embedding/extraction pipeline during `add_episode` /
  `search`, which can call external LLM/embedding APIs and incur cost or rate
  limits (verify item 5 in section 5 before assuming none).  The executed
  2026-09-03 run replaced all three providers (LLM, embedder, reranker) with
  deterministic in-process clients, so it never called an external model API;
  Graphiti's anonymous telemetry was disabled by PRISM's builder
  (`GRAPHITI_TELEMETRY_ENABLED=false`) and by pytest itself;
- creates/extends the PRISM SQLite file (`index.db` under `data_dir`) with
  the additive `graphiti_episode_registry` table: each successful first
  write records its PRISM key, real Graphiti uuid, group/database and
  canonical body there.  Existing rows and the EvidenceStore tables are
  untouched (the table is created with `CREATE TABLE IF NOT EXISTS`).
  Registry rows are authoritative PRISM-side mapping state: deleting or
  rebuilding `index.db` would lose them (timeline readback from stored
  bodies still works, but cross-restart write idempotency and body-less
  search attribution would degrade until rows are re-recorded);
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

- [x] Live double-write of the same case is idempotent across process
      restarts: the composition root injects the persistent SQLite registry
      on the enabled path, so a restarted runtime short-circuits every
      duplicate write by PRISM key before the client is called (no second
      Graphiti node).  A registry-less backend exists only for offline
      adapter tests / caller-injected custom registries — it is NOT the live
      path; without a registry a restarted process would re-write under a
      fresh Graphiti uuid (harmless for timelines because search dedups by
      the body's episode_key).
- [x] Restart simulation: after closing and recreating the runtime, timeline
      queries return the same episode keys written before the restart —
      including body-less `EntityEdge` search results attributed through the
      persisted registry mapping (offline-covered in
      `tests/test_graphiti_registry.py`, `tests/test_graphiti_backend.py`
      and `tests/test_graphiti_runtime.py`).
- [x] PRISM-dedicated instance isolation + schema marker: live Phase B data
      lives in the PRISM-owned Community container's single database under
      group == database == `neo4j`; isolation from any other PRISM
      environment is the dedicated instance itself (own home/service/data
      volume/ports).  Two-group isolation on one Community instance is NOT an
      acceptance item (graphiti 0.29.3 realises a Neo4j group as a database,
      and Community has one) — the adapter's group filtering stays an
      offline, pure-adapter-contract test plus a defensive filter for future
      multi-database servers.  Foreign data never enters PRISM timelines:
      search mapping requires positive attribution (registry/cache uuid
      knowledge or the PRISM schema marker in an episode body).
- [x] A fact revision with `invalid_at` is excluded after its invalidation;
      the replacing fact is included.
- [x] Conflicting facts (same subject/predicate/object, different sources)
      coexist at the same cutoff with distinct keys.
- [x] Two different historical cutoffs return different, correct states
      (material publication boundaries respected).
- [x] `PrismRuntime.close()` closes the client/driver exactly once without
      error, and the offline suite (`python -m pytest -q`) still passes with
      the optional extra installed.
- [x] Port preflight: the spike server binds loopback-only on the PRISM-owned
      ports (HTTP 127.0.0.1:7475 / Bolt 127.0.0.1:7688); occupied
      default-port services on the machine were never touched — the spike
      connected only to its own PRISM-owned instance.

Scope of the 2026-09-03 acceptance: every marked item above was confirmed by
the live run (deterministic provider clients; no external model API) and/or
the offline suite. Real-provider extraction, reruns over real case corpus
material and production-scale search pagination are NOT part of this
acceptance and remain open (section 5).
