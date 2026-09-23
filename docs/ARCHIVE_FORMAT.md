# Named archive format

Each named archive is an independent bundle:

    Archive/<name>/
    +-- archive.json
    +-- Content/
    +-- Network/
    `-- App/

`Content/` is the canonical preserved Tumblr material and local media.
`Network/` contains observations, relationships, neighborhoods, and derived
network state. `App/` is generated presentation output and can always be
deleted and regenerated from source plus the selected archive data.

`Archive/active.json` is a small selection pointer used by archive management;
it is not application source or canonical archive content.

Archive names are filesystem-facing identifiers and are validated by the
source-side archive manager. Save, load, rename, and selection operations must
remain outside the generated bundle.

The archive reader is derived from shared source presentation code. Live and
static readers must not become separate feature implementations.
