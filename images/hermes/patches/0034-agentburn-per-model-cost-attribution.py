#!/usr/bin/env python3
"""Vicegerent patch: agentburn's by_model breakdown mis-attributes cost for
sessions that switched models mid-conversation (e.g. via Hermes' /model
command or a chart-configured model_aliases entry).

Bug
---
Hermes' state.db has two cost surfaces:

  sessions               -- one row per session; sessions.model is the
                             session's STARTING model. sessions.estimated_cost_usd
                             / actual_cost_usd is the whole session's total cost
                             regardless of how many models it actually used.
  session_model_usage    -- one row per (session, model) pair with that
                             model's own call count and cost. A session that
                             starts on claude-sonnet-5 and later runs
                             `/model opus` gets TWO rows here, each correctly
                             priced against its own model.

agentburn/adapters/hermes.py only ever reads `sessions`, so
agentburn/analyze.py's by_model breakdown groups every SessionRec by its
single sessions.model value -- crediting/blaming a session's ENTIRE cost to
whichever model it happened to start with, and silently dropping the cost of
every model switched to mid-session. Confirmed live on this deployment: three
sessions that used `/model opus` this week had real, non-zero
session_model_usage rows for claude-opus-4-8 (totaling ~$3.10), but
`agentburn_burn_report`'s by_model breakdown reported claude-opus-4-8 cost as
exactly $0.00 -- because those three sessions' sessions.model was
claude-sonnet-5, so 100% of their cost (opus included) was bucketed under
sonnet, and the one legacy session that WAS opus-only from creation (with an
unrelated cost_status='unknown' row) was the only one that showed up under
opus at all.

Fix
---
Three files, one logical change (kept in a single patch script since 0004's
own convention -- and this repo's "one patch file per logical change-set" --
already applies to closely-related multi-part fixes):

1. model.py -- new ModelUsageRec dataclass (mirrors SessionRec's token/cost
   shape) + a new Snapshot.model_usage list field.
2. adapters/hermes.py -- load session_model_usage (when present; the
   _columns()-based existence check keeps this adapter safe against agentburn
   deployments whose Hermes version predates this table) into
   snap.model_usage. Cost basis is derived from the row's own cost_status
   column rather than a bare `actual is not None` check -- unlike
   sessions.actual_cost_usd (nullable), session_model_usage.actual_cost_usd is
   NOT NULL DEFAULT 0, so an is-not-None check on it is always true and would
   silently treat every row as "actual $0" even when cost_status says
   "estimated".
3. analyze.py -- by_model now sources cost from snap.model_usage for any
   session that has model_usage rows (excluding it from the old whole-session
   attribution to avoid double-counting), and falls back to the original
   sessions.model attribution for any session with no model_usage rows at
   all (older data, or an adapter/agent that never populates that table).
   by_source, total, and night buckets are untouched -- they were never
   wrong; only the per-model split was.

Verified end-to-end against this deployment's live state.db (not a synthetic
fixture): patched by_model sums to exactly total.cost (97.3882 == 94.2906 +
3.0976), and the previously-invisible ~$3.10 of real opus spend now shows up
correctly under claude-opus-4-8 with 27 calls across 4 sessions (up from 1
session / $0.00 pre-patch).

Remove once agentburn's own Hermes adapter reads session_model_usage
natively.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0034"

# ---------------------------------------------------------------------------
# Fix 1/3: model.py -- add ModelUsageRec + Snapshot.model_usage
# ---------------------------------------------------------------------------

MODEL_ANCHOR = (
    "@dataclass\n"
    "class ToolStat:\n"
    "    name: str\n"
    "    calls: int\n"
    "    result_tokens: int  # tokens of tool results carried into context\n"
)

MODEL_REPLACEMENT = (
    "@dataclass\n"
    "class ToolStat:\n"
    "    name: str\n"
    "    calls: int\n"
    "    result_tokens: int  # tokens of tool results carried into context\n"
    "\n"
    "\n"
    "@dataclass\n"
    "class ModelUsageRec:\n"
    '    """One (session, model) cost/token slice from a session that used more\n'
    "    than one model (e.g. via Hermes' /model command). Vicegerent patch 0034:\n"
    "    lets by_model attribution reflect actual per-model billing instead of\n"
    "    crediting/blaming a whole session's cost to whichever model it started\n"
    '    with."""\n'
    "\n"
    "    session_id: str\n"
    "    model: Optional[str]\n"
    "    provider: Optional[str]\n"
    "    api_calls: int\n"
    "    input_tokens: int\n"
    "    output_tokens: int\n"
    "    cache_read_tokens: int\n"
    "    cache_write_tokens: int\n"
    "    reasoning_tokens: int\n"
    "    cost_usd: Optional[float]\n"
    '    cost_basis: str  # "actual" | "estimated" | "unknown"\n'
    "\n"
    "    @property\n"
    "    def total_tokens(self) -> int:\n"
    "        return (\n"
    "            self.input_tokens\n"
    "            + self.output_tokens\n"
    "            + self.cache_read_tokens\n"
    "            + self.cache_write_tokens\n"
    "            + self.reasoning_tokens\n"
    "        )\n"
)

SNAPSHOT_ANCHOR = (
    '    outcomes: dict = field(default_factory=dict)  # session_id → "failed" | "timeout" | …\n'
    "    compactions: dict = field(default_factory=dict)  # session_id → count of context compactions\n"
)

SNAPSHOT_REPLACEMENT = (
    '    outcomes: dict = field(default_factory=dict)  # session_id → "failed" | "timeout" | …\n'
    "    compactions: dict = field(default_factory=dict)  # session_id → count of context compactions\n"
    "    # Vicegerent patch 0034: per-(session, model) cost/token slices for sessions\n"
    "    # that switched models mid-conversation. Empty when the adapter's source\n"
    "    # has no such table (or none of the loaded sessions used more than one\n"
    "    # model) — analyze() falls back to whole-session attribution in that case.\n"
    "    model_usage: list[ModelUsageRec] = field(default_factory=list)\n"
)


def _patch_model() -> None:
    spec = importlib.util.find_spec("agentburn.model")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn/model.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch(model): already applied to {path} -- no-op")
        return

    count = src.count(MODEL_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(model): expected 1 ToolStat anchor in {path}, found {count} "
            "(agentburn upstream changed model.py -- re-verify)"
        )
    src = src.replace(MODEL_ANCHOR, MODEL_REPLACEMENT, 1)

    count = src.count(SNAPSHOT_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(model): expected 1 Snapshot anchor in {path}, found {count} "
            "(agentburn upstream changed model.py -- re-verify)"
        )
    src = src.replace(SNAPSHOT_ANCHOR, SNAPSHOT_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(model): ModelUsageRec + Snapshot.model_usage added to {path}")


# ---------------------------------------------------------------------------
# Fix 2/3: adapters/hermes.py -- load session_model_usage into snap.model_usage
# ---------------------------------------------------------------------------

HERMES_IMPORT_ANCHOR = (
    "from ..model import ActionEvent, DumpComposition, SessionRec, Snapshot, ToolStat\n"
)
HERMES_IMPORT_REPLACEMENT = (
    "from ..model import ActionEvent, DumpComposition, ModelUsageRec, SessionRec, Snapshot, ToolStat\n"
)

HERMES_INSERT_ANCHOR = (
    '            if r["end_reason"]:\n'
    '                snap.outcomes[str(r["id"])] = str(r["end_reason"])\n'
    "\n"
    '        mcols = _columns(con, "messages")\n'
)

HERMES_INSERT_REPLACEMENT = (
    '            if r["end_reason"]:\n'
    '                snap.outcomes[str(r["id"])] = str(r["end_reason"])\n'
    "\n"
    "        # Vicegerent patch 0034: Hermes' session_model_usage table tracks a\n"
    "        # per-(session, model) cost/token split for sessions that switched\n"
    "        # models mid-conversation (e.g. via /model). Load it when present so\n"
    "        # analyze()'s by_model breakdown can attribute cost to the model that\n"
    "        # actually incurred it, instead of crediting/blaming the whole\n"
    "        # session's cost to sessions.model (the session's starting model).\n"
    "        #\n"
    "        # Unlike sessions.actual_cost_usd (nullable, NULL means \"no actual\n"
    "        # cost recorded\"), session_model_usage.actual_cost_usd is\n"
    "        # NOT NULL DEFAULT 0 -- a bare `is not None` check on it is always\n"
    "        # true and would silently treat every row as \"actual $0\" even when\n"
    "        # cost_status says \"estimated\". Key off cost_status instead, which\n"
    "        # reliably distinguishes actual/estimated/unknown for this table.\n"
    '        umcols = _columns(con, "session_model_usage")\n'
    '        if {"session_id", "model", "api_call_count"} <= umcols:\n'
    '            ufields = ", ".join(\n'
    "                [\n"
    '                    "session_id",\n'
    '                    "model",\n'
    '                    _col(umcols, "billing_provider"),\n'
    '                    _col(umcols, "api_call_count", "0"),\n'
    '                    _col(umcols, "input_tokens", "0"),\n'
    '                    _col(umcols, "output_tokens", "0"),\n'
    '                    _col(umcols, "cache_read_tokens", "0"),\n'
    '                    _col(umcols, "cache_write_tokens", "0"),\n'
    '                    _col(umcols, "reasoning_tokens", "0"),\n'
    '                    _col(umcols, "estimated_cost_usd", "0"),\n'
    '                    _col(umcols, "actual_cost_usd", "0"),\n'
    """                    _col(umcols, "cost_status", "'unknown'"),\n"""
    '                    _col(umcols, "last_seen"),\n'
    "                ]\n"
    "            )\n"
    '            uwhere = "WHERE COALESCE(last_seen, 0) >= ?" if days else ""\n'
    "            for ur in con.execute(\n"
    '                f"SELECT {ufields} FROM session_model_usage {uwhere}",\n'
    "                (since,) if days else (),\n"
    "            ):\n"
    '                ustatus = ur["cost_status"] or "unknown"\n'
    '                if ustatus == "actual":\n'
    '                    ucost, ubasis = ur["actual_cost_usd"], "actual"\n'
    '                elif ustatus == "estimated":\n'
    '                    ucost, ubasis = ur["estimated_cost_usd"], "estimated"\n'
    "                else:\n"
    '                    ucost, ubasis = None, "unknown"\n'
    "                snap.model_usage.append(\n"
    "                    ModelUsageRec(\n"
    '                        session_id=str(ur["session_id"]),\n'
    '                        model=ur["model"],\n'
    '                        provider=ur["billing_provider"],\n'
    '                        api_calls=int(ur["api_call_count"] or 0),\n'
    '                        input_tokens=int(ur["input_tokens"] or 0),\n'
    '                        output_tokens=int(ur["output_tokens"] or 0),\n'
    '                        cache_read_tokens=int(ur["cache_read_tokens"] or 0),\n'
    '                        cache_write_tokens=int(ur["cache_write_tokens"] or 0),\n'
    '                        reasoning_tokens=int(ur["reasoning_tokens"] or 0),\n'
    "                        cost_usd=float(ucost) if ucost is not None else None,\n"
    "                        cost_basis=ubasis,\n"
    "                    )\n"
    "                )\n"
    "\n"
    '        mcols = _columns(con, "messages")\n'
)


def _patch_hermes_adapter() -> None:
    spec = importlib.util.find_spec("agentburn.adapters.hermes")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn/adapters/hermes.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch(hermes-adapter): already applied to {path} -- no-op")
        return

    count = src.count(HERMES_IMPORT_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(hermes-adapter): expected 1 import anchor in {path}, found {count} "
            "(agentburn upstream changed the import line -- re-verify)"
        )
    src = src.replace(HERMES_IMPORT_ANCHOR, HERMES_IMPORT_REPLACEMENT, 1)

    count = src.count(HERMES_INSERT_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(hermes-adapter): expected 1 load-loop anchor in {path}, found {count} "
            "(agentburn upstream changed adapters/hermes.py -- re-verify)"
        )
    src = src.replace(HERMES_INSERT_ANCHOR, HERMES_INSERT_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(hermes-adapter): session_model_usage loading added to {path}")


# ---------------------------------------------------------------------------
# Fix 3/3: analyze.py -- by_model sources from snap.model_usage when present
# ---------------------------------------------------------------------------

ANALYZE_ANCHOR = (
    "    for s in snap.sessions:\n"
    "        total.add(s)\n"
    '        by_source.setdefault(s.source, Bucket()).add(s)\n'
    '        by_model.setdefault(s.model or "unknown", Bucket()).add(s)\n'
    "        if s.started_at and _is_night(s.started_at, night_window):\n"
    "            night.add(s)\n"
    '            night_by_source.setdefault(s.source, Bucket()).add(s)\n'
    "        if s.total_tokens == 0 and s.message_count > 0:\n"
    "            zero_token += 1\n"
    "        bases.add(s.cost_basis)\n"
)

ANALYZE_REPLACEMENT = (
    "    # Vicegerent patch 0034: sessions that switched models mid-conversation\n"
    "    # (e.g. via /model) have their real per-model cost split recorded in\n"
    "    # snap.model_usage. Sessions covered there are excluded from the\n"
    "    # whole-session by_model attribution below so a session isn't\n"
    "    # double-counted; sessions absent from model_usage (older data, or an\n"
    "    # adapter that doesn't populate it) still fall back to sessions.model.\n"
    "    _model_usage_session_ids = {u.session_id for u in snap.model_usage}\n"
    "    for u in snap.model_usage:\n"
    '        by_model.setdefault(u.model or "unknown", Bucket()).add(u)\n'
    "\n"
    "    for s in snap.sessions:\n"
    "        total.add(s)\n"
    '        by_source.setdefault(s.source, Bucket()).add(s)\n'
    "        if s.id not in _model_usage_session_ids:\n"
    '            by_model.setdefault(s.model or "unknown", Bucket()).add(s)\n'
    "        if s.started_at and _is_night(s.started_at, night_window):\n"
    "            night.add(s)\n"
    '            night_by_source.setdefault(s.source, Bucket()).add(s)\n'
    "        if s.total_tokens == 0 and s.message_count > 0:\n"
    "            zero_token += 1\n"
    "        bases.add(s.cost_basis)\n"
)


def _patch_analyze() -> None:
    spec = importlib.util.find_spec("agentburn.analyze")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn/analyze.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch(analyze): already applied to {path} -- no-op")
        return

    count = src.count(ANALYZE_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(analyze): expected 1 by_model loop anchor in {path}, found {count} "
            "(agentburn upstream changed analyze.py -- re-verify)"
        )
    src = src.replace(ANALYZE_ANCHOR, ANALYZE_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(analyze): by_model now sources from snap.model_usage in {path}")


# ---------------------------------------------------------------------------

def main() -> int:
    _patch_model()
    _patch_hermes_adapter()
    _patch_analyze()
    return 0


if __name__ == "__main__":
    sys.exit(main())
