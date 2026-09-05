# Prompt Profiles and the Offline Prompt-Profile Benchmark

PRISM's semantic quality lives in the LLM prompt; deterministic code only
guards the evidence, time, source, case and relation boundaries.  A
*prompt profile* is therefore an explicitly selected, additive instruction
block in front of the untouched baseline strict evolution prompt — nothing
else.  Profiles never relax quote, time, source, case or relation
validation, and never create relations by hand: candidate acceptance is
decided by the same strict parser regardless of profile.

## The controlled profile parameter

`ExtractionService` accepts one optional keyword:

```python
ExtractionService(router, evidence_locator=store.locate)              # baseline (default)
ExtractionService(router, evidence_locator=store.locate,
                  prompt_profile="protocol-v1")                        # experimental, opt-in
ExtractionService(router, evidence_locator=store.locate,
                  prompt_profile="protocol-v2")                        # experimental, opt-in
```

Rules enforced in code (`src/prism/extraction/profiles.py`):

* `None` (the default; also spelled `"baseline"`) keeps the strict
  evolution prompt **byte-identical** to the production contract.  The
  default production composition passes no profile.
* `"protocol-v1"` and `"protocol-v2"` are the only experimental profiles.
  Each *prepends* its block in front of the complete baseline prompt; the
  baseline bytes are never edited.
* Unknown, empty, wrongly-cased or non-string profile names raise
  `ValueError` at construction — a typo can never silently run baseline.
* The profile applies only to `extract_material` (the strict evolution
  prompt).  The legacy `extract` entry point rejects every non-baseline
  profile.
* The profile changes the prompt only: the same completion parses to the
  same `ExtractionResult` under baseline, protocol-v1 and protocol-v2.
* Protocol-v2 is never a default and never a config-file or SQLite value;
  it is an explicit constructor/CLI selection only.

## Wiring profiles into real runs

`create_runtime` accepts the same controlled keyword — a constructor
argument only, never written into any config file or SQLite schema:

```python
await create_runtime(config_path)                                # baseline (default)
await create_runtime(config_path, prompt_profile="protocol-v1")  # experimental, opt-in
await create_runtime(config_path, prompt_profile="protocol-v2")  # experimental, opt-in
```

Fail-closed rules enforced at composition:

* `None`/`"baseline"` keep the default LLM `ExtractionService` byte-identical
  baseline (the default production composition).
* An experimental profile may only flow into the **default** LLM
  `ExtractionService` that composition itself creates.  Combining it with an
  injected `extraction_service`, or requesting one without a configured LLM
  router, raises `ValueError` — a selection can never be silently ignored.

The live acceptance runner exposes the same selection on the CLI:

```
python tools/run_live_case_acceptance.py \
    --source-root <PRISM material workspace> \
    --output-dir <acceptance output directory> \
    --prompt-profile protocol-v2 \
    --run-id live-001
```

`--prompt-profile` defaults to `baseline`; unknown values are rejected
before any filesystem or network work (exit code 2).  The value is passed
to `create_runtime` for every runtime the runner composes (first pass and
fresh restart).  Public summaries carry the profile's safe label
(`prompt_profile`) — runtime status and privacy output never contain any
prompt text.

## protocol-v1: SILENT PRE-JSON SELF-CHECK

The block instructs the model to verify, silently before writing any
JSON:

1. **Quote check** — every evidence quote is a verbatim, continuous,
   character-for-character fragment of one non-empty paragraph and is
   paragraph-unique; failing candidates are dropped, not repaired.
2. **Time check** — `observed_at` / `valid_at` / `happened_at` /
   `stated_at` (whichever apply) are timezone-aware, ordered
   (`observed_at` not earlier than the others), and none is later than
   the material's `fetched_at` (the concrete bound is embedded in the
   block).
3. **Prediction check** — forecasts, possibilities, recommendations and
   hypotheticals appear only as claims with `claim_type: prediction` and
   `stance: uncertain`, never as nodes, temporal facts, conflicts or
   relations.
4. **Relation check** — a relation is emitted only when the material
   explicitly states it, `source_ref`/`target_ref` reference candidates
   actually emitted in the same response, and a verbatim quote supports
   it; otherwise `relations: []`.  Chronology alone is never causality.

The block also states that the self-check is never part of the response:
the returned JSON must contain no self-check results, notes or fields.

## protocol-v2: canonical event/fact identities

Protocol-v2 is experimental and opt-in.  It retains all four protocol-v1
silent checks, then adds **CANONICAL ID CHECK**.  The model silently
selects one canonical event/fact identity before output and constructs
IDs with only ASCII lowercase letters, digits and `-`:

* node: `node-{node_type}-{source_id}-{YYYYMMDD}`
* temporal_fact: `fact-{source_id}-p{paragraph}-{ordinal}`
* claim: `claim-{source_id}-p{paragraph}-{ordinal}`
* relation: `rel-{relation_type}-{source_ref}-{target_ref}`

`node_type`, `source_id` and `relation_type` are normalized
deterministically: lowercase, replace every character other than ASCII
lowercase letters, ASCII digits and `-` with `-`, collapse consecutive
hyphens, and remove leading/trailing hyphens.  A common policy change in
one material, on one date, with one node type produces one node; details
belong in facts.  Paragraph/ordinal values are positive integers, with
the ordinal representing 1-based original-text order among same-kind
candidates in that paragraph.  If either value cannot be determined, the
candidate is dropped.

The check forbids semantic English naming, topic slugs, random numbers,
underscores, uppercase and non-ASCII characters.  It adds no JSON field
or metadata: canonical IDs use the existing `id`, `fact_id`, `claim_id`
and `relation_id` fields only.  Reference fields must point to candidates
actually emitted in the same response.  A relation still requires an
explicitly stated relationship and verbatim evidence.  The ID rule never
replaces quote, time, source, case or relation validation.

## Preregistered protocol-v2 evaluation

Live comparisons use the same materials, case, policy revision window and
runner configuration; only the explicit prompt profile varies.  Each
profile/case group requires at least two completed runs.  Report all
three metric families for every group:

1. **Stable key intersection** — per candidate kind, report ID
   intersection, union and intersection/union across runs.  Node and
   temporal_fact IDs are the primary endpoints because protocol-v1
   already reduced claim-layer drift while node/fact IDs remained
   unstable.  Claims are secondary; relations are evaluated only when the
   material explicitly states them.
2. **Failure rate** — report material-level extraction failures and
   candidate validation gaps (especially `candidate_validation_failed`).
   Protocol-v2 is not accepted if it increases the failure rate relative
   to the comparison profile.
3. **Evidence coverage** — report both `source_ids` and
   `evidence_locator` coverage rates.  Protocol-v2 is not accepted if it
   reduces either coverage measure.

A protocol-v2 result is considered better only when stable key
intersection/union improves or is non-inferior, the failure rate does not
increase, and evidence coverage does not decrease.  If stability improves
while failures increase or coverage falls, the result is a tradeoff, not
a win.  Candidate and node counts are diagnostic context only and are
never a victory condition.

## The live-run bridge: `prompt-run-summary.json`

On a **pass/partial** live acceptance run, the runner writes one more
artifact next to the acceptance summary: `prompt-run-summary.json`, a
strictly sanitized per-run summary that `prism_prompt_benchmark.py` reads
**directly** — no manual conversion step between live runs and the
benchmark.

The bridge is a projection of *this run's own* resources only:

* candidate ids/types per kind (`node`, `temporal_fact`, `claim`,
  `conflict`, `relation`) come from the run's own SQLite
  `case_extraction_ledger` (read-only, this run's PRISM home, this run's
  target case only — never other directories, other databases or old
  runs);
* `gap_types` come from the same ledger rows' evidence gaps;
* `coverage` rates and the `mechanism`/`semantic` verdict statuses come
  from the run's quality-gate results.

Sanitization is structural, not cosmetic: the projection emits only safe
opaque labels, closed-vocabulary type labels (with an `other`/`unset`
bucket), counts, rates and statuses.  Candidate ids that are not safe
labels (prose, paths, over-long strings) are dropped, never rewritten.
Material content, quotes, candidate payloads, corpus and absolute paths,
secrets and prompt text have no code path into the bridge, and the
finished payload passes the same privacy guard as the acceptance summary
before it is written.

The `run_id` is either an explicit `--run-id` (validated as a safe label)
or safely auto-generated as `run-<UTC timestamp>-<random token>` — a
stable, readable safe label derived from neither private paths nor
material content.

### Benchmarking live runs directly

The benchmark's `--input` accepts the runner's bridge files verbatim:

```
python tools/prism_prompt_benchmark.py \
    --input runs/live-baseline/prompt-run-summary.json \
    --input runs/live-protocol-v1/prompt-run-summary.json \
    --input runs/live-protocol-v2/prompt-run-summary.json \
    --input runs/live-protocol-v2-r2/prompt-run-summary.json \
    --output bench-manifest.json --indent
```

Repeat a run (fresh `--run-id` each time) to give a profile × case group
at least two runs; with a single run the group is reported as
`insufficient_runs`.

## The offline benchmark tool

`tools/prism_prompt_benchmark.py` is stdlib-only and offline-only.  It
reads **already-sanitized** per-run JSON summaries (one file per run —
hand-authored offline inputs and live `prompt-run-summary.json` bridges
alike) and computes stability per `profile` × `case` across repeated
runs:

* per candidate kind (`node`, `temporal_fact`, `claim`, `conflict`,
  `relation`): per-run id lists, id intersection / union / frequency /
  stability rate, and type-label intersection / union / frequency;
* gap-type per-run maps and frequency;
* coverage metric min/max across runs;
* explicit, separate `mechanism` and `semantic` verdict-status counts;
* an overall `stability` verdict with reasons.

```
python tools/prism_prompt_benchmark.py \
    --input runs/protocol-v1/ --input runs/protocol-v2/ \
    --output bench-manifest.json --indent
```

### Per-run summary input contract

One JSON object per run, sanitized upstream to ids and closed
vocabularies:

```json
{
  "schema_version": 1,
  "profile": "protocol-v2",
  "run_id": "run-01",
  "case_id": "case-alpha",
  "candidates": {
    "node": {"ids": ["proposal"], "types": {"proposal": 1}},
    "temporal_fact": {"ids": [], "types": {}},
    "claim": {"ids": ["forecast"], "types": {"prediction": 1}},
    "conflict": {"ids": [], "types": {}},
    "relation": {"ids": [], "types": {}}
  },
  "gap_types": {"candidate_validation_failed": 1},
  "coverage": {"source_ids": 1.0, "evidence_locator": 1.0},
  "verdict": {"mechanism_status": "pass", "semantic_status": "partial"}
}
```

`gap_types`, `coverage` and `verdict` are optional.  The reader rejects
summaries carrying material-like fields (`quote`, `content`, `body`,
`prompt`, `path`, secret-bearing keys, …), unknown top-level fields,
unknown candidate kinds, over-long prose values, invalid statuses and
duplicate `(profile, case, run_id)` triples.

### Verdict policy

* **Node counts are never a success criterion.**  A profile is judged
  only by cross-run agreement of ids/types/gaps/coverage — never by how
  many candidates it produced.
* **Missing relations never fail.**  No case-specific expected relations
  are assumed; an empty relation union is `not_applicable`.
* **Mechanism vs semantic stay separate.**  Both are reported as status
  counts per profile and per group; neither is folded into the stability
  verdict.
* **Real provider execution is not implemented.**  Live execution lives
  in the acceptance runner; this tool stays offline and reads sanitized
  summaries only (offline inputs and `prompt-run-summary.json` bridges),
  never calling an LLM, the network or a Graphiti adapter.

### Output hygiene

The report/manifest is sanitized before it leaves the process: no
absolute paths (input/output path fragments are explicitly forbidden), no
secret-like wording, no string longer than 200 characters — a violation
is a hard `sanitization` error, never a silent redaction.  Exit codes:
`0` report produced, `2` usage/input errors, `3` data or sanitization
errors.
