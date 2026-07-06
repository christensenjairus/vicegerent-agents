package eval

import "testing"

func TestNotionHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("notionAttr"); !ok {
		t.Fatal("notionAttr not registered; helpers_notion.go init() did not run")
	}
}

func TestAnyMapBool(t *testing.T) {
	cases := []struct {
		name string
		m    map[string]any
		key  string
		want bool
	}{
		{"real bool true", map[string]any{"allow_deleting_content": true}, "allow_deleting_content", true},
		{"real bool false", map[string]any{"allow_deleting_content": false}, "allow_deleting_content", false},
		{"string true", map[string]any{"allow_deleting_content": "true"}, "allow_deleting_content", true},
		{"string false", map[string]any{"allow_deleting_content": "false"}, "allow_deleting_content", false},
		{"absent key", map[string]any{}, "allow_deleting_content", false},
		{"wrong type (number)", map[string]any{"allow_deleting_content": 1}, "allow_deleting_content", false},
		{"case-insensitive key", map[string]any{"Allow_Deleting_Content": true}, "allow_deleting_content", true},
		{"unparseable string", map[string]any{"allow_deleting_content": "yes"}, "allow_deleting_content", false},
	}
	for _, c := range cases {
		got := anyMapBool(c.m, c.key)
		if got != c.want {
			t.Errorf("%s: anyMapBool(%v, %q) = %v, want %v", c.name, c.m, c.key, got, c.want)
		}
	}
}
