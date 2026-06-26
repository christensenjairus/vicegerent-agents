package eval

import (
	"strings"
	"testing"

	"github.com/google/cel-go/cel"
)

// The k8s helper file's init() must self-register canonicalK8s, with no edit to
// the generic core. This guards the "drop a helpers_<backend>.go" contract.
func TestK8sHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("canonicalK8s"); !ok {
		t.Fatal("canonicalK8s not registered; helpers_k8s.go init() did not run")
	}
}

// An unknown helper named in a mapping is fatal at env build (fail closed).
func TestUnknownHelperFailsClosed(t *testing.T) {
	if _, err := newEnv([]string{"definitelyNotAHelper"}); err == nil {
		t.Fatal("expected error for unknown helper, got nil")
	}
}

// Duplicate registration is a programming error and must panic at startup.
func TestRegisterHelperDuplicatePanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on duplicate helper registration")
		}
	}()
	registerHelper("canonicalK8s", func() []cel.EnvOption { return nil })
}

// A backend that does not list canonicalK8s must not have it in scope.
func TestHelperNotInScopeWithoutOptIn(t *testing.T) {
	env, err := newEnv(nil)
	if err != nil {
		t.Fatalf("newEnv: %v", err)
	}
	_, iss := env.Compile(`canonicalK8s(args)`)
	if iss == nil || iss.Err() == nil {
		t.Fatal("canonicalK8s compiled without opt-in; helper leaked into core")
	}
	if !strings.Contains(iss.Err().Error(), "canonicalK8s") {
		t.Fatalf("unexpected compile error: %v", iss.Err())
	}
}
