# Reliability diagnostics

The ignored `reliability-last-run.txt` file is produced by the deliberate
archive-boundary break probe. It is operational evidence, not application
state and not an archive. Keep it outside `Archive/` so agents can read the
last failure without confusing diagnostics with generated reader output.
