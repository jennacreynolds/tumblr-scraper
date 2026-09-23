# Temporary Firefox integration (Linux)

This is a small Firefox launcher for the existing Tumblr Scraper application.
It does not inspect Tumblr tabs, request Tumblr permissions, crawl, archive, or
provide another GUI.

The intended flow is:

```text
Firefox extension
        -> ensure the existing Tumblr Scraper backend is running headlessly
        -> open the existing local Tumblr Scraper GUI
        -> show it in a normal Firefox tab
```

This is explicitly not a sidebar, not a second GUI, and not a JavaScript port
of the application. The terminal and current launchers remain completely
independent and continue to start the same Python application.

## One-time setup

From the repository root:

```text
python3 integrations/firefox/native-host/install-linux.py install
```

In Firefox, open `about:debugging`, select `This Firefox`, choose `Load
Temporary Add-on...`, and select `integrations/firefox/extension/manifest.json`.
Pin `Tumblr Scraper` to the toolbar.

Clicking the toolbar button starts or reuses the existing local application and
opens its normal GUI tab. A later click focuses that tab instead of opening a
duplicate.

The Firefox-started backend is Firefox-owned and is stopped when Firefox exits.
A backend started by the terminal or desktop launcher is external-owned and is
not stopped when Firefox exits. The GUI exposes a server-stop button only for a
Firefox-owned session.

Remove the temporary host registration with:

```text
python3 integrations/firefox/native-host/install-linux.py uninstall
```

The extension is temporary and must be loaded again after Firefox restarts.

## Responsibility boundary

Firefox integration may detect local reachability, ask a small local helper to
start the application, learn its local URL, open/focus that URL, and later add
browser-specific convenience actions.

It may not own crawler scheduling, Tumblr retrieval, normalization, archive
paths or formats, source preservation, neighborhood logic, Graph View data
semantics, policy interpretation, profile parsing, or presentation generation.
Those remain application ground truth in `src/tumblr_scraper/`.

The extension and native-host folders contain only the browser adapter,
Native Messaging framing, and Firefox-session supervision. The application
core remains authoritative.

The temporary implementation uses Mozilla Native Messaging with the explicit
host name `tumblr_scraper_firefox` and extension ID
`tumblr-scraper-firefox@local.invalid`.
