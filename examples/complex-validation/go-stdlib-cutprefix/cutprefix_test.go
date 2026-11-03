package upstreamstrings

import "testing"

func TestCutPrefix(t *testing.T) {
	tests := []struct {
		name, input, prefix, after string
		found                      bool
	}{
		{"leading_prefix", "abc", "a", "bc", true},
		{"exact_match", "abc", "abc", "", true},
		{"empty_prefix", "abc", "", "abc", true},
		{"absent_prefix", "abc", "d", "abc", false},
		{"empty_input", "", "d", "", false},
		{"both_empty", "", "", "", true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			after, found := CutPrefix(test.input, test.prefix)
			if after != test.after || found != test.found {
				t.Errorf(
					"CutPrefix(%q, %q) = %q, %v, want %q, %v",
					test.input,
					test.prefix,
					after,
					found,
					test.after,
					test.found,
				)
			}
		})
	}
}
