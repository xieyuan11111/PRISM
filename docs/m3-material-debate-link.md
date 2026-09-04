# M3 Material Append and Debate Context Link

## Scope

This API/CLI slice links an appended material to an immutable parent debate
without creating a second fact store or automatically re-running an LLM
debate. The temporal source of truth remains PRISM's GTI/Graphiti projection
through the existing analyzer.

## Use

```console
python -m prism.cli add-material INPUT \
  --case-id CASE_ID \
  --parent-debate-run PARENT_RUN_ID
```

`--as-of` is optional with a parent run. When omitted, it uses the parent's
stored, timezone-aware cutoff. When supplied, it must represent the exact same
instant or the command fails before ingestion, pipeline processing, or report
version writing.

## Validation and result

The durable debate ledger supplies the parent run. PRISM rejects an unknown
parent or a parent from another case before any material write. After the
existing material pipeline succeeds, it deterministically recomputes the
GTI/analyzer evidence bundle at the parent cutoff; it never calls the debate
LLM to determine whether the parent became stale.

`ProcessMaterialResult.debate_link`, when a parent is supplied, records:

- `parent_run_id`, case, and cutoff;
- the parent evidence-bundle hash;
- the recomputed current hash;
- `affected` / `stale` flags.

A changed hash means the user can explicitly start a new debate or an
appropriate new follow-up. PRISM does not silently reuse the old parent and
does not automatically invoke a debate or follow-up. The parent debate remains
immutable and readable.

A successful append creates the existing immutable `material_added` report
version at the same cutoff. If the post-pipeline GTI evidence refresh fails,
no report version is created; the append result never claims a complete
linked delivery.

## Boundaries

This does not provide a live NiceGUI discussion theater, background automatic
re-debate, or a human candidate-review queue. It is a deterministic API/CLI
link between existing pipeline, GTI, debate ledger, and report-version
components.
