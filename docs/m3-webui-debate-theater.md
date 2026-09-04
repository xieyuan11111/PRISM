# M3 NiceGUI Debate Theater v0

PRISM's optional WebUI now exposes `/debate` as a thin presentation layer over
`PrismAPI.debate_case()` and `PrismAPI.follow_up_debate()`.

The page supports selecting a case and timezone-aware historical cutoff,
entering a question, choosing perspective ids, running one automatic debate,
viewing typed and evidence-bound statements, cross-examination, synthesis,
errors and warnings, and asking a named perspective a follow-up using an
existing parent run id.

The page does not access GTI, SQLite, or the LLM router directly. It does not
add streaming, pause/continue controls, authentication, or an automatic
re-debate after new evidence. Those remain later product work.
