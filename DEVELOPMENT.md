# Development guide

This is a local-first Tumblr archiver. It saves public Tumblr source records
and a bounded neighborhood archive on the local machine.

## Run it

From a checkout, the normal Linux launcher is `Tumblr-Scraper-Linux.sh`.
Double-click `Tumblr Scraper - Linux.desktop` when your file manager permits
desktop launchers.

For terminal work:

```text
./tumblr-scraper BLOG
```

For the full option list:

```text
./tumblr-scraper --help
```

Named archive bundles are managed from source, outside `Archive/`:

```text
python3 tools/manage_archive.py list
python3 tools/manage_archive.py save working-copy
python3 tools/manage_archive.py load working-copy
python3 tools/manage_archive.py rename working-copy preserved-copy
```

`load` selects the bundle for the next process start by writing the small
`Archive/active.json` pointer. The live application can also be selected
explicitly with `TUMBLR_SCRAPER_ARCHIVE=<name>`. `save` copies data and
generated output only; it never copies the application source bundle.

The launcher/bootstrap prepares the private `.runtime/` environment. The
operating system Python is only a bootstrap tool, not an installation target.

## Run tests

From the repository root:

```text
python3 -m unittest discover -s tests -p 'test_*.py'
```

Some browser/launcher integration tests require permission to bind a local
TCP socket. A restricted execution environment may report those failures
separately; do not weaken the tests to hide that limitation.

## Where things live

| I want to change... | Look in... |
| --- | --- |
| Tumblr HTTP retrieval | `src/tumblr_scraper/network.py` |
| Raw Tumblr records becoming canonical records | `src/tumblr_scraper/normalize.py` |
| Tumblr profile HTML becoming a small observation | `src/tumblr_scraper/profile.py` |
| Observed reblog/ask relationship evidence | `src/tumblr_scraper/relationships.py` |
| Canonical source JSON storage and lookup | `src/tumblr_scraper/archive.py` |
| Shared generated-page shell, controls, and asset copying | `src/tumblr_scraper/presentation.py` |
| Named archive save/load/rename and bundle layout | `src/tumblr_scraper/archive_manager.py` |
| Crawler policy validation | `src/tumblr_scraper/config.py` and `network-policy.json` / `context-policy.json` |
| Overall crawl workflow | `src/tumblr_scraper/application.py`, then the still-coupled parts of `src/tumblr_scraper/main.py` |
| Graph data projection | `src/tumblr_scraper/graph_projection.py` |
| Graph appearance/interaction | `assets/graph-view.css` and `assets/graph-view.js` |
| Page-specific generated archive HTML | currently still in `src/tumblr_scraper/main.py`; shared presentation mechanics are in `presentation.py` |
| Feed membership and factual affinity annotations | `_feed_target_index()` and `build_feed_index()` in `src/tumblr_scraper/main.py` |
| Tests | `tests/` |
| Release/developer utilities | `tools/` |
| Deeper technical notes | `docs/` |
| Firefox integration work | `integrations/firefox/` (scaffold only) |

## Source, data, and generated files

Source and safe-to-edit project material:

```text
src/       Python application
assets/    browser CSS, JavaScript, and vendored browser libraries
tests/     automated verification
tools/     release and developer utilities
docs/      deeper technical documentation
integrations/firefox/  future Firefox launcher scaffold; not application logic
policies   Policy concepts; the current policy JSON remains at the root for
           compatibility with the existing resource resolver.
```

Mutable personal data:

```text
Archive/<name>/Content/   canonical records and locally captured media
Archive/<name>/Network/   observations and derived graph data
Archive/<name>/App/       disposable generated reader export
```

`Archive/` is a container, not the application. Multiple named bundles may
coexist there. Use the archive manager or the documented launcher selection to
load one; do not copy application code into a bundle. The old flat `Archive/`
layout is a migration input only.

Generated or local-only material:

```text
.runtime/              private Python environment and bootstrap state
release-candidates/    temporary release-gate output
*.zip                  generated release artifacts
__pycache__/           Python bytecode
.pytest_cache/         test cache
deprecated/            retained false recreations and experiments; not source
```

Do not commit personal archive data, `.runtime/`, caches, generated release
ZIPs, or local reference material. Do not hand-edit files under `Archive/`.
Presentation output is regenerated from source; preserved canonical records
inside a named bundle remain the data authority for that bundle.

## Compatibility files

The real Python implementation is under `src/tumblr_scraper/`.

The root `main.py` and `graph_projection.py` are temporary compatibility
entrypoints. They keep older imports and tools working while the refactor
continues. They must delegate inward rather than acquire a second
implementation or mutable state.

The root launchers and `bootstrap.py` are also intentionally retained because
they are the user-facing and release-facing entrypoints.

## Important boundaries

Current data flow:

```text
request/crawl decision
        -> network.py retrieves Tumblr material
        -> normalize.py creates the canonical source record
        -> archive.py durably stores source JSON
        -> relationships.py derives only explicitly observed relationship evidence
        -> main.py/application.py coordinate acquisition and optional work
        -> generated presentation is produced afterward
```

Canonical source JSON is authoritative. Neighborhood evidence is authoritative
for its observations. Generated HTML, indexes, graphs, and other presentation
output are derived and regenerable. Presentation failure must not erase a
preserved source record.

### Feed boundary

The Feed has two separate layers:

```text
source model:
    scope + POV
      -> directional observed evidence
      -> eligible source blogs
      -> canonical post universe
      -> factual annotations

browser enhancement:
    search, neighborhood, breadth, evidence, affinity band,
    include-target, and chronology over that universe
```

Do not select a global newest-N post slice before resolving affinity. A
qualifying old post must remain available even when more than N unrelated
posts are newer. The live and static adapters must consume the same source
Feed model. If windowing is later needed for very large archives, apply it
after membership selection and report universe size separately from rendered
card count.

The current implementation uses a bounded `FeedQuery -> FeedFrontier ->
FeedWindow` path. The default window is 50 cards. `FeedCursor` contains the
normalized query fingerprint, index generation, and final ordering key. A
cursor remains attached to its coherent generation while a crawl publishes
newer generations; refresh starts a new frontier instead of silently
reordering the current reading position. Query-specific total counts may be
unknown when exact counting would require a full traversal.

The crawler frontier and Feed frontier are different state machines. Feed
continuation never belongs in `Network/` scheduler state. Canonical writes and
derived-index publication must be atomic/coherent enough that the reader sees
the last complete generation while a crawl is active. Static App output uses
bounded generated Feed window files because `file://` cannot route an opaque
server cursor; those files remain disposable presentation output.

Keep these facts independent: breadth is network distance, depth is captured
history, fidelity is representation quality, and current user focus is only a
temporary priority hint. Browsing must not become a downloader or silently
change CapturePolicy, budgets, breadth limits, or request pressure. Any future
browsing priority request must enter the existing scheduler machinery.

When adding code, put it at the lowest boundary that has the needed inputs and
no broader ownership. Keep network code out of orchestration, keep archive
storage out of presentation, and leave coupled scheduling in `main.py` until a
real seam is characterized.

## Application versus host

The Python application knows how Tumblr Scraper works. Launchers and future
host integrations know how a particular environment starts or displays it.
The current terminal/CLI path does not depend on Firefox. The planned Firefox
path is only a thin local launcher: it will eventually ensure the same Python
application is running without a visible terminal and open the same GUI in a
normal Firefox tab. It is not a sidebar or a second application.
