package eval

import (
	"reflect"
	"sort"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestAwsSecretReadHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("awsSecretReadAttr"); !ok {
		t.Fatal("awsSecretReadAttr not registered; helpers_aws.go init() did not run")
	}
}

// TestAwsSecretReadAttrEval drives awsSecretReadAttr end-to-end through
// Compile/Eval, covering the AWS CLI grammar parse: plain commands, interleaved
// global options (value-taking + boolean + = form), quoted option values, the
// batch []string cli_command, and the fail-open no-command case.
func TestAwsSecretReadAttrEval(t *testing.T) {
	m := &config.Mapping{Backends: map[string]config.Backend{
		"vmcp": {
			Helpers: []string{"awsSecretReadAttr"},
			Tools: map[string]config.Tool{
				"aws_call_aws": {
					ResourceType: "aws_command",
					ID:           "'call_aws'",
					AttrFrom:     "awsSecretReadAttr(args)",
				},
			},
		},
	}}
	eng, err := Compile(m)
	if err != nil {
		t.Fatalf("Compile: %v", err)
	}

	cases := []struct {
		name string
		args map[string]any
		want []string // nil means no "awsOps" key at all
	}{
		{
			name: "plain get-secret-value",
			args: map[string]any{"cli_command": "aws secretsmanager get-secret-value --secret-id foo"},
			want: []string{"secretsmanager/get-secret-value"},
		},
		{
			name: "batch-get-secret-value",
			args: map[string]any{"cli_command": "aws secretsmanager batch-get-secret-value --secret-id-list a b"},
			want: []string{"secretsmanager/batch-get-secret-value"},
		},
		{
			name: "interleaved value-taking + boolean global options",
			args: map[string]any{"cli_command": "aws --region us-west-2 --profile prod --debug secretsmanager get-secret-value"},
			want: []string{"secretsmanager/get-secret-value"},
		},
		{
			name: "equals-form global option",
			args: map[string]any{"cli_command": "aws --output=json secretsmanager get-secret-value"},
			want: []string{"secretsmanager/get-secret-value"},
		},
		{
			name: "quoted profile value not mistaken for the service",
			args: map[string]any{"cli_command": `aws --profile "my profile" secretsmanager get-secret-value`},
			want: []string{"secretsmanager/get-secret-value"},
		},
		{
			name: "leading aws omitted still parses",
			args: map[string]any{"cli_command": "secretsmanager get-secret-value"},
			want: []string{"secretsmanager/get-secret-value"},
		},
		{
			name: "batch cli_command as []string",
			args: map[string]any{"cli_command": []any{
				"aws s3api list-buckets",
				"aws secretsmanager get-secret-value --secret-id x",
			}},
			want: []string{"s3api/list-buckets", "secretsmanager/get-secret-value"},
		},
		{
			name: "metadata read is surfaced but not a secret-value op",
			args: map[string]any{"cli_command": "aws secretsmanager describe-secret --secret-id foo"},
			want: []string{"secretsmanager/describe-secret"},
		},
		{
			name: "non-aws command with only one positional yields no ops",
			args: map[string]any{"cli_command": "aws help"},
			want: nil,
		},
		{
			name: "no cli_command falls through (no awsOps key)",
			args: map[string]any{"other": "x"},
			want: nil,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := eng.Eval(CallInput{Tool: "aws_call_aws", Backend: "vmcp", Args: c.args})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			v, ok := res.Attr["awsOps"]
			if c.want == nil {
				if ok {
					t.Fatalf("expected no awsOps key, got %v", v)
				}
				return
			}
			if !ok {
				t.Fatalf("expected awsOps key, got none; attr=%v", res.Attr)
			}
			got, ok := v.([]string)
			if !ok {
				t.Fatalf("expected []string, got %T (%v)", v, v)
			}
			sort.Strings(got)
			want := append([]string(nil), c.want...)
			sort.Strings(want)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("awsOps = %v, want %v", got, want)
			}
		})
	}
}
