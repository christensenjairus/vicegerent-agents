package eval

import "testing"

func TestNotionCreateAttrHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("notionCreateAttr"); !ok {
		t.Fatal("notionCreateAttr not registered; helpers_notion.go init() did not run")
	}
}

func TestNormalizeNotionID(t *testing.T) {
	cases := []struct{ in, want string }{
		{"393de885-9710-809c-9f5e-c57a91d2c81a", "393de8859710809c9f5ec57a91d2c81a"}, // pragma: allowlist secret
		{"393DE8859710809C9F5EC57A91D2C81A", "393de8859710809c9f5ec57a91d2c81a"},     // pragma: allowlist secret
		{"393de8859710809c9f5ec57a91d2c81a", "393de8859710809c9f5ec57a91d2c81a"},     // pragma: allowlist secret
		{"", ""},
	}
	for _, c := range cases {
		if got := normalizeNotionID(c.in); got != c.want {
			t.Errorf("normalizeNotionID(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestHasNonEmptyKey(t *testing.T) {
	m := map[string]any{"page_id": "x", "Empty": "", "Nested": map[string]any{"a": 1}}
	if !hasNonEmptyKey(m, "page_id") {
		t.Error("expected page_id present")
	}
	if !hasNonEmptyKey(m, "PAGE_ID") {
		t.Error("expected case-insensitive match")
	}
	if hasNonEmptyKey(m, "empty") {
		t.Error("empty string value should not count as present")
	}
	if hasNonEmptyKey(m, "missing") {
		t.Error("absent key should not count as present")
	}
}
