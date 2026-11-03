# Upstream provenance

- Repository: https://github.com/golang/go
- Tag: `go1.21.2`
- Commit: `26b5783b72376acd0386f78295e678b9a6bff30e`
- Source: `src/strings/strings.go`, function `CutPrefix`
- Tests: `src/strings/strings_test.go`, table `cutPrefixTests`
- License: BSD-3-Clause (the upstream `LICENSE` is retained beside this file)

This offline fixture extracts `strings.CutPrefix` and the six upstream table
cases. Because the function no longer resides inside Go's `strings` package,
the extraction calls `strings.HasPrefix` explicitly. Package/module names were
changed only to avoid shadowing the standard library.

For repair evaluation, one defect is injected before the upstream logic:
a non-empty string exactly equal to its prefix is incorrectly reported as not
found. All other behavior remains the pinned upstream contract. Verification
does not clone the repository or access the network, and the original Go
repository is never modified.
