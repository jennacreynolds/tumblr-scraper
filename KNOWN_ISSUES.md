# Known deferred work

This is a stabilization note, not a release claim.

## Dependency and distribution

This beta does not ship a `wheels/` directory. If the pinned dependencies are
not already installed, the launcher performs an online pip bootstrap. It is
therefore not an offline installer. A future release may add verified bundled
wheels and matching third-party notices.

## Core reentrancy

The crawler still uses module-level mutable state. The supported model is one
active crawl per process. Concurrent crawler instances are not supported.
Future modularization should wait until integration behavior is protected.

## Entry-point consolidation

Multiple compatibility launch paths remain. The eventual hierarchy is:

```text
crawler core
    ^
application controller
  ^             ^
browser        CLI
    ^
platform launchers
```

Paths should not be removed until current usage and tests establish what is
safe.

## main.py size

`main.py` is large. Splitting it merely for aesthetics is deferred. Future
extraction should follow stable behavioral boundaries protected by integration
tests.

## Packaging and release

No release should be called portable or offline until it has been tested from
a genuinely clean environment. Rebuilding the distribution ZIP is deferred
from this stabilization pass.
