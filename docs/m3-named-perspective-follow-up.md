# M3 Named Perspective Follow-up

## Scope

PRISM can ask one named perspective a follow-up question over an existing,
immutable debate run. The follow-up re-reads the parent's case and `as_of`
through the configured analyzer/GTI path, rebuilds the evidence bundle, and
refuses to continue if its hash differs from the parent run. It therefore does
not create a second fact store or allow a follow-up to silently read a newer
historical state.

## API and CLI

The facade exposes:

```python
await api.follow_up_debate(
    parent_run_id="...",
    question="Why did implementation begin at this point?",
    perspective="institutional_regulatory",
)
```

The CLI exposes the same operation:

```console
python -m prism.cli follow-up PARENT_RUN_ID \
  --perspective institutional_regulatory \
  --question "Why did implementation begin at this point?"
```

Only one configured perspective is accepted. The model receives the same
historical cutoff and evidence bundle as the parent debate. Its structured
statements use the existing fact/interpretation/value-judgment/prediction/
unresolved classifications and the existing evidence-id validation.

## Persistence and idempotency

Follow-ups are stored in the additive SQLite `debate_followup_audit` table.
Each row records the follow-up id, parent run id, case, question, cutoff,
perspective, evidence-bundle hash and serialized structured result. The input
hash includes all of those identity fields, so repeating the same request
returns the saved result without another LLM call. Existing `debate_audit`
rows remain readable and the parent result is never modified.

Malformed provider output, unknown citations, missing providers and changed
parent evidence fail safely with an auditable failure result or explicit
validation error; no unsupported conclusion is fabricated.

## Boundaries

This is an API/CLI vertical slice. It does not yet provide the NiceGUI debate
theater, live pause/continue controls, or a streaming conversation UI.
