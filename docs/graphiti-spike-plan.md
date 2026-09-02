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
>
> **Group/database semantics (source-confirmed, not yet live-validated)**:
> graphiti-core 0.29.3's `add_episode` source (inspected from the installed
> package during Phase A) treats an explicit `group_id` as the Neo4j database
> selection: when `group_id` differs from `driver._database`, the client
> clones its driver with `database=group_id` and writes there.  Neo4j
> Community serves ONE built-in database, so a live Community config must use
> `group_id == database == "neo4j"`, and PRISM's `GraphitiConfig` rejects any
> enabled config where they differ.  Whether that holds end to end on a live
> server remains a Phase B item.

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
| `src/prism/config/models.py` | `GraphitiConfig` — `enabled` (default false), `uri`, `database`, `group_id`, `username_env`, `password_env`, `timeout`. URI rejects embedded credentials and empty hosts; connections require an explicit non-default port (the standard 7474/7687 are never applied, so an enabled config cannot silently reach a default local Neo4j); `enabled` configs require an explicit `database` AND an explicit `group_id` **equal to it** (graphiti-core 0.29.3 realises a Neo4j group as a database — see the header note); env fields store variable **names**, never values; portable JSON; old config files without a `graphiti` section still load. |
| `src/prism/graph/backend.py` | `GraphitiBackend` — explicit `group_id`; injected `GraphEpisodeRegistry` gives write-before existence lookup for real persistent idempotency; the in-process cache is documented as non-persistent; `search` maps only episodes positively attributable to PRISM (registry/cache uuid knowledge or PRISM schema marker in the returned body); group filtering is a **defensive adapter contract** for group-aware/multi-database servers, not a Community isolation mechanism; `close()` closes the client exactly once. |
| `src/prism/graph/graphiti_client.py` | Lazy real-client construction: credentials are resolved from env **before** any graphiti-core/neo4j import; the keyword surface matches graphiti-core 0.29.3, while eager network behavior remains marked PHASE B VERIFY. |
| `src/prism/runtime/composition.py` | Default path never imports graphiti-core/neo4j, never probes dependencies, never builds a client. `graphiti.enabled=true` attempts the real path only with an injected `graphiti_client_factory` or the optional dependencies installed; missing env/dependency fails with explicit errors; `PrismRuntime.close()` closes resources the runtime created. |
| `deploy/graphiti-spike/` | Public deployment template (compose + env template + port preflight) — relative paths and placeholders only. |
| `docs/graphiti-spike-plan.md` | This plan. |
| `tests/test_graphiti_*.py` | Offline unit/contract tests + opt-in live integration tests (skipped unless env vars are set). The two-group isolation test in `test_graphiti_backend.py` is a **pure adapter contract** test and is annotated as such: it is NOT a Community live acceptance item. |

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

## 5. Unverified assumptions (Phase B verify list)

Phase A could not check these offline; each is a candidate for correction on
the first live run, isolated so fixes stay small:

1. `graphiti-core` / `neo4j` minimum versions (pyproject extra is unpinned on
   purpose) — pin after the first live install.
2. Whether `graphiti_core.Graphiti(...)` construction performs eager network
   I/O.  Its graphiti-core 0.29.3 keyword surface (`uri`, `user`, `password`)
   has been checked offline; that constructor does not accept or consume the
   PRISM `database` metadata, and PRISM's enabled configs satisfy the
   group-as-database rule (`group_id == database`, both `neo4j` on the
   Community container) before a client is ever built.
3. `add_episode`/`search` group semantics: the 0.29.3 source confirms
   `add_episode` accepts an explicit `group_id` and clones the driver to
   `database=group_id` whenever it differs from the connected database, and
   `search` accepts `group_ids` (plural list), not a singular `group_id`.  The
   adapter introspects and negotiates the singular keyword (so the real
   `search` is called without it and relies on PRISM attribution + group
   mismatch filtering); a live run must confirm the real behavior end to end.
4. `graphiti_core.nodes.EpisodeType.json` exists and is the right `source`
   value for JSON episodes.
5. Whether `add_episode` and/or `search` trigger Graphiti's extraction or
   embedding pipeline (LLM/embedding API calls and cost) and what
   configuration that requires.
6. What live search results carry: the 0.29.3 source shows `search` returns
   `EntityEdge` objects whose fields include `group_id` and an `episodes`
   list of episode uuid references — not the episode body itself.  PRISM
   therefore maps results through registry/cache uuid knowledge, and bodies
   (with the PRISM schema marker) only when a result carries one; a live run
   must confirm which results are actually attributable after a restart.
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
   `PRISM_GRAPHITI_PASSWORD` (and optionally `PRISM_GRAPHITI_USERNAME`;
   `PRISM_GRAPHITI_DATABASE`/`PRISM_GRAPHITI_GROUP` default to `neo4j` and
   must stay equal to each other).
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
  into the PRISM-owned container's single `neo4j` database under
  group/database `neo4j` — the only group the Community edition can serve
  (graphiti 0.29.3 realises a group as a database; two groups on one
  Community instance are not possible, so no second group is ever written);
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
- [ ] PRISM-dedicated instance isolation + schema marker: live Phase B data
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
