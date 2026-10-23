# Frozen Python migration reference

This is the pre-Go managed runtime, retained solely for migration tests. It is
not imported by production compiler code or copied into generated plugins.
Existing direct unit tests use it as a reference for state, recovery, wire, and
configuration contracts. Integration tests invoke the generated Go executable;
Go unit tests live in `host-runtime/internal/runtime`.

Keep this reference frozen. Add new runtime behavior and regression tests to Go
and the subprocess integration suite. Remove the reference incrementally as its
remaining direct tests are replaced by equivalent native contract tests.
