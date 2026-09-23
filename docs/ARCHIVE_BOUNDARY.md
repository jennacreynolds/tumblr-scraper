# Archive boundary

`Archive/` is disposable application output and user archive data. It is not
part of the application source.

The application must remain understandable, buildable, testable, and runnable
after:

    rm -rf Archive/

A fresh checkout may have no `Archive/` directory until the application creates
one. Nothing authoritative may be stored beneath it: no Python modules,
editable JavaScript or CSS, templates, builders, tests, policy, or project
documentation.

Authoritative implementation remains outside the archive:

    src/          application and presentation behavior
    assets/       browser assets
    tests/        deterministic verification
    tools/        developer and archive-management utilities
    docs/         project and archive-format documentation

Named archive output is disposable and may contain:

    Archive/<name>/Content/   canonical preserved records and local media
    Archive/<name>/Network/   observations and derived network data
    Archive/<name>/App/       generated offline reader output

Deleting `App/` must not delete canonical archive data. Deleting an entire
named archive must not affect application source. Do not recover features by
editing generated files under `Archive/`; change source and regenerate.
