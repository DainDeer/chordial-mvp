# Grounded Action Reconciliation: the extractable engine

*Revision 2, 2026-07-12. The converged design after cross-review with Codex's
`ACTION_TRANSLATION_ENGINE_DESIGN.md` (the "Action Compiler" proposal — now
marked outdated; its durable ideas are folded in here, credited inline).
Follows `NATIVE_WORKSPACE_DESIGN.md`.*

**Converged decision (both reviews agree):** build the narrow `reconcile`
module now, share its typed operations with ordinary tool calling, and
revisit any broader "Action Compiler" umbrella only after three or four real
passes reveal what abstraction is actually common.

---

## 1. Landscape verdict: which layer is novel

The problem has three progressively less commoditized layers (the three-way
split is Codex's refinement of rev 1's two-way split, and it's sharper):

### Layer 1 — explicit command → typed call: **commoditized, adopt**

"add a high-priority task to the Sika Deer sprint" → `create_task(...)`.
Model-native function calling (benchmarked publicly — Berkeley Function
Calling Leaderboard, ICML 2025) plus structured-output libraries (instructor,
BAML, outlines, TypeChat's schema-with-repair-loop) handle this. Chordial
already rides it via `ToolRegistry` + the agent loop. An engine pitched here
competes with model providers and loses.

### Layer 2 — contextual dialogue → grounded command: **partially solved, stay in the persona loop for now**

"move that to Friday instead" — reference resolution, revision, dialogue
state. The strongest research precedent is Microsoft Semantic Machines'
[Task-Oriented Dialogue as Dataflow Synthesis](https://arxiv.org/abs/2009.11423)
(SMCalFlow): utterances as programs with explicit reference/revision
operations. The strongest product precedent is
[Rasa CALM](https://rasa.com/docs/learn/concepts/calm/): an LLM command
generator emits a small command language; a deterministic runtime owns
behavior. Both validate "model proposes commands, code disposes" — neither
ships a lightweight production Python contract for arbitrary schemas.

For chordial, layer 2 currently lives in the persona's tool loop, where
frontier-model context handling covers "that one" well enough. It moves into
engine scope only if evaluation shows it failing (see §6 deferred pieces).

### Layer 3 — implicit report → warranted side effect: **the novel wedge**

"i walked outside AND practiced piano :3" → mark two tasks done, log a win,
never mention the bookkeeping. Research exists around the edges (implicit
intent detection, action-item/commitment extraction, LangChain's "ambient
agents" framing) but it's papers and vertical meeting-summarizer products —
no reusable library ships the combination chordial already runs in
production:

1. **The two-jobs insight**: warmth and bookkeeping compete in one call and
   warmth wins — so extraction is a *separate, narrow, cheap post-hoc pass*
   (`CompletionReconcilerService`), not a bigger prompt on the persona.
2. **Grounding against a closed-world snapshot** of live entities.
3. **A deterministic validated executor**: hallucinated id = rejected op.
4. **User-calibrated judgment policy** ("generic activity = any amount counts").
5. **Audit/replay**: executed actions become events the next turn sees.

That stack is **grounded action reconciliation** — the name matters (Codex's
articulation, adopted): the user's lived reality and the recorded state have
*diverged*, and conversational evidence lets the system bring them back into
agreement. Reconciliation in the distributed-systems/accounting sense. The
framing is also a scoping tool and a design principle:

> **A reconciler converges records toward reality; it never manufactures
> reality.** It does not create intentions, commitments, or work the user
> never expressed. "I should probably…" is not evidence of anything.

---

## 2. Converged architecture: two paths, one executor

```
Explicit command                      Implicit conversational evidence
"move piano to friday"                "piano's just not happening tomorrow"
        │                                        │
provider-native tool calling            grounded reconciliation pass
(persona loop, layer 1/2)               (reconcile engine, layer 3)
        │                                        │
        └──────────────┬─────────────────────────┘
                       ▼
        shared typed domain operations
        (WorkspaceStore — validation, idempotency, invariants)
                       ▼
        audit events (kind='action') replayed into the next turn
```

This is not aspirational — it is chordial's current shape: the reconciler
already executes through the same `update_task` tool the persona uses, and
`NATIVE_WORKSPACE_DESIGN`'s "all writes go through `WorkspaceStore`" makes
the store the shared validated-op layer. The engine's `Op.execute` wraps the
same store methods the tools call. Both paths converge; the novel component
is the right-hand path, not another implementation of the left.

---

## 3. The engine contract

Working name **`reconcile`** (internal module first; chordial is consumer
#1). One sentence: *given a conversation delta, a snapshot of the user's
domain entities, typed ops, and a judgment policy, return validated
side-effects and an audit trail — silently, cheaply, and without ever
fabricating a write.*

```python
engine = ReconcileEngine(provider=...)          # any LLM provider (protocol, not SDK)

engine.register_pass(ReconcilePass(
    name="task_completion",
    snapshot=lambda ctx: store.open_tasks(ctx.user_id),   # closed world: [{id, line, meta}]
    ops=[MarkDone],                                        # typed ops (schema + validator + effector)
    policy=COMPLETION_POLICY,                              # judgment: matching + satisfaction, separately
    trigger=on_user_message,                               # engine doesn't own scheduling
))
```

- **`Snapshot`** — the closed world of referable entities. Hard safety
  boundary (Codex: "foundational" — agreed): the model may select only
  entities the application supplied.
- **`Op`** — typed operation: json-schema input, deterministic
  `validate(op, snapshot, ctx)`, `execute(op, ctx) -> AuditLine` wrapping a
  store method. **Ops come in two kinds with different guarantees** (rev 2
  addition — rev 1 and the Compiler doc both underspecified this):
  - **Reference ops** (`mark_done`, `bump_reschedules`): every entity id must
    exist in the snapshot. The strong guarantee — a fabricated or stale id is
    structurally impossible to execute.
  - **Creation ops** (`log_win`, `log_checkin`): the *relations* are
    snapshot-grounded but the created content is free text, so the guarantee
    is weaker and needs its own guard set: dedup against recent rows, a
    per-message cap, and evidence fields required to be verbatim quotes from
    the message (conveniently also the product requirement for Wins).
  The "never hallucinates a write" pitch means: reference ops by
  construction; creation ops by guard.
- **`Policy`** — a prompt fragment the engine keeps in two named parts,
  because they are different decisions (Codex, agreed): *matching
  conservatism* (is this message even about entity X — always conservative)
  and *satisfaction bar* (does what they described count — per-domain,
  per-user overridable; chordial's "generic activity = any amount counts"
  lives here and nowhere else).
- **`Trigger`** — post-user-turn (today's reconciler), post-session, cron,
  or event-driven. The engine exposes `run(pass, delta, ctx)`; the host owns
  when.

### Properties v1 gets for free (not roadmap items)

- **Multi-action independence** — ops are validated and applied
  independently; one bad op never blocks its siblings. Already how
  `_apply` works.
- **Idempotency by construction** — for reference ops, mostly free: a task
  already done isn't in the open-tasks snapshot, so a duplicate mark-done is
  a rejected op. Creation-op dedup is part of the guard set above.

### Provisioned now, built later: corrections

"oh wait, i didn't actually finish it" is a real utterance. The `AuditLine`
format must record enough to reverse cleanly — op, snapshot line at
execution time, evidence quote, resulting state — so a future `correction`
pass (or the persona, via an ordinary tool) can undo with confidence. Cheap
to provision in the audit schema now, painful to retrofit. (Promoted from
the Compiler doc's "correction frames," stripped to what reconciliation
needs.)

---

## 4. What the engine owns

1. **Prompt assembly** for the narrow pass: delta + bounded recent context +
   snapshot lines + policy + strict-json contract, with **"no action" as the
   explicit, common, correct answer** (today's reconciler prompt already
   ends "an empty list is the correct, common answer" — the Compiler doc's
   `NoAction`-is-essential point, already proven here).
2. **Parse & repair** — strict JSON out, at most one repair round on
   malformed output, then give up *silently*. The asymmetry is a design
   principle: a missed reconciliation is recoverable next turn; a bad write
   is not. No invisible retry loops that keep guessing until something
   validates (Compiler doc §7.4, agreed).
3. **The validated executor** — schema check, snapshot-grounding check, op
   validator, effect. Rejects are recorded in the audit trail, never
   surfaced as user-facing errors.
4. **Grounding hygiene** — when a pass resolves names rather than opaque
   ids: exact normalized match, then *unique* constrained match; **never
   silently accept a first-of-many substring match** (Compiler doc §7.5 —
   a genuine catch: `notion_tools._resolve_by_title` takes the first
   substring hit today; the workspace-native resolver should return
   ambiguity instead, and in a reconcile pass ambiguity = no action).
5. **Temporal hygiene** — where ops carry dates ("friday night" in a
   plan-drift pass): extraction keeps the raw expression; normalization is
   deterministic code in the *user's* timezone anchored to the message
   timestamp, never server time, and both are recorded in the audit line
   (Compiler doc §6.4, trimmed to reconciliation needs).
6. **Audit & replay protocol** — every executed op yields an `AuditLine`;
   chordial maps these to `kind='action'` events replayed into the next
   turn. **Reconciliation is not complete until the resulting action is
   visible to subsequent turns** — that's part of the contract, not a nicety
   (it's the existing fix for the re-do/re-ask bug class).
7. **The eval harness** — see §5; the piece that makes this a framework
   instead of a pattern.

Explicitly **out of scope**: the persona/chat loop, tool registries, memory,
scheduling, clarification dialogs, any UI. The engine is the silent second
pass, not the assistant. In chordial, *the warm persona is the clarification
layer* — "wait, did you mean the piano task?" is a personality moment; a
framework clarification module would be redundant here (and is deferred, §6).

---

## 5. Evaluation: asymmetric by design

Golden fixtures: `(conversation delta, snapshot fixture) → expected ops`,
runnable against any provider/model. Enriched with the Compiler doc's best
evaluation ideas (its §15 is its strongest section):

- **End-state assertions, not call-syntax matching**: a case asserts
  `task:piano.status == done`, not that a particular JSON string was emitted
  — τ-bench's insight ([paper](https://arxiv.org/abs/2406.12045)) that final
  DB state under domain rules is the only honest score. Multiple op
  sequences producing equivalent valid state all pass.
- **`forbidden` blocks** per case ("create any task", "touch another user's
  rows") — violations fail loudly regardless of other scores.
- **Precision and recall reported separately, never one aggregate**: a false
  write costs far more than a missed write. Release gating keys on
  false-mutation rate first (effectively zero for reference ops), missed
  actions second.
- **Repeated-trial consistency**: reconciliation runs on every message
  forever; a pass that's right 4 times in 5 is a flaky pass. Score `pass^k`
  over repeated trials, not single-shot accuracy.
- **Failure taxonomy** on every miss: parse / grounding / policy(matching) /
  policy(satisfaction) / validation-reject — so a regression names the layer
  that moved.
- **Shadow mode before writes**: any new pass runs with execution disabled,
  its proposals logged and compared against what actually happened (and
  later user corrections) before its ops go live. The existing reconciler
  becomes the shadow-mode baseline for its own port.
- **A frozen holdout slice** untouched by prompt iteration, so policy tuning
  can't overfit the corpus.

Fixture corpus seeded from existing reconciler tests + hand-authored cases
covering: accomplishment buried in emotional speech, discussion *without*
commitment ("i should really practice"), hedges, duplicate/similar titles,
date/timezone boundaries, and adversarial ids not in the snapshot. Corpus
comes **first** — the Compiler doc's phase-0 sequencing is right: make the
behavior measurable before touching the engine code.

---

## 6. Deferred pieces (adopt when a real pass demands them)

From the Compiler doc — good ideas, wrong time. Each with its trigger:

| piece | adopt when |
|---|---|
| Action frames / pending-state stack (its §8) | a pass needs multi-turn resolution ("did you mean X?" → "the second one") — i.e. when reconciliation stops being silent. Persona handles this today. |
| Structured clarification protocol (§9) | same trigger; in chordial the persona *is* the clarification surface |
| Risk/approval tiers + approval hashes (§10) | ops appear that are irreversible or externally visible (calendar invites, deletes). All current reconcile ops are low-risk/reversible by construction |
| State-diff preview & verification (§7.6/7.9) | ops mutate through anything other than the local `WorkspaceStore` (external APIs) — local writes are already exact |
| Capability retrieval (§7.2) | the op catalog outgrows the prompt; at ≤ a dozen ops per pass, send them all |
| Speech-act taxonomy as *output schema* (§6.2) | keep as *policy language* now ("commitment vs. hedge" belongs in the matching-conservatism text); promote to schema only if the taxonomy needs to be machine-read |
| AIR / full intermediate representation (§6) | a second host application with a genuinely different domain arrives |
| Separate service + HTTP/MCP surface (§12.3–12.4) | a second host that can't embed the Python package |

---

## 7. Chordial's pass roster and build plan

| pass | snapshot | ops (kind) | policy sketch |
|---|---|---|---|
| `task_completion` (exists → port) | open tasks | mark_done (ref) | generic-activity bar |
| `win_noticing` (V3 §5) | active plans + today's tasks | log_win (creation) | "log liberally; evidence = their words verbatim" |
| `checkin_extraction` | today's check-ins | log_checkin / update (creation/ref) | "asked, never demanded — extract, don't infer" |
| `plan_drift` / `reschedule_detection` | scheduled tasks | bump_reschedules / move (ref) | "slips are renegotiation triggers, not guilt" |

Four consumers of one pattern is the threshold where a pattern becomes a
module: **`src/services/reconcile/`** (~300–500 lines: pass runner, executor,
audit, fixtures runner), `CompletionReconcilerService` refactored into the
first registered pass.

**Build order (est. 4–5 days total, part of NATIVE_WORKSPACE phase B):**

1. **Corpus first** (~1 day): fixture format + seed cases from existing
   reconciler tests + hand-authored implicit/hedge/adversarial cases;
   baseline today's reconciler against it.
2. **Engine + port** (~2 days): contract from §3, executor, audit lines;
   port `task_completion`; old service becomes the shadow baseline until
   parity on the corpus.
3. **Second pass in shadow** (~1–2 days): `win_noticing` (the first creation
   op — proves the guard set), shadow-mode until its precision clears the
   gate, then enable.

The remaining passes land with their product features (check-ins ship with
digest v2; plan-drift with the reschedule UX).

## 8. The standalone-framework question (separate, later decision)

Unchanged from rev 1, sharpened by the review: going public
(`pip install reconcile`) adds provider hardening, non-chordial examples,
semver, a public eval suite — ~1–2 weeks plus maintenance, decided as a
deliberate open-source/product bet only after **≥3 live passes and a fixture
corpus proving the harness earns its keep**. The Compiler doc's own risk
list applies (§19: domain leakage, framework overreach) and its rule is
right: no standalone package until a *second real domain* has forced at
least two iterations of the contract.

On the umbrella: if the engine ever grows interactive clarification and
explicit-command interpretation, "Action Compiler" could name the umbrella —
but skepticism is warranted that the umbrella should *ever* exist as a
product. Its other legs are exactly what providers keep commoditizing, and
re-absorbing them would dilute the one differentiated claim: **a measurable
semantic safety layer that turns messy conversation into correct state
transitions — while knowing when not to act** (the Compiler doc's closing
line, which is worth keeping as the mission statement even though the
architecture it capped was too broad to start with).
