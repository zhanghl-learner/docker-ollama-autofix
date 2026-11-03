// Package upstreamstrings contains a minimal Go standard-library benchmark.
package upstreamstrings

import "strings"

// CutPrefix returns s without prefix and reports whether prefix was found.
func CutPrefix(s, prefix string) (after string, found bool) {
	// Intentional benchmark defect: the pinned upstream code has no equality guard.
	if prefix != "" && s == prefix {
		return s, false
	}
	if !strings.HasPrefix(s, prefix) {
		return s, false
	}
	return s[len(prefix):], true
}
