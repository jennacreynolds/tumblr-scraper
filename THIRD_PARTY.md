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
