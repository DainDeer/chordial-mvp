# Action translation engine: research and framework design

> **⚠️ OUTDATED (2026-07-12).** Superseded by
> [`ACTION_RECONCILIATION_ENGINE.md`](ACTION_RECONCILIATION_ENGINE.md) rev 2,
> the converged design after cross-review: build the narrow `reconcile`
> module first; the broad Action Compiler is deferred (most of its
> later-stage pieces are tabled in that doc's §6 with adoption triggers).
> Durable ideas from this doc — end-state/τ-bench evaluation, speech-act and
> explicitness taxonomy, temporal reference hygiene, grounding order (never
> first-substring-match), corpus-first sequencing, release gates, the
> SMCalFlow/CALM precedents — are folded in there. Kept for reference; do
> not implement from this doc.

*Status: superseded — see banner above*  
*Date: 2026-07-12*  
*Working name: Action Compiler*  
*Companion: `BESPOKE_DATA_BACKEND_DESIGN.md`*

## 1. Thesis

Chordial's differentiated technical problem is not task storage. It is turning
ordinary, contextual, emotionally messy speech into the actions the user
actually intended—without silently doing too much, doing too little, or
mutating the wrong thing.

Examples:

- “I got the run done somehow, but piano is not happening tomorrow. Maybe
  Friday night?”
- “Actually make that the writing one, not the music one.”
- “I need to stop pretending the launch is happening this month.”
- “The dentist thing is handled.”
- “Can we make the next two weeks less ridiculous?”

The first example may mean: complete one existing task, preserve evidence for
a win, resolve a different task by context, move its planned date, choose an
evening window, and increment its reschedule history. The third may mean pause
a plan, move its horizon, renegotiate a goal, or merely invite discussion. A
valid JSON function call is only a small part of correctness.

The proposed reusable product is an **Action Compiler**: a domain-agnostic
runtime that compiles conversational language into typed, grounded,
policy-checked commands; asks targeted clarification when necessary; executes
approved commands through domain adapters; and verifies the resulting state.

Chordial becomes its first client and contributes a work-management domain
package. Chordial still owns personality, relationship, memory, proactive
behavior, and response voice. The Action Compiler owns interpretation and safe
action semantics.

## 2. Is this novel?

### Short answer

The broad field is not novel. The specific integration layer is still open.

Natural-language-to-action has decades of work under names including semantic
parsing, task-oriented dialogue, dialogue-state tracking, program synthesis,
function calling, and tool-using agents. Current libraries can produce typed
JSON, select tools, manage workflows, request approval, and resume durable
jobs.

What remains poorly solved as a reusable package is:

> Given an ongoing conversation, a changing domain state, and explicit
> business rules, determine the smallest set of user-intended state
> transitions; ground every reference; distinguish discussion from commitment;
> clarify only the consequential ambiguity; execute safely and idempotently;
> and measure success by the resulting state rather than plausible text or
> syntactically valid calls.

That is not a claim of a new academic field. It is a credible framework and
product opportunity at the seam between language models and domain services.

### Where the novelty concentrates

The problem has three progressively less commoditized layers:

1. **Explicit command → typed call**: “add a task called book dentist.” Model
   function calling and structured-output libraries handle this well enough
   that Chordial should adopt rather than reinvent it.
2. **Contextual dialogue → grounded command**: “move that to Friday instead.”
   This requires dialogue state, reference resolution, revision, and domain
   preconditions. Research and assistant frameworks address parts of it, but
   the production contract is not standardized.
3. **Implicit report → warranted side effect**: “I walked outside and somehow
   practiced piano too.” This requires noticing action inside ordinary speech,
   applying a product-specific satisfaction bar, grounding only against live
   entities, and making a conservative write without competing with the
   persona's emotional response. This **grounded action reconciliation** mode
   is the clearest differentiator and the best first wedge.

The proposed engine supports all three because they share contracts,
grounding, policy, execution, and evaluation. Its product story should lead
with layers two and three, not claim that JSON tool calling itself is novel.

### Closest research precedent: conversational semantic parsing

Microsoft Semantic Machines' **Task-Oriented Dialogue as Dataflow Synthesis**
represents dialogue state as a dataflow graph. Each utterance becomes a program
that extends the graph, including explicit operations for reference and
revision. Its SMCalFlow corpus contains complex conversations annotated with
executable programs. This is the strongest conceptual precedent for handling
“that one,” corrections, and multi-turn revisions as program operations rather
than flattened chat history:

- [Task-Oriented Dialogue as Dataflow Synthesis](https://arxiv.org/abs/2009.11423)
- [SMCalFlow dataset and leaderboard](https://microsoft.github.io/task_oriented_dialogue_as_dataflow_synthesis/)
- [OpenDF simplified execution engine](https://arxiv.org/abs/2206.14125)
- [DFEE execution and evaluation toolkit](https://ojs.aaai.org/index.php/AAAI/article/view/27073)

The research shows that an explicit executable intermediate representation is
valuable. It does not provide a lightweight, production-oriented Python
framework for arbitrary application schemas, risk policies, idempotent
mutations, end-state verification, and contemporary hosted LLMs.

### Closest product precedent: Rasa CALM

Rasa CALM uses an LLM **Command Generator** to translate a message plus
conversation/flow state into a small command language such as starting a flow,
setting a slot, cancelling, or disambiguating. Deterministic flows own the
business logic. It also retrieves only relevant flows when the catalog grows:

- [Rasa LLM Command Generators](https://rasa.com/docs/reference/config/components/llm-command-generators/)
- [Rasa CALM architecture](https://rasa.com/docs/learn/concepts/calm/)
- [Rasa flows](https://rasa.com/docs/reference/primitives/flows/)

This strongly validates “LLM emits commands; deterministic runtime controls
behavior.” Rasa is optimized around designed conversational flows and slot
collection. The proposed engine focuses instead on compiling free-form,
possibly multi-action utterances into mutations over an existing domain model,
including entity grounding, state-diff validation, partial clarification,
idempotency, and execution evaluation.

### Evidence that function calling alone is insufficient

The Berkeley Function Calling Leaderboard evaluates correct function and
argument selection, including multi-turn and relevance cases. **τ-bench** goes
further: it evaluates agents interacting with users, tools, and domain policies
by comparing the final database state with the goal state. Its original paper
reported low and inconsistent success even for strong function-calling models,
which supports making end-state correctness and repeated-trial reliability
first-class:

- [Berkeley Function Calling Leaderboard paper](https://openreview.net/forum?id=2GmDdhBdDk)
- [τ-bench paper](https://arxiv.org/abs/2406.12045)
- [TaskBench](https://proceedings.neurips.cc/paper_files/paper/2024/file/085185ea97db31ae6dcac7497616fd3e-Paper-Datasets_and_Benchmarks_Track.pdf)
- [Gorilla / APIBench](https://arxiv.org/abs/2305.15334)

The engine should therefore evaluate a proposed action not only by schema
validity or exact command text, but by whether it produced the intended state
under domain rules.

## 3. Existing libraries and what they solve

No single library below is the proposed engine. Several are useful components
or design references.

| project / area | provides | does not solve |
|---|---|---|
| [TypeChat](https://github.com/microsoft/TypeChat) | Natural language to TypeScript types; “schema engineering” and validation/repair | Stateful entity grounding, policies, execution, end-state evaluation; TypeScript-first |
| [Outlines](https://github.com/dottxt-ai/outlines) | Constrained structured generation from Python/Pydantic/JSON Schema, regex, or grammar | Whether a valid structure expresses the right action |
| [Instructor](https://github.com/567-labs/instructor) | Typed extraction with validation and retry across model providers | Domain command lifecycle, grounding, clarification, execution semantics |
| Model-native function calling | Tool selection and JSON arguments | Correct commitment, ambiguity, policy, idempotency, verification |
| [Rasa CALM](https://rasa.com/docs/learn/concepts/calm/) | Command generation, dialogue stack, slot filling, disambiguation, deterministic flows | General mutation compiler over arbitrary stateful domain APIs |
| [Pydantic AI deferred tools](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/) | Typed tools, approval gates, externally executed/deferred calls | Domain interpretation contract and state-diff correctness |
| [LangGraph](https://www.langchain.com/langgraph) | Stateful graph orchestration, persistence, interrupts, human-in-loop patterns | The semantic action language and domain correctness model |
| [Semantic Kernel](https://learn.microsoft.com/en-us/semantic-kernel/concepts/enterprise-readiness/filters) | Plugin/function orchestration and invocation filters | Correct grounding and intent-to-state evaluation |
| [Temporal](https://docs.temporal.io/) | Durable, resumable execution for long-running workflows | Language interpretation; excessive for ordinary atomic task mutations |
| [MCP](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) | Standard discovery and invocation protocol for tools with JSON Schemas | Intent resolution, orchestration policy, or correctness; tools are model-controlled |
| Semantic parsing / SMCalFlow | Executable programs, references, revisions, dialogue state | A compact production SDK for current application stacks |

### Recommended use, not reinvention

- Define the intermediate representation with Python dataclasses or Pydantic.
- Use provider-native structured output where reliable; use Outlines for local
  models or a validation/retry library where provider support is weak.
- Borrow Rasa's command-generator/deterministic-runtime split.
- Borrow dataflow dialogue's explicit reference and revision concepts without
  adopting its full expression language initially.
- Borrow τ-bench's final-state and repeated-trial evaluation model.
- Use the application's existing transactions/outbox for short actions.
- Add Temporal only if the engine later runs hours-long, multi-service
  workflows that truly need durable suspension.
- Offer MCP as an adapter for capability discovery, not as the internal
  semantic model.

Do not begin by putting Chordial inside a general-purpose agent framework.
Chordial already has a provider-neutral agent loop and a domain-specific need.
The valuable code is the compiler, policies, grounding, and evaluator.

## 4. Product boundary

### The engine owns

- capability/command registration;
- relevant-command retrieval;
- conversational action-state representation;
- natural-language compilation into an intermediate representation;
- structural and semantic validation;
- entity and temporal grounding;
- ambiguity and risk assessment;
- clarification and confirmation requests;
- deterministic command execution through an adapter;
- idempotency and action-run history;
- outcome verification and normalized results;
- evaluation datasets, replay, comparison, and model/prompt selection.

### The host application owns

- authentication and tenant identity;
- domain repositories and transaction semantics;
- authorization and ultimate enforcement of invariants;
- chat transport and response delivery;
- persona, tone, memories, and relationship behavior;
- domain-specific confirmation policy choices;
- user-facing rendering, though the engine returns structured material;
- credentials and provider configuration.

### The model owns

- proposing interpretations from language;
- producing structured candidate commands;
- ranking plausible references when deterministic resolution cannot decide;
- optionally drafting clarification wording.

The model never owns authorization, tenant choice, database access, command
preconditions, or the decision that execution succeeded.

## 5. Core abstraction: a semantic contract

A domain package registers **semantic contracts**, not raw API endpoints. Each
contract describes one meaningful user action and everything needed to compile,
validate, execute, explain, and test it.

```python
class CommandSpec(Generic[ArgsT, ResultT]):
    name: str
    description: str
    args_type: type[ArgsT]
    examples: list[Example]
    negative_examples: list[Example]
    required_explicitness: Explicitness
    reversibility: Reversibility
    risk: RiskLevel

    async def retrieve_context(ctx, partial_args) -> ContextSlice: ...
    async def ground(ctx, args) -> Grounded[ArgsT]: ...
    async def validate(ctx, args) -> Validation: ...
    async def preview(ctx, args) -> StateDiff: ...
    async def execute(ctx, args, idempotency_key) -> ResultT: ...
    async def verify(ctx, before, result) -> Verification: ...
    def render_receipt(result) -> Receipt: ...
```

From one contract, the engine derives:

- the model-facing schema and descriptions;
- retrieval documents for selecting relevant commands;
- structural validation;
- policy/approval defaults;
- dry-run previews;
- observability labels;
- a test fixture surface;
- optional JSON Schema, HTTP, and MCP adapters.

This “define semantics once, compile several runtime artifacts” approach is the
most credible framework-level innovation. Existing tool registries usually
stop at a name, description, argument schema, and callable.

## 6. Action Intermediate Representation (AIR)

The LLM does not emit database calls. It emits an **Action Interpretation**
containing claims, references, and candidate commands.

### 6.1 Top-level result

```python
ActionInterpretation = Union[
    NoAction,
    ActionProposal,
    ClarificationNeeded,
    UnsupportedRequest,
]
```

`NoAction` is essential. Conversation about a task is not necessarily a request
to change it.

```python
class NoAction:
    reason: Literal[
        "conversation_only",
        "question_only",
        "insufficient_commitment",
        "no_matching_capability",
    ]
```

### 6.2 Proposal

```python
class ActionProposal:
    groups: list[ActionGroup]
    speech_acts: list[SpeechAct]
    source_spans: list[SourceSpan]
    global_assumptions: list[Assumption]

class ActionGroup:
    id: str
    mode: Literal["atomic", "independent"]
    commands: list[ProposedCommand]
    depends_on: list[str]

class ProposedCommand:
    local_id: str
    command: str
    arguments: dict
    evidence_spans: list[SourceSpan]
    explicitness: Literal["explicit", "implied", "speculative"]
    assumptions: list[Assumption]
```

`speech_acts` distinguish report, request, correction, question, suggestion,
commitment, cancellation, and emotional disclosure. This helps prevent “I
should probably…” from being treated the same as “move it to Friday.”

The engine never trusts a numeric model confidence as truth. Confidence is
calibrated from observable features: explicitness, grounding cardinality,
policy risk, agreement among passes/models, validation result, and historical
evaluation performance.

### 6.3 Entity references

Entity arguments begin as references, not guessed IDs:

```python
EntityRef = Union[
    ById,
    ByExactName,
    ByDescription,
    ByConversationAnchor,
    ByPreviousResult,
    ByOrdinal,
]
```

Examples:

- “the run” → `ByDescription(type="task", text="run", status="open")`
- “that one” → `ByConversationAnchor(anchor="last_discussed_task")`
- “the second one” → `ByOrdinal(listing_id="...", index=2)`
- “actually the writing one” → a `Revision` targeting a pending reference

Grounding returns zero, one, or many candidates plus features explaining the
match. Only one valid candidate permits silent execution unless a domain
contract explicitly permits creation on no match.

### 6.4 Temporal references

Preserve both the original expression and the normalized value:

```python
class TemporalRef:
    expression: str          # "Friday night"
    local_date: date | None
    local_time: time | None
    window: str | None       # "evening"
    timezone: str
    resolution_basis: date  # local date at utterance time
```

Normalization is deterministic after extraction. Relative dates must use the
user's timezone and message timestamp, not server time. If “next Friday” is
locally ambiguous, the domain policy can clarify or show the resolved date in
the receipt.

### 6.5 Revision and cancellation

Corrections are first-class:

```python
class Revision:
    target: PendingActionRef | PreviousCommandRef
    replace_arguments: dict

class Cancellation:
    target: PendingActionRef | ActiveFlowRef
```

This avoids reinterpreting “actually Tuesday” as a brand-new scheduling
request without knowing what Tuesday modifies.

## 7. Compiler pipeline

```text
message + dialogue action state + domain snapshot
                         |
                1. speech-act gate
                         |
             2. capability retrieval
                         |
              3. semantic compilation
                         |
              4. schema validation/repair
                         |
          5. entity + temporal grounding
                         |
         6. precondition and policy checks
                         |
       7. ambiguity/risk decision per group
              /          |          \
         execute      clarify      confirm
              \          |          /
                8. deterministic execution
                         |
                9. outcome verification
                         |
              receipt + updated action state
```

### 7.1 Speech-act gate

Classify whether the message contains actionable commitment at all. It may also
contain an actionable clause alongside ordinary conversation. Favor
under-action for implied destructive or high-impact changes.

This stage can be combined with compilation in the first implementation, but
must remain explicit in the output and evaluation taxonomy.

### 7.2 Capability retrieval

Do not send hundreds of command schemas on every turn. Retrieve a bounded set
using:

- active/pending commands and referenced entity types;
- deterministic keyword/alias matching;
- embedding similarity over contract descriptions/examples;
- guards based on actor, domain state, and persona permissions;
- always-included meta commands: no-action, revise, cancel, clarify.

Log which commands were retrieved. A missing correct command is a retrieval
failure, distinct from a compiler failure.

For Chordial's first dozen work commands, skip vector infrastructure and
include the entire stable catalog. Add retrieval only when evaluation shows
catalog interference or prompt size warrants it.

### 7.3 Semantic compilation

Send the model:

- the current user message;
- a bounded action-relevant conversation view;
- pending clarification/confirmation state;
- relevant command contracts and examples;
- compact candidate entity context where known;
- the AIR schema;
- explicit instruction that no action is valid.

The output is an untrusted proposal. Use low-variance settings and provider
structured output. Preserve model/prompt/schema versions on every run.

### 7.4 Structural validation and repair

Validate discriminator, command names, argument types, allowed fields, and
group dependencies. Schema-constrained generation prevents malformed JSON but
does not prove semantic correctness.

Permit at most one format-repair attempt. Semantic errors go through normal
grounding/clarification, not an invisible loop asking the model to keep
guessing until something validates.

### 7.5 Grounding

Resolve references through host-provided resolvers. A resolver must be pure or
read-only and tenant-scoped. It returns candidates with stable IDs and compact
labels; the model never chooses a tenant.

Grounding order:

1. explicit opaque ID from a recent engine-produced listing;
2. active pending-action anchor;
3. exact normalized title;
4. unique constrained search result;
5. contextual/fuzzy candidates requiring ambiguity policy.

Never silently select the first substring match.

### 7.6 Precondition and state-diff validation

The command contract checks present state and calculates an expected diff
before mutation:

```json
{
  "entity": "task:...",
  "changes": {
    "status": {"from": "in_progress", "to": "done"},
    "completed_at": {"from": null, "to": "<now>"}
  },
  "side_effects": ["may_create_win"]
}
```

The engine rejects impossible transitions and exposes consequential effects to
the confirmation policy. The domain service validates everything again inside
the transaction; preview is not authorization.

### 7.7 Ambiguity and risk decision

Decision is per independent action group, so an ambiguous reschedule does not
block an unambiguous completion in the same sentence.

Clarify when:

- two or more materially plausible entities remain;
- a required argument has no safe default;
- the utterance supports materially different commands;
- a revision lacks a unique pending target;
- the expected state diff conflicts with what the user appears to believe.

Confirm when:

- the command is irreversible or externally visible;
- it affects many entities;
- the user's commitment is only implied;
- a domain policy requires approval;
- model interpretation and deterministic state disagree.

Execute without confirmation when the intent is explicit, target is unique,
operation is reversible/low-risk, and all preconditions pass. Constantly
confirming task capture and completion would destroy the product experience.

### 7.8 Execution

Execution receives only grounded, validated command objects. It runs through a
host adapter with:

- injected authenticated actor/tenant context;
- an engine-generated idempotency key;
- optional expected entity version;
- transaction-group metadata;
- timeout and cancellation;
- normalized domain errors.

The engine must not generate or execute arbitrary Python, SQL, URLs, or MCP
tool names supplied by user text.

### 7.9 Verification

Verification compares the committed result or fresh state with the predicted
diff. It produces `verified`, `partially_verified`, `conflict`, or `unknown`.
A successful HTTP/tool response alone is insufficient.

For database-local Chordial commands, verification should normally be exact
and cheap. For external systems, it may be delayed through the outbox.

## 8. Conversational action state

Do not use the raw chat transcript as the only dialogue state. Maintain a
small structured **Action Frame** alongside it:

```python
class ActionFrame:
    frame_id: str
    status: Literal[
        "proposed", "awaiting_clarification", "awaiting_confirmation",
        "ready", "executing", "completed", "cancelled", "expired", "failed"
    ]
    interpretation: ActionInterpretation
    resolved_entities: dict[str, ResolvedEntity]
    unresolved_questions: list[Clarification]
    approvals: list[ApprovalDecision]
    source_message_ids: list[str]
    expires_at: datetime
```

This frame makes the following deterministic:

- “the first one” references the last displayed candidate list;
- “yes” approves a specific preview, not whatever the model remembers;
- “no, Friday” revises a pending date;
- “forget it” cancels the active pending action;
- a new unrelated request can coexist with a suspended frame;
- stale confirmations expire if domain state changes.

Keep at most a small stack of active frames. A later message may interrupt one
frame with an independent action, but vague replies bind to the most recently
addressed frame.

## 9. Clarification protocol

A clarification is structured data that the host can render in its own voice:

```python
class Clarification:
    id: str
    frame_id: str
    kind: Literal["entity", "command", "argument", "conflict"]
    prompt_facts: dict
    options: list[ClarificationOption]
    accepts_free_text: bool
```

Good clarification asks one question that changes the action:

> “Did you mean ‘morning run’ for today or ‘long run’ for Saturday?”

Bad clarification asks the user to restate the whole request or exposes
internal schemas.

Options carry opaque engine tokens, not raw entity IDs. The response may select
an option or provide a correction in natural language. Resolution updates the
same frame; it does not rerun the original message as an unrelated turn.

Measure clarification quality separately:

- necessary clarification rate;
- unnecessary clarification rate;
- correct option coverage;
- turns to resolution;
- abandonment after clarification.

## 10. Risk and approval model

Each contract declares defaults, and the host may tighten them:

| risk | examples in Chordial | default |
|---|---|---|
| observational | list tasks, build agenda | execute |
| low/reversible | create task, mark done, move planned day | execute when explicit and grounded |
| medium | pause plan, bulk reschedule, reopen completed goal | preview/confirm when implied or broad |
| high/external | delete data, send invite, create external calendar event | explicit confirmation |
| prohibited | cross-tenant access, arbitrary command/code | reject |

Approval binds to a hash of command, grounded arguments, state versions, and
preview. Changing any of those invalidates approval.

## 11. Chordial domain package

The first adapter wraps the native application services described in
`BESPOKE_DATA_BACKEND_DESIGN.md`.

### 11.1 Initial commands

```text
work.task.list
work.task.create
work.task.update_details
work.task.schedule
work.task.complete
work.task.reopen
work.task.deprioritize

work.plan.list
work.plan.create
work.plan.update
work.plan.pause
work.plan.complete

work.goal.list
work.goal.create
work.goal.update
work.goal.complete

work.cycle.list
work.cycle.create
work.cycle.activate
work.cycle.complete

work.win.list
work.win.record
work.check_in.record
```

The engine catalog can expose a smaller model-facing surface by grouping
closely related commands while retaining explicit internal operations.

### 11.2 Entity resolvers

- task: ID, exact title, recent agenda membership, active plan/cycle, status,
  temporal description, conversation anchor;
- plan: ID, exact title, helper steward, active status;
- goal: ID, exact title, parent plan;
- cycle: ID, current/next, exact title, date range;
- win/check-in: usually created rather than resolved; listing remains scoped.

Resolvers call tenant-scoped repositories and return compact candidates. They
never mutate.

### 11.3 Domain policies

- “done/handled/finished” can complete a uniquely grounded open task.
- Reporting an accomplishment may create a win only under the configured
  conservative evidence policy.
- “I should,” “maybe,” and hypothetical planning do not create work unless the
  larger context expresses commitment.
- `planned_for` and `due_on` are semantically distinct.
- moving `planned_for` increments reschedule history through the domain service.
- goal/plan completion is more consequential than task completion and may
  require confirmation if implied.
- bulk changes always preview counts and representative items.
- helper persona permission is checked before compilation and again by the
  domain adapter.

### 11.4 Example compilation

Input:

> “I got the run done somehow, but piano is not happening tomorrow. Maybe
> Friday night?”

Possible AIR after grounding:

```json
{
  "groups": [
    {
      "id": "completion",
      "mode": "independent",
      "commands": [{
        "command": "work.task.complete",
        "arguments": {"task": {"anchor": "morning run"}},
        "explicitness": "explicit",
        "evidence_spans": ["I got the run done somehow"]
      }]
    },
    {
      "id": "reschedule",
      "mode": "independent",
      "commands": [{
        "command": "work.task.schedule",
        "arguments": {
          "task": {"description": "piano", "planned_for": "tomorrow"},
          "new_time": {"expression": "Friday night", "window": "evening"}
        },
        "explicitness": "implied",
        "evidence_spans": ["piano is not happening tomorrow", "Maybe Friday night?"]
      }]
    }
  ]
}
```

The completion can execute if “the run” resolves uniquely. The reschedule may
be presented as a light confirmation because “maybe” is a suggestion rather
than an unambiguous directive. The response persona remains Chordial's job.

## 12. Engine interfaces

### 12.1 Embedded Python API first

```python
result = await engine.interpret(
    message=MessageRef(id=event_id, text=text, timestamp=sent_at),
    actor=ActorContext(user_uuid=user_uuid, helper_id=helper_id),
    conversation=action_context,
    domain="chordial.work.v1",
)

outcome = await engine.resolve_or_execute(result)
```

Start as a package inside Chordial's process. This avoids a network boundary,
distributed transactions, and premature multi-tenant service auth while the
IR changes rapidly.

### 12.2 Stable package boundaries

```text
action_compiler/
  contracts/       # CommandSpec, DomainPackage
  ir/              # interpretations, refs, diffs, receipts
  compiler/        # retrieval, prompting, provider adapter
  grounding/       # resolver protocols and ambiguity logic
  policy/          # risk, explicitness, confirmation
  runtime/         # frames, execution, verification
  evaluation/      # cases, runner, metrics, reports
  storage/         # action-run/frame interfaces
  adapters/
    mcp/
    http/

chordial/
  action_domain/
    commands.py
    resolvers.py
    policies.py
    examples.py
    adapter.py
```

Do not publish a standalone package until the Chordial adapter has forced at
least two iterations of the contract and AIR.

### 12.3 Later service API

Once stable, a separate service can expose:

```text
POST /v1/interpret
POST /v1/frames/{id}/answer
POST /v1/frames/{id}/approve
POST /v1/frames/{id}/cancel
GET  /v1/runs/{id}
POST /v1/evaluations/run
```

A service boundary requires signed host-to-engine identity, encrypted tenant
context, strict adapter registration, tracing propagation, and a decision
about whether execution occurs in the engine or returns an execution plan to
the host. Prefer embedded execution until another real host application needs
the engine.

### 12.4 MCP adapter

Export registered contracts as MCP tools only as an interoperability feature.
MCP standardizes discovery, schemas, and invocation; the Action Compiler adds a
higher-level layer that may expose one `interpret_and_propose` capability or
wrap individual domain commands with grounding/policy. Never accept arbitrary
remote MCP servers into the trusted execution catalog without host approval.

## 13. Persistence model

The engine can share Chordial's Postgres initially.

### `action_runs`

- run ID, tenant/user, host, domain/version;
- source message/event ID;
- status and timestamps;
- provider/model/prompt/schema versions;
- retrieved command names;
- raw structured interpretation (sensitive, retention-limited);
- validation/grounding/policy/verification outcomes;
- token usage and latency;
- parent run ID for correction/retry.

### `action_frames`

- frame ID, tenant, status, serialized AIR;
- resolved references and state versions;
- pending clarification/confirmation;
- approval hash and decisions;
- source message IDs;
- created/updated/expiry timestamps.

### `action_steps`

- run/frame ID, ordered stage;
- input/output summaries;
- status, error category, latency;
- command local ID and idempotency key where relevant.

### `action_feedback`

- run ID;
- implicit correction/revert or explicit user feedback;
- adjudicated expected interpretation/state when reviewed;
- dataset eligibility and privacy consent flags.

Store redacted/minimized traces in production. Raw messages and model outputs
may already exist in the host conversation log; avoid unnecessary duplication.
Evaluation exports must be de-identified.

## 14. Integration with Chordial's current loop

Today, `AgentService` lets the persona model choose and execute tools while
generating the reply. That couples warm conversation and exact action parsing
in one pass—the same competition already observed by the completion
reconciler.

### Recommended path

1. The incoming user message is recorded normally.
2. In parallel or immediately before the persona response, the Action Compiler
   produces a proposal from a stable, action-focused prompt.
3. Safe explicit commands execute through native domain services.
4. Pending clarification/confirmation is provided to the orchestrator as
   structured response obligations.
5. The persona model receives verified receipts and unresolved questions, then
   responds naturally.
6. The ordinary persona tool surface is initially retained as fallback, then
   work mutations are removed once compiler recall is proven.

This separates “notice what should happen” from “be emotionally present,”
without forcing every conversational turn through a long agentic loop.

### Latency strategy

- Run action compilation and non-action persona preparation concurrently only
  if command results are not needed to phrase the response.
- For likely actionable messages, compile first and inject receipts.
- Use a small/fast model once evaluation proves it meets accuracy thresholds.
- Skip compilation through a high-recall deterministic gate only after its
  false-negative rate is measured. Initially, `NoAction` from the compiler is
  safer than a separate brittle classifier.

### Compatibility with the completion reconciler

Use the reconciler as a shadow benchmark during transition. Eventually its
narrow completion detection becomes a specialized compiler policy/pass. Keep a
separate conservative accomplishment detector if that improves recall without
inflating the main AIR.

## 15. Evaluation framework: the actual moat

The framework is only valuable if it can show that changes improve behavior.
Evaluation is a first-class package, not a collection of prompt snapshots.

### 15.1 Case format

```yaml
id: mixed_completion_reschedule_001
domain: chordial.work.v1
initial_state_fixture: fixtures/busy_week.yaml
conversation:
  - user: "what's tomorrow looking like?"
  - assistant: "run in the morning and piano anytime"
  - user: "got the run done, piano needs to move to friday night"
expected:
  actions:
    - complete task:morning-run
    - schedule task:piano for:2026-07-17 window:evening
  clarification: none
  final_state_assertions:
    - task:morning-run.status == done
    - task:piano.planned_for == 2026-07-17
    - task:piano.reschedule_count == 1
forbidden:
  - create any task
  - mutate another user's rows
```

Support multiple acceptable command sequences when they produce equivalent
valid state.

### 15.2 Metrics

**Compiler metrics**

- actionable/no-action precision and recall;
- command selection precision/recall;
- argument exact/semantic match;
- entity grounding accuracy;
- temporal resolution accuracy;
- multi-action completeness;
- correction/revision attachment accuracy.

**Safety/UX metrics**

- false mutation rate;
- missed mutation rate;
- necessary and unnecessary clarification rates;
- confirmation appropriateness;
- cross-tenant and prohibited-action rate;
- duplicate side-effect rate;
- undo/correction rate in live use.

**End-state metrics**

- exact expected state success;
- invariant violations;
- forbidden state changes;
- partial completion;
- verification agreement.

**Reliability/operational metrics**

- repeated-trial `pass^k`/consistency;
- p50/p95 latency and token cost;
- format repair and retry rate;
- retrieval miss rate;
- calibration by risk/explicitness bucket.

The release gate emphasizes false mutations and end state, not only F1 or
syntactic validity.

### 15.3 Dataset strategy

Begin with 200–500 hand-authored and paraphrased cases covering Chordial's
actual semantics:

- direct commands;
- accomplishment embedded in emotional speech;
- discussion without commitment;
- hedging and suggestions;
- duplicate and similar titles;
- pronouns, ordinals, and recent-list references;
- revisions, cancellations, and interruptions;
- multiple independent actions;
- date/timezone boundaries;
- stale state and concurrent edits;
- invalid transitions and permission failures;
- adversarial requests to select another tenant or invoke unknown tools.

Then add de-identified production failures through explicit review. Never train
or optimize directly on unreviewed inferred “success”; the system's own
behavior can create misleading labels.

### 15.4 Shadow and counterfactual evaluation

Before enabling writes:

- run the compiler in shadow mode;
- compare its proposed commands with actual tool calls and later observed
  corrections;
- have a human adjudicate disagreements;
- replay the same cases across models, prompts, schemas, and context policies;
- execute against an isolated state simulator, never production.

Every prompt/model/domain-contract change produces a versioned evaluation
report in CI. Maintain a frozen holdout set to resist prompt overfitting.

## 16. Build-versus-adopt recommendation

### Build

- AIR and action-frame state;
- semantic contract/command registry;
- entity/temporal grounding protocols;
- explicitness, ambiguity, risk, and approval policy;
- deterministic execution and verification lifecycle;
- state-based evaluation harness;
- Chordial domain package.

These are the differentiated layer and must reflect Chordial's product
semantics.

### Adopt

- Pydantic or dataclasses + JSON Schema for typed IR;
- hosted model structured-output/function-call capability;
- Outlines if local constrained decoding becomes useful;
- Postgres transactions, idempotency, and outbox from the bespoke backend;
- standard tracing such as OpenTelemetry;
- MCP adapters for interoperability;
- Temporal only for later genuinely long-running workflows.

### Evaluate, but do not anchor on initially

- Rasa CALM as a design reference or prototype baseline;
- Pydantic AI for approval/deferred-tool mechanics;
- LangGraph for a visual/durable orchestration graph;
- DSPy or other optimizers after a strong executable metric and dataset exist.

Adding a large framework before the semantic contract is stable will move the
design problem into framework configuration rather than solve it.

## 17. Implementation plan

### Phase 0 — corpus and semantics (1 week)

- extract action examples from current tests and synthetic conversations;
- define Chordial command catalog, speech acts, explicitness, risk, and
  ambiguity labels;
- build 150+ initial cases with expected end-state assertions;
- establish baseline using today's direct tool loop.

Exit: the problem is measurable before a new engine exists.

### Phase 1 — AIR and contracts (1–1.5 weeks)

- implement typed AIR, refs, temporal values, diffs, receipts, and errors;
- implement `CommandSpec`, `DomainPackage`, catalog, and schema generation;
- define task/plan commands and resolvers against in-memory fixtures;
- serialize/version all artifacts.

Exit: hand-authored AIR can validate, ground, preview, execute in a simulator,
and verify.

### Phase 2 — compiler and evaluation runner (1–2 weeks)

- provider-neutral compiler adapter using existing `BaseAIProvider` concepts;
- structured output, bounded repair, prompt/schema versioning;
- batch evaluator, state simulator, metrics, and comparison reports;
- failure taxonomy: retrieval, parse, grounding, policy, execution,
  verification.

Exit: model outputs can be compared by end state over the initial corpus.

### Phase 3 — Chordial shadow integration (1–2 weeks)

- integrate incoming messages and structured action context;
- implement native domain adapter and real resolvers;
- persist runs/frames with privacy minimization;
- shadow against current tool loop and reconciler;
- adjudicate and add failures to the corpus.

Exit: sufficient precision/recall evidence exists without new writes.

### Phase 4 — gated execution (1–2 weeks)

- enable low-risk explicit commands;
- add per-group clarification, confirmation, revision, cancellation, expiry;
- inject verified receipts/questions into persona responses;
- idempotency, optimistic concurrency, telemetry, and rollback flags.

Exit: Chordial uses the engine for task capture/schedule/complete with monitored
false-mutation thresholds.

### Phase 5 — domain expansion and extraction (2–4 weeks)

- add plans, goals, cycles, wins, and check-ins;
- remove duplicate work mutations from the free-form persona tool loop;
- stabilize embedded SDK and write a second toy/reference domain;
- only then extract a standalone package/service and optional MCP adapter.

Realistic total: **6–9 weeks** for a useful Chordial-native engine, **8–12
weeks** for a credible reusable framework with a second domain, documentation,
and stable extension contracts. A thin “message to Pydantic tool call” demo is
days, but it would not contain the innovation described here.

## 18. Release gates

Initial production execution should require, on a held-out suite:

- zero cross-tenant/prohibited mutations;
- >= 99.5% structural validity after no more than one format repair;
- >= 99% exact grounding for silent low-risk execution cases;
- false mutation rate below an agreed threshold (recommended < 0.5% overall
  and effectively zero for medium/high-risk commands);
- >= 95% end-state success for direct, explicit single actions;
- measured multi-action and correction performance reported separately;
- unnecessary clarification rate low enough not to damage normal chat;
- duplicate side-effect tests pass under message/tool retry;
- model/prompt rollback is one configuration change;
- shadow logs and evaluation cases are privacy-reviewed.

Thresholds should tighten with real data. Until a bucket passes, it remains
confirmation-only or shadow-only rather than being hidden in an aggregate.

## 19. Risks

### False confidence from valid structure

JSON Schema guarantees shape, not intent. Mitigation: semantic grounding,
state previews, policy, and end-state tests.

### Over-confirmation

A safe system that asks about every capture is unusable. Mitigation: risk- and
explicitness-based gates, independent action groups, and clarification UX
metrics.

### Under-action due to conservative policy

The companion may feel inattentive. Mitigation: measure missed actions,
specialized completion/accomplishment passes, and allow reversible explicit
actions once grounding is strong.

### Context pollution

Full chat history may distract the compiler. Mitigation: structured action
frames, bounded recent turns, entity anchors, and evaluation by context slice.

### Domain leakage into the engine

If task-specific status rules enter core code, extraction fails. Mitigation:
require a second reference domain before publishing the SDK.

### Framework overreach

Trying to replace agent runtimes, workflow engines, MCP, and business services
would dilute the product. Mitigation: keep the engine narrowly between speech
and approved domain commands.

### Evaluation contamination

Synthetic cases and self-labelled production logs can flatter the engine.
Mitigation: human adjudication, frozen holdouts, adversarial cases, and live
undo/correction signals.

## 20. Open decisions

1. Should the compiler run on every message or behind a high-recall action
   gate? Recommended: every message initially; optimize only after measuring.
2. Should safe commands execute before the persona responds? Recommended: yes
   when explicit and uniquely grounded, so the response can truthfully report
   the result.
3. Can one message partially execute while another group awaits clarification?
   Recommended: yes for explicitly independent groups; receipts must say what
   happened and what remains pending.
4. Should model confidence be exposed? Recommended: no raw number. Expose
   status, assumptions, and reasons for clarification/confirmation.
5. When should this become a separate repository/service? Recommended: after
   Chordial plus one second domain validate the abstraction.
6. Is the framework open source or a hosted control plane? Defer until the
   engine's defensible value is known; design the core as an embedded library
   and keep evaluation/observability deployable separately.

## 21. Recommendation

Build the Action Compiler as a bounded subsystem inside Chordial, not as a new
company-sized framework on day one.

Start with the evaluation corpus and end-state simulator. Then define semantic
contracts and AIR, run the compiler in shadow mode, and enable only explicit,
reversible, uniquely grounded task actions. Let Chordial's native backend
remain the authoritative executor and let the persona remain the conversational
surface.

If the same contracts, grounding, clarification, and evaluator work cleanly for
a second domain, extract them into a reusable engine. The novel value is not
another tool-calling SDK. It is a measurable semantic safety layer that turns
messy conversation into correct state transitions while knowing when not to
act.
