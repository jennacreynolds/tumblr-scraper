# Release candidate gate

This procedure validates one commit and one immutable ZIP. It does not rebuild
the package separately for each platform.

## 1. Build the candidate

From the exact release-candidate commit:

    python3 tools/build_release_artifact.py \
      --output-dir release-candidates \
      --artifact-name Tumblr-Scraper-Early-Beta-vX.Y.Z.zip

The command writes the ZIP and `release-manifest.json`. Record:

    RC_COMMIT
    RC_ZIP
    RC_ZIP_SHA256

Verify it:

    python3 tools/verify_release_artifact.py \
      --zip release-candidates/Tumblr-Scraper-Early-Beta-vX.Y.Z.zip \
      --manifest release-candidates/release-manifest.json \
      --commit "$RC_COMMIT"

## 2. Local Linux gate

Run the exact candidate ZIP, including clean bootstrap and extracted-path
tests:

    python3 tools/run_linux_release_gate.py \
      --zip release-candidates/Tumblr-Scraper-Early-Beta-vX.Y.Z.zip \
      --manifest release-candidates/release-manifest.json

Use `--skip-browser` only on a headless machine. The hard checks still run;
loopback/browser-wrapper evidence is then recorded as skipped.

## 3. Android/Pydroid gate

The APK is external test input. Never commit or upload it. The gate records its
SHA-256 and separates core runtime evidence from UI evidence:

    python3 tools/run_android_pydroid_gate.py \
      --zip release-candidates/Tumblr-Scraper-Early-Beta-vX.Y.Z.zip \
      --manifest release-candidates/release-manifest.json \
      --apk /secure/path/Pydroid3.apk \
      --sdk-root "$ANDROID_SDK_ROOT" \
      --avd Moto_G53_Reference \
      --evidence-dir release-candidates/android-evidence

The device path is under shared `Download/` storage and deliberately contains
spaces. The probe verifies that the installed Pydroid build can read and write
that path under Android 13 scoped-storage rules.

Android core is mandatory: extraction, Pydroid execution, imports, dependency
bootstrap, policy/assets resolution, and localhost bridge startup. Browser/UI
handoff is reported separately and may be `PASS`, `FAIL`, or
`SKIPPED-FLAKY`.

## 4. Native CI gate

Push the release-candidate branch only. GitHub Actions builds the canonical ZIP
once, uploads it, and makes Linux, Windows, and macOS jobs download that exact
ZIP. The jobs verify its commit and SHA-256 before testing.

The matrix tests observable launcher behavior from spaced paths and unrelated
working directories. It does not require shell-source implementation details.
`gio launch` is best-effort desktop-session evidence, not a hosted-CI hard gate.

## 5. Publish

Only after local Android evidence and the native CI matrix pass:

1. Fast-forward or merge the exact tested commit to `main`.
2. Confirm the final tag points to `RC_COMMIT`.
3. Publish the exact `RC_ZIP` whose digest is `RC_ZIP_SHA256`.
4. Do not edit source, regenerate the ZIP, or alter release contents afterward.

Any source or artifact change invalidates the gate and requires a new candidate.
