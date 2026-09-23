# Tumblr Scraper application package

This package is the real Python implementation. Root-level Python files are
compatibility entrypoints; do not add a second implementation there.

## File map

- `paths.py`: bundle, resource, and selected named-archive locations.
- `archive_manager.py`: portable named archive save/load/rename operations;
  no UI or crawler policy lives here.
- `models.py`: shared request/state models and domain errors.
- `config.py`: policy parsing and validation. Policy file locations remain
  owned by the compatibility surface in `main.py`.
- `network.py`: public Tumblr retrieval and bounded retry behavior.
- `normalize.py`: pure conversion from raw Tumblr records to canonical source
  records.
- `profile.py`: pure interpretation of title and public description metadata
  from Tumblr profile HTML.
- `relationships.py`: deterministic extraction of explicitly observed reblog
  and structured-ask evidence from canonical records.
- `archive.py`: canonical source JSON path, read, existence, and atomic-write
  mechanics.
- `presentation.py`: shared generated-archive shell, controls, asset copying,
  and presentation-state mechanics. Page-specific rendering remains in
  `main.py` until its runtime dependencies are separable.
- `application.py`: host-neutral crawl lifecycle coordination.
- `graph_projection.py`: derived offline Graph View data.
- `main.py`: compatibility and orchestration surface; still contains the
  coupled acquisition, neighborhood, rendering, enrichment, and CLI behavior.

## Current flow

```text
request/crawl decision
        -> network.py
           fetch Tumblr data
        -> normalize.py
           turn Tumblr data into a canonical record
        -> profile.py
           interpret optional profile metadata without persistence
        -> archive.py
           durably save/read canonical source
        -> relationships.py
           derive only explicit relationship evidence for graph/neighborhood consumers
        -> main.py/application.py
           coordinate remaining crawl and optional work
        -> generated presentation
```

Canonical source JSON is authoritative within a selected named archive bundle.
Generated HTML is derived and disposable. The application source remains
outside `Archive/` and can serve the live interface without a generated
`App/` export.
Presentation failure must not erase preserved source.

## Where new code belongs

Use the lowest module that can do the job with explicit inputs:

- transport/retry mechanics: `network.py`;
- raw-record conversion: `normalize.py`;
- canonical source storage: `archive.py`;
- observed relationship evidence: `relationships.py`;
- policy validation: `config.py`;
- lifecycle or scheduling: currently `main.py`/`application.py`;
- graph projection: `graph_projection.py`.

Do not create a general `common.py`, service container, or future bot module.
If a responsibility still needs active crawler state, presentation callbacks,
or scheduling decisions, leave it in `main.py` until its ownership is clear.

Firefox-specific work belongs in `integrations/firefox/`. That scaffold is not
the application and is not imported by this package. The future extension and
native/local host must reuse the same Python application and local GUI; they
must not duplicate crawler, archive, normalization, policy, or presentation
logic.
