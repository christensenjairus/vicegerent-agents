// Package promptinjection provides two-stage detection of prompt-injection
// shapes in free text returned by external tool-read results (a scraped
// webpage via Firecrawl/Tavily, a fetched Notion/Jira/Confluence page body, a
// GitHub file, a GitLab merge-request diff, ...). Untrusted content flowing
// INTO the agent's context from a tool RESULT is a different risk than
// HAH-106's outbound content-moderation gate, which checks free-text
// arguments of WRITE calls flowing OUT to Notion/Linear/GitHub/etc. before
// they reach Cerbos -- this package has no relationship to that one beyond
// sharing the "registry of regexes run over free text" shape (see
// secrets_redact.go's secretPatternRegistry, this package's closest
// structural sibling in this codebase) for its first stage.
//
// Stage 1 (this file's InjectionPatternRegistry + RegexDetector) is
// deliberately broad/high-recall: it is EXPECTED to over-match legitimate
// content (a security blog post discussing "ignore previous instructions"
// attacks, documentation that quotes a jailbreak prompt as an example,
// ...). That's fine by design -- it's cheap, runs on every eligible
// response, and exists purely to cut the volume of text that needs the
// expensive stage 2 (judge.go) LLM-judge call down to the rare subset that
// actually matched something. Stage 2 is what filters that recall down to
// a confirmed detection worth blocking on -- see judge.go's doc comment.
// Blocking behavior itself (HAH-107, upgraded from log-only) lives in
// server.go's checkPromptInjection/CheckResponse wiring, not in this
// package.
package promptinjection

import "regexp"

// injectionPattern is one named, independently-testable entry in the
// stage-1 detection registry -- mirrors secretPatternRegistry's shape
// (internal/server/secrets_redact.go) so both registries stay easy to
// extend the same way: append one entry, no other code changes needed.
type injectionPattern struct {
	name string
	re   *regexp.Regexp
}

// InjectionPatternRegistry is the list every stage-1 Detect call walks.
// Exported (unlike secretPatternRegistry) so a future admin tool or test
// fixture outside this package can enumerate known pattern names without
// reaching into an unexported var.
//
// Deliberately over-broad (recall over precision): every entry here is
// written to catch a known injection SHAPE as cheaply as possible, accepting
// that it will also fire on benign text that merely discusses, quotes, or
// documents that shape (a security writeup, a support article, this very
// source file's own comments). That tradeoff is safe specifically because a
// stage-1 match alone never blocks anything -- it only gates whether the
// (comparatively expensive) stage-2 LLM judge runs at all. This list is NOT
// exhaustive and isn't meant to be; add an entry whenever a new shape shows
// up in practice (real traffic, red-team exercises, or a public writeup),
// same "don't hold out for complete" posture as secretPatternRegistry.
var InjectionPatternRegistry = []injectionPattern{
	{
		name: "ignore-instructions",
		re: regexp.MustCompile(
			`(?i)\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(previous|prior|above|earlier|preceding|your)\s+(instructions?|prompts?|rules?|guidelines?)\b`,
		),
	},
	{
		name: "new-instructions",
		re: regexp.MustCompile(
			`(?i)\b(new|updated|real)\s+(instructions?|system\s+prompt|rules?)\s*[:.]`,
		),
	},
	{
		name: "jailbreak-mode",
		re: regexp.MustCompile(
			`(?i)\b(developer|DAN|god|jailbreak|unrestricted|unfiltered)\s+mode\b`,
		),
	},
	{
		name: "no-restrictions",
		re: regexp.MustCompile(
			`(?i)\b(act|pretend|behave)\s+as\s+(if\s+)?(you\s+)?(have\s+)?no\s+(restrictions?|rules?|guidelines?|limits?|filters?)\b`,
		),
	},
	{
		name: "you-are-now",
		re:   regexp.MustCompile(`(?i)\byou\s+are\s+now\s+(a|an|in)\b`),
	},
	{
		name: "reveal-system-prompt",
		re: regexp.MustCompile(
			`(?i)\b(reveal|print|show|output|repeat)\s+(your|the)\s+(system\s+prompt|instructions?|initial\s+prompt)\b`,
		),
	},
	{
		// A "system:"/"assistant:" line embedded inside externally-fetched
		// tool content is inherently suspicious -- legitimate tool results
		// don't normally contain chat-role-prefixed lines.
		name: "embedded-system-role",
		re:   regexp.MustCompile(`(?im)^\s*(system|assistant)\s*:\s*\S`),
	},
	{
		name: "chat-template-tokens",
		re:   regexp.MustCompile(`<\|(im_start|im_end|assistant|system|user)\|>|\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>`),
	},
	{
		name: "agent-hijack",
		re: regexp.MustCompile(
			`(?i)\b(AI|assistant|agent|model)\s*[,:]?\s*(please\s+)?(execute|run|call)\s+the\s+following\b`,
		),
	},
}

// Detector reports whether s contains a known injection shape. Production
// uses *RegexDetector; tests substitute a stub. This is the stage-1
// interface only -- stage 2 (LLM-judge confirmation) is Judge, below.
type Detector interface {
	Detect(s string) *Result
}

// maxOffsetsPerPattern bounds how many occurrences of a single pattern
// Detect records. Without a cap, a pathological response (many repeats of
// a benign-looking trigger phrase) could otherwise generate an unbounded
// number of stage-2 judge calls -- see checkPromptInjection's own
// maxJudgeCallsPerResponse budget in server.go for the response-wide cap
// that backstops this per-pattern one.
const maxOffsetsPerPattern = 8

// Result is the outcome of a stage-1 Detect call.
type Result struct {
	Matched bool
	// MatchedNames holds one entry per (pattern, occurrence) pair -- a
	// pattern that matches N times (N capped at maxOffsetsPerPattern)
	// appears N times, once per occurrence, so callers can judge each
	// occurrence independently instead of only the first (a real
	// injection placed after an earlier benign match of the same pattern
	// -- e.g. "ignore previous instructions" first appearing inside a
	// sentence *describing* the attack, with the actual attack later in
	// the same document -- must not be able to hide behind that first,
	// judged-benign occurrence).
	MatchedNames []string
	// MatchedOffsets holds the byte offset of each occurrence, same
	// order/length as MatchedNames -- callers use this to build the
	// bounded text window Judge.Confirm scans (see judge.go), so the
	// judge sees only the text around each match rather than the whole
	// document.
	MatchedOffsets []int
}

// RegexDetector walks InjectionPatternRegistry over a string.
type RegexDetector struct{}

// New constructs a RegexDetector.
func New() *RegexDetector {
	return &RegexDetector{}
}

// Detect walks InjectionPatternRegistry against s and returns every
// occurrence of every pattern that matched, up to maxOffsetsPerPattern
// occurrences per pattern (empty/non-matching input returns a non-matched
// Result, never nil). Reports ALL occurrences, not just the first -- a
// single FindStringIndex-per-pattern call would let a real injection later
// in the same string hide behind an earlier, judged-benign match of the
// same pattern name.
func (d *RegexDetector) Detect(s string) *Result {
	var names []string
	var offsets []int
	for _, p := range InjectionPatternRegistry {
		locs := p.re.FindAllStringIndex(s, maxOffsetsPerPattern)
		for _, loc := range locs {
			names = append(names, p.name)
			offsets = append(offsets, loc[0])
		}
	}
	return &Result{Matched: len(names) > 0, MatchedNames: names, MatchedOffsets: offsets}
}
