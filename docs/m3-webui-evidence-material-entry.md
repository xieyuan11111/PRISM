# M3 NiceGUI Evidence Browser and Material Entry v0

PRISM's optional WebUI now exposes `/evidence` for paginated evidence search
and `/materials` for explicitly appending a local Markdown or PDF path to a
caller-selected case.

The evidence page delegates filters and pagination to `PrismAPI.search()` and
keeps source ids, corpus/raw paths, publication time, URL, retrieval metadata
and available locator fields. The material page delegates to
`PrismAPI.add_material()` and reports pipeline status, the immutable report
version, and parent-debate evidence hashes/staleness when a parent run is
provided.

The UI never guesses a case, writes corpus files directly, calls an LLM
itself, performs remote upload, or exposes credentials. Multi-user auth,
remote binding, richer corpus browsing and background progress streaming
remain later work.
