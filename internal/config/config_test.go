package config

import (
	"testing"
	"time"
)

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

// Dur backs every timeout and heartbeat in the project, and the defaults it
// falls back to are what a typo silently keeps: a window of "1h30m" is valid, and
// "1hr" must not become zero.
func TestDurParsesDurationsAndFallsBackOnNonsense(t *testing.T) {
	for _, tc := range []struct {
		value string
		def   time.Duration
		want  time.Duration
	}{
		{"", 20 * time.Second, 20 * time.Second},
		{"90s", time.Minute, 90 * time.Second},
		{"1h30m", time.Minute, 90 * time.Minute},
		{" 250ms ", time.Second, 250 * time.Millisecond},
		{"0", time.Minute, 0},
		{"1hr", time.Minute, time.Minute},
		{"soon", time.Minute, time.Minute},
		// A parser reports what the text says. A duration that cannot be
		// meaningful for its setting is refused where it is used, by name —
		// see host.New's heartbeat check — because an env var and a flag
		// arrive by different routes and only one of them goes through here.
		{"-5s", time.Minute, -5 * time.Second},
	} {
		t.Setenv("BOTHY_TEST_DUR", tc.value)
		if got := Dur("BOTHY_TEST_DUR", tc.def); got != tc.want {
			t.Errorf("Dur(%q, %s) = %s, want %s", tc.value, tc.def, got, tc.want)
		}
	}
}

func TestIntFallsBackWhenUnset(t *testing.T) {
	t.Setenv("BOTHY_TEST_INT_UNSET", "")
	if got := Int("BOTHY_TEST_INT_UNSET", 4); got != 4 {
		t.Errorf("Int on an unset variable = %d, want the default 4", got)
	}
}

func TestListSplitsAndDropsEmpties(t *testing.T) {
	t.Setenv("BOTHY_TEST_LIST", " a , b ,, c ")
	got := List("BOTHY_TEST_LIST")
	if len(got) != 3 || got[0] != "a" || got[2] != "c" {
		t.Errorf("List = %#v, want a, b, c", got)
	}
}
