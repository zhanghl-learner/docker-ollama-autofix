package calculator

import "testing"

func TestAdd(t *testing.T) {
	cases := []struct {
		left, right, expected int
	}{
		{2, 3, 5},
		{10, 4, 14},
	}
	for _, item := range cases {
		if actual := Add(item.left, item.right); actual != item.expected {
			t.Fatalf("Add(%d, %d) = %d; want %d", item.left, item.right, actual, item.expected)
		}
	}
}
