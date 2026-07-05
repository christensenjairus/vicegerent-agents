package eval

import "testing"

func TestAlertmanagerHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("alertmanagerAttr"); !ok {
		t.Fatal("alertmanagerAttr not registered; helpers_alertmanager.go init() did not run")
	}
}

func TestParseAlertmanagerDuration(t *testing.T) {
	cases := []struct {
		in   string
		want int64
	}{
		{"30m", 1800},
		{"2h", 7200},
		{"1d", 86400},
		{"45s", 45},
		{"", 7200}, // handled by caller's default via lookupCI, not this func; empty here is malformed
		{"garbage", 1 << 40},
		{"5x", 1 << 40},
		{"-5m", 1 << 40},
	}
	for _, c := range cases {
		got := parseAlertmanagerDuration(c.in)
		if c.in == "" {
			// empty string has no unit suffix; treated as malformed, not 2h default
			// (the 2h default is applied by alertmanagerAttrOption via lookupCI's
			// default arg, not by this parser).
			if got != 1<<40 {
				t.Errorf("parseAlertmanagerDuration(%q) = %d, want sentinel", c.in, got)
			}
			continue
		}
		if got != c.want {
			t.Errorf("parseAlertmanagerDuration(%q) = %d, want %d", c.in, got, c.want)
		}
	}
}
