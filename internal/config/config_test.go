package config

import "testing"

func TestStrFallsBackWhenUnsetOrBlank(t *testing.T) {
	t.Setenv("BOTHY_TEST_STR", "")
	if got := Str("BOTHY_TEST_STR", "fallback"); got != "fallback" {
		t.Errorf("Str on an empty value = %q, want the fallback", got)
	}
	t.Setenv("BOTHY_TEST_STR", "  value  ")
	if got := Str("BOTHY_TEST_STR", "fallback"); got != "value" {
		t.Errorf("Str = %q, want it trimmed", got)
	}
}

func TestIntFallsBackOnNonsense(t *testing.T) {
	t.Setenv("BOTHY_TEST_INT", "not a number")
	if got := Int("BOTHY_TEST_INT", 7); got != 7 {
		t.Errorf("Int on nonsense = %d, want the default", got)
	}
	t.Setenv("BOTHY_TEST_INT", "12")
	if got := Int("BOTHY_TEST_INT", 7); got != 12 {
		t.Errorf("Int = %d, want 12", got)
	}
}

// These defaults end up in compose files and shell exports, where yes/no and
// on/off are what people write. A typo must fall back rather than quietly flip a
// setting off, which is the failure that matters: silently disabling a limit.
func TestBoolAcceptsTheSpellingsPeopleUse(t *testing.T) {
	for _, tc := range []struct {
		value string
		def   bool
		want  bool
	}{
		{"", true, true},
		{"", false, false},
		{"true", false, true},
		{"TRUE", false, true},
		{"1", false, true},
		{"yes", false, true},
		{"on", false, true},
		{"false", true, false},
		{"0", true, false},
		{"no", true, false},
		{"off", true, false},
		{"nonsense", true, true},
		{"nonsense", false, false},
	} {
		t.Setenv("BOTHY_TEST_BOOL", tc.value)
		if got := Bool("BOTHY_TEST_BOOL", tc.def); got != tc.want {
			t.Errorf("Bool(%q, %v) = %v, want %v", tc.value, tc.def, got, tc.want)
		}
	}
}

func TestListSplitsAndDropsEmpties(t *testing.T) {
	t.Setenv("BOTHY_TEST_LIST", " a , b ,, c ")
	got := List("BOTHY_TEST_LIST")
	if len(got) != 3 || got[0] != "a" || got[2] != "c" {
		t.Errorf("List = %#v, want a, b, c", got)
	}
}
