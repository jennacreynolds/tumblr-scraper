# Third-party provenance

This project keeps the generated archive and personal crawl output outside source control.
The files under `assets/` are project-owned presentation source unless a file says otherwise.

## tumblr-backup / tumblr-utils

- Used by the existing crawler and archive generation path.
- The project reuses its generated post HTML as derived presentation material.
- Canonical source JSON remains the archival authority.
- Existing upstream licensing and attribution requirements remain applicable to the installed package.

## AprilSylph/Vision

- Repository: https://github.com/AprilSylph/Vision
- Author: April Sylph
- Use: visual/reference audit for feed width, card geometry, author rows, captions, media, and responsive layout.
- Status: reference only; no Vision CSS or theme code is copied into this project.
- License: GPL-3.0 as identified by the repository; no derived Vision code is distributed here.

## glenthemes/npf-images-v4

- Repository: https://github.com/glenthemes/npf-images-v4
- Author: glenthemes
- Use: reference audit for NPF image sizing, photosets, captions, and overflow handling.
- Status: reference only; its live Tumblr-theme JavaScript is not bundled.

## sindresorhus/modern-normalize

- Repository: https://github.com/sindresorhus/modern-normalize
- Status: not currently vendored; the project stylesheet contains its own small baseline.
- If vendored later, include the exact source version and license notice here.

## Phosphor Icons

- Repository: https://github.com/phosphor-icons/web
- License: MIT.
- Status: no Phosphor SVGs are currently bundled.
- If selected icons are added later, preserve the MIT notice and list the exact SVGs here.

## Cytoscape.js

- Repository: https://github.com/cytoscape/cytoscape.js
- Version: 3.34.3
- License: MIT.
- Use: local offline Graph View rendering, interaction, and layouts.
- Bundled file: `assets/vendor/cytoscape.min.js`
- SHA-256: `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91`

The untracked `Graph/` directory is not part of the release allowlist. Its
provenance and license are unresolved, so Graph View does not depend on it.

## Cytoscape fCoSE layout stack

- `cytoscape-fcose` 2.2.0, MIT, https://github.com/iVis-at-Bilkent/cytoscape.js-fcose
  - Bundled file: `assets/vendor/cytoscape-fcose.min.js`
  - SHA-256: `4b1cab218d74996aa59cd8473f9239cc6398b8c1774d84d7e59ad9a68959cb57`
- `cose-base` 2.2.0, MIT, https://github.com/iVis-at-Bilkent/cose-base
  - Bundled file: `assets/vendor/cose-base.min.js`
  - SHA-256: `7cae9509bd36235a63a85edc8d9fa2cd0bc1d0c1ecc5b5a737976f39d040ddf`
- `layout-base` 2.0.1, MIT, https://github.com/iVis-at-Bilkent/layout-base
  - Bundled file: `assets/vendor/layout-base.min.js`
  - SHA-256: `ec15ab5df9af3f20708f4faab994accf91cda71848cd5bb10a23432cc50b6745`
- `cytoscape-layout-utilities` 1.1.1, MIT, https://github.com/iVis-at-Bilkent/cytoscape.js-layout-utilities
  - Bundled file: `assets/vendor/cytoscape-layout-utilities.min.js`
  - SHA-256: `b7e0db7c6631e57cf821e855e6dd8b37f1a4cfb894551681b324c03c928e86f3`

These browser distributions are vendored in dependency order for offline use;
no build step or CDN is required at archive-read time.
