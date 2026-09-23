# HUMAN ACCEPTANCE PROTOCOL

The project owner is not expected to infer application behavior from source code,
test output, logs, or implementation reports.

For every meaningful user-visible or runtime behavior change, automated tests are
necessary but not sufficient.

Before declaring the work complete:

1. Run all deterministic automated and synthetic integration tests available to you.

2. Clearly distinguish:
   - what automated tests proved;
   - what synthetic end-to-end tests proved;
   - what still requires verification in the owner's real application environment.

3. Provide a HUMAN TEST CARD with:
   - what visibly changed;
   - one exact way to start the correct current build;
   - any disposable-data reset required;
   - no more than roughly 3-7 concrete human actions;
   - the exact visible result expected after important actions;
   - the durable-data result expected where applicable;
   - an explicit PASS condition;
   - an explicit FAIL condition;
   - one simple diagnostic collection procedure if it fails.

4. Never write vague directions such as:
   "verify it works"
   "check the output"
   "look at the logs"
   "confirm the new UI"
   Tell the owner exactly what they should see.

5. User-facing work is not accepted merely because source/tests contain the new
   implementation. The real browser/application must be shown to be running that
   implementation.

6. Make stale-build confusion easy to detect. The application should expose, in a
   small development/diagnostics surface:
      Git/build revision
      frontend asset/build revision
      backend/server instance ID
      active archive path
      active run ID
      resolved CapturePolicy identifier/summary
  Do this with one factual source of truth, not duplicated manually maintained
  version strings.

7. Prefer launch/regeneration workflows that automatically prevent stale generated
   assets and stale backend processes rather than asking the human to remember cache
   invalidation rituals.

8. Development archives are disposable. When a clean-state test is useful, say so
   explicitly and provide the exact reset procedure. Do not build migration machinery
   solely to preserve disposable beta test output.

9. If the owner's observed result differs from the acceptance card, treat the human
   observation as a failed acceptance test. Investigate the delivery/runtime path
   before redesigning underlying architecture.

10. A completion report for user-facing work should end with one of:
      AUTOMATED + HUMAN ACCEPTANCE PASSED
      AUTOMATED PASSED — HUMAN ACCEPTANCE REQUIRED
      FAILED — <specific failing layer>

The goal is that the project owner can remain relatively out of the implementation
details while still being able to tell, confidently and quickly, whether the actual
product has improved.
