# Tumblr-Scraper architecture

This document records the current application boundary. It is intentionally
small: the project has one application core, one browser interface, and thin
hosts around them.

## Before this boundary

The launcher mixed startup, terminal prompts, browser launching, and crawler
execution. The archive was primarily a static reader, while live controls
were not attached to an explicit application lifecycle. That made it easy for
each host to become a second entry point or a second source of state.

## Current shape

```text
Python application core (main.py)
    - crawl request validation and policy
    - archive persistence and presentation generation
    - job lifecycle, progress, and runtime controls
    - configuration and provenance
             ^
             | JSON-compatible callbacks over loopback HTTP
             v
Canonical HTML/CSS/JS interface (Backups + assets/)
             ^
             | browser/WebView hosting and filesystem access
             v
Thin launchers and hosts
    - Tumblr Scraper - Android.py
    - desktop launchers
    - future hosts
```

`main.py` is the product authority. `bridge/local_http.py` is transport and
static-file hosting only. The launcher owns startup and host lifecycle; it
does not implement crawler or archive behavior.

## Future request-channel boundary

The application controller accepts neutral crawl requests. It must not assume
that a request came from the browser, CLI, Pydroid, Tumblr messaging, Discord,
or another community adapter. Those hosts are responsible for translating
their environment-specific input into the ordinary application call and for
consuming the neutral status/result.

Request channel and acquisition identity are separate:

```text
request channel != Tumblr acquisition identity
```

The current application does not implement messaging, bots, queues, or
authenticated acquisition. This boundary is documented so future adapters do
not duplicate crawler logic or put platform-specific concepts into crawler,
archive, network, or acquisition models.

## Entry point

Launching a supported wrapper starts an idle Python application, prepares the
global archive entry page, starts a loopback bridge, and opens the canonical
archive in the normal browser. The user opens the collapsed `Crawler` panel
and starts a crawl there. Terminal input is only a fallback (`T`) for hosts
where browser interaction is unavailable.

The static archive remains usable after the Python process exits. A live
browser session requires the local bridge and its owning application process.

## Bridge contract

The bridge uses ordinary JSON-compatible objects and a per-process capability
token. Mutating requests require the token; arbitrary Python execution is not
exposed.

| Method and path | Purpose |
| --- | --- |
| `GET /__crawler/bootstrap` | Discover live mode, capability, and initial state |
| `GET /__crawler/status` | Read the structured application snapshot |
| `POST /__crawler/start` | Submit a validated crawl request |
| `POST /__crawler/control` | Change one permitted runtime control |
| `POST /__crawler/stop` | Request safe cancellation and finalization |

The interface does not parse terminal output or logs. It renders the
structured snapshot returned by the core. The core remains authoritative for
configuration, job state, progress, persistence, and generated archive views.

## Source of truth

Canonical stored archive records and Python application state are authoritative.
HTML, DOM values, logs, generated pages, caches, indexes, and wrapper state are
projections or transport state only. A new host should be able to replace the
current launcher without changing crawler, archive, or interface behavior.

Persisted archive paths remain relative to the archive root. Platform paths,
browser opening, permission behavior, and lifecycle handling stay at the host
edge.

## Compatibility and unresolved coupling

- The historical terminal prompt helpers remain for the `T` fallback and
  compatibility; normal startup no longer enters through them.
- The existing static archive generator remains in `main.py` because it is
  part of canonical presentation generation, not wrapper behavior.
- The current Android-named launcher is still the cross-platform Python host;
  it does not require Android APIs. A future rename or split is intentionally
  deferred to avoid a gratuitous packaging change.
- A live crawl still depends on the owning Python process staying alive. This
  is a lifecycle constraint, not a second source of truth.

## Mutable-state inventory

The relocated core still has a deliberately small legacy mutable boundary.
This is an inventory for staged extraction, not a new context container:

| Category | Current owner | Migration status |
| --- | --- | --- |
| Application/resource/data locations | `tumblr_scraper.paths` | centralized |
| Crawl request and run models | `tumblr_scraper.models` | explicit objects |
| Active blog compatibility state (`BLOG`, `OUT`, `JSON_DIR`) | core module | legacy adapter; preserve until acquisition extraction |
| Active run/status (`ACTIVE_RUNTIME`, `ACTIVE_STATUS`) | core module | process-wide lifecycle state; consumed by `application.py` |
| Cancellation and lifecycle coordination | core module | process-wide event/lock; minimize during extraction |
| Host capability flags and verbosity | core module | host compatibility state; migration target |
| Network/configuration constants | core module | immutable or configuration candidates |
| Test reassignment of archive paths | tests | compatibility seam; shrink with migration |

The compatibility `main.py` facade aliases the package module rather than
loading a second implementation. `import main` and
`import tumblr_scraper.main` therefore share one module dictionary and one
set of mutable variables.

## Change record

- Original behavior: launcher asked terminal whether to open an archive or
  start a crawl, with browser opening as a side effect.
- New behavior: launcher opens the canonical browser interface first; the
  interface submits crawl actions through the narrow loopback bridge.
- Preserved behavior: the crawler, archive schema, static reader, terminal
  fallback, and existing tests remain in place.
- Deleted duplication: no separate HTML crawler implementation or wrapper
  crawler state was introduced.
