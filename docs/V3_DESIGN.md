# chordial v3 — the ensemble: multi-helper design doc

*status: PROPOSAL, revision 3 — all kickoff questions resolved (2026-07-09);
adds the proactive-outreach non-interaction guard. nothing here is implemented
yet.*

v3 turns chordial from one companion into a small cast of helpers — each with
its own baseline personality and specialty, but whose *identity* (name, form,
gender) emerges per-user through an introduction ritual — sharing a Telegram
group chat with the user, individually DM-able, all puppeteered by one master
orchestrator. This doc covers: Telegram feasibility, the persona system, the
onboarding/introduction overhaul, exemplars, the Notion redesign, and
orchestration + memory.

The good news up front: **the v2 architecture was accidentally built for
this.** The orchestrator/agent split, the author-attributed event log
(`ConversationEvent.author` already accepts arbitrary persona names), the
`Agent` protocol, and per-prefix prompt caching are all seams v3 extends
rather than replaces. Even the identity ritual is proven in miniature: Dain
named chordial "Ember the red panda" in v2 chat and it landed as a core
memory that has steered behavior ever since.

**Locked decisions from the kickoff Q&A:**
1. Working archetype names stay as internal ids; each helper's *user-facing*
   identity is formed naturally at introduction (user picks form/gender/none;
   helper proposes a name or the user picks one) — saved as core memory.
2. All existing Notion Projects migrate to Plans, each assigned to a helper
   (one-time migration; future Plans are born assigned).
3. **Cycles stay**, as the bi-weekly *balancing lever* across plans — pep and
   the orchestrator use them to check balance/importance, since Plans can be
   lofty multi-month arcs and time is finite.
4. Dain creates the Telegram group manually.
5. **DMs are truly private** (never rendered into siblings' context); the
   cross-helper channel is *memory* — important things get logged as shared
   memories with `created_by` attribution, so a sibling can naturally say
   "i heard from aria that…".
6. **Onboarding is overhauled** (storytelling intro, personality questions,
   representation ritual, meet-the-guides flow) and **launches in the same
   release as the helpers**. The v1-era onboarding code gets fully rewritten.
7. A sixth helper joins: a **writing assistant**, archetype id **poet**
   (Dain's pick — a step less musical than "lyric", but not tone-deaf to the
   theme).
8. Act-1 personality questions are **fully freeform** — no fixed
   questionnaire; the model weaves questions in and saves what it learns.
9. The migration's Area→helper mapping is approved as proposed.
10. **Helpers may initiate in DMs** — proactive nudges can land in a
    specialty helper's own DM, not just the group.
11. **Non-interaction guard, built early (phase 1):** no helper ever stacks
    more than ~3 unanswered proactive messages (with a crew-wide cap too);
    the gap between check-ins grows exponentially while the user stays
    silent; and the gate is pure code that runs *before* any generation —
    never spend tokens on a message that won't be sent.

---

## 1. Telegram group-chat feasibility

**Verdict: feasible, with one platform quirk that our architecture neutralizes.**

### The facts (verified against Telegram Bot API docs)

1. **One bot = one BotFather token.** Each helper gets its own bot account
   (`@chordial_bot`, `@tempo_bot`, …). A group supports up to 20 bots — plenty.
2. **Bots can NEVER see messages from other bots.** Hard platform rule,
   regardless of privacy mode — Telegram's anti-loop protection. This kills
   any design where bots observe each other *through Telegram*.
3. **Privacy mode:** by default a bot in a group only receives commands,
   @mentions of itself, and replies to its messages. Disabled via BotFather
   `/setprivacy` (re-add the bot to the group after changing), the bot
   receives all *human* messages.
4. **DMs are unaffected:** every bot always receives all private messages
   sent to it. So "message a helper individually" works exactly like today's
   single-bot DM flow, per bot.
5. **Bots cannot initiate DMs** until the user has `/start`-ed them. This
   shapes the meet-the-guides flow: each introduction rides a deep link
   (`t.me/<bot>?start=meet`) the user taps — which both opens the DM channel
   forever and triggers that helper's introduction. Identity is already
   solved: the user's numeric telegram id is the same across every bot's DM,
   and `PlatformIdentity` is keyed on `(platform, platform_user_id)` — so a
   helper bot recognizes an already-linked user with zero extra linking.

### Why fact 2 doesn't hurt us

The bots don't need to see each other on Telegram, because **they are all one
process and the event log is the shared reality.** When tempo speaks in the
group, the orchestrator wrote that message into `conversation_events` before
delivery; when aria is briefed next, tempo's line is in her event window.
Telegram is just the delivery surface — the same "many doors into one room"
property that made cross-platform conversations work in v2.

### Inbound plumbing

With privacy mode disabled on N bots in the same group, each human message
arrives **N times** — once per bot's polling stream. Dedupe centrally with a
small seen-set keyed on `(chat_id, message_id)` (identical across all N
update streams) in the shared inbound path:

```python
class UpdateDeduper:
    """N bots in one group each deliver every human message; keep the first."""
    def __init__(self, maxlen: int = 512):
        self._seen: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._maxlen = maxlen

    def is_duplicate(self, chat_id: int, message_id: int) -> bool:
        key = (chat_id, message_id)
        if key in self._seen:
            return True
        self._seen[key] = None
        if len(self._seen) > self._maxlen:
            self._seen.popitem(last=False)
        return False
```

### @-mention parsing

Telegram gives us `MessageEntity(type="mention")` spans — no regex guessing:

```python
def mentioned_helpers(message, handle_to_helper: dict[str, str]) -> list[str]:
    """resolve @-mentions in a group message to helper ids, in order."""
    out = []
    for ent in (message.entities or []):
        if ent.type == "mention":  # '@tempo_bot'
            handle = message.text[ent.offset + 1 : ent.offset + ent.length]
            if (helper := handle_to_helper.get(handle.lower())):
                out.append(helper)
    return out
```

(Users will usually @ the *handle*, not the chosen name — but the director
also gets the cast list with chosen names, so "ember, what do you think"
routes correctly even without a formal mention.)

### Addressing model (group vs DM)

| where the message arrived | who's summoned |
|---|---|
| DM to a specific helper's bot | that helper, always (a DM is a private conversation) |
| group, with @mentions | mentioned helper(s) must speak; director may add one more |
| group, no mention | director's call — usually 1 helper, occasionally 2 |

### What changes in code

- `_build_interfaces` builds **one `TelegramInterface` per enabled helper
  token** (config: `bots.yaml` mapping helper id → token + handle). All share
  one `UpdateDeduper` and one inbound callback.
- `UnifiedMessage` gains `chat_scope` (`'dm' | 'group'`), `group_chat_id`,
  `via_bot` (which helper's token received it), `mentioned` (parsed helpers).
- Outbound: `MessageRouter.deliver` gains a `speaker` param — it picks the
  interface for `(platform, speaker)`. Group delivery targets the group
  `chat_id`; DM delivery targets the user's telegram id via the speaker's bot.
- The group `chat_id` is stored once when Dain creates the group and adds the
  bots (a `PlatformIdentity` row with `platform='telegram-group'`, captured
  via a `/setup_group` command or the bot-added service message).
- Single-token config keeps working — one enabled persona = exactly v2
  behavior, which is the rollback story.

Discord note: Discord is *easier* (bots can see each other; one server channel,
N bot applications) — everything below is platform-agnostic, so Discord group
support falls out of the same design later. v3 targets Telegram first.

---

## 2. The persona system: baseline archetypes + emergent identities

### Two layers of "who is this helper"

**Layer 1 — the baseline archetype (repo YAML, frozen):** role, specialty,
voice *tendencies*, boundaries, tools, proactivity. This is what ships in
`src/personas/*.yaml` and forms the frozen, cache-stable persona block.
Deliberately written **form-agnostic**: no species, no gender, no name beyond
the internal id — because those belong to layer 2.

**Layer 2 — the emergent identity (per-user, runtime):** chosen at
introduction. The user decides form/gender/vibe (or "no preference" or
"please don't be a character"), the helper proposes a name from that (or the
user names them). Saved as **shared-visibility core memories** — shared so
siblings can say "ember mentioned…" — plus a denormalized `persona_name` on
`HelperState` (below) for the director's cast list and delivery labels.
*This is proven v2 behavior:* "chordial = Ember the red panda" already lives
as a core memory and steers the persona today. v3 just makes the ritual
deliberate and universal.

### The roster (archetype ids — user-facing names emerge per user)

| id | archetype | role | proactivity |
|---|---|---|---|
| `chordial` | the general | warm friendly presence, reaches out most, mornings, big-picture; *for Dain: Ember the red panda* | high (the default voice) |
| `tempo` | fitness coach | training plans, movement nudges, rest-day advocacy | medium |
| `aria` | musical advisor | practice plans, listening suggestions, music-project feedback | low-medium |
| `pep` | productivity cheerleader | cycles, focus sessions, balance-keeper, celebrates loudly | medium |
| `mochi` | emotional support animal | comfort, low-pressure presence, short soft lines, never assigns anything | low (but always answers when called) |
| `poet` | writing assistant | drafting, editing, journaling prompts, story/wordcraft; songwriting is a deliberate duet with aria (a fun sibling seam) | low-medium |

### PersonaCard

```python
@dataclass(frozen=True)
class PersonaCard:
    id: str                      # event-log author id: 'tempo'
    archetype: str               # 'fitness coach'
    telegram_handle: str         # 'tempo_bot' (no @)
    specialty: str               # one line, used by the director
    persona_block: str           # frozen system text: role, voice tendencies, boundaries
    intro_block: str             # how this one introduces itself (see §3)
    tools: list[str]             # tool allowlist (registry view)
    model: str | None = None     # override; None = Config.CHAT_MODEL
    effort: str | None = None
    proactivity: float = 0.5     # scheduler weight
```

Example `src/personas/poet.yaml` (abridged — note the form-agnostic voice):

```yaml
id: poet
archetype: writing assistant
telegram_handle: poet_bot
specialty: writing — drafting, revising, journaling, finding the right words
proactivity: 0.4
tools: [search_memories, save_memory, save_exemplar, notion_plans, notion_tasks, notion_wins]
persona_block: |
  you are the writing helper in a small crew who share a group chat with the
  person you support. you help them draft, revise, untangle, and finish —
  and you treat a messy first draft as a victory, not a liability.

  your voice tendencies: thoughtful, a little wry, precise without being
  fussy. you quote the user's own best lines back to them. you'd rather ask
  one sharp question than give three pages of notes.

  your crew: chordial (the friendly generalist), tempo (fitness), aria
  (music), pep (productivity), mochi (comfort). when writing turns into
  songwriting, loop aria in rather than going it alone.
intro_block: |
  when meeting someone new: explain you help with writing of every size —
  journal lines to long projects. ask what they're writing (or wish they
  were). then the representation ritual (see shared intro instructions).
```

### Rendering: persona block + identity zone

`PromptService` takes a `PersonaCard`; system block 1 is the frozen archetype
(one warm cache prefix per helper), and the **identity core memories render
into system block 2** (the per-user zone) exactly like core memories do
today — `always remember: to dain, you are ember, a red panda; warm amber
energy`. No new rendering machinery; the v2 pipeline already does this.

```python
class HelperAgent:                      # generalization of CompanionAgent
    def __init__(self, card: PersonaCard, agent_service, tool_registry):
        self.card = card
        self.name = card.id             # event-log author
        self.loop = agent_service
        self.registry = tool_registry.view(card.tools)
        self.prompts = PromptService(persona=card)
```

`CompanionAgent` becomes `HelperAgent(cards["chordial"], …)` — one class, six
instances. The curator stays as-is.

### HelperState: enablement + introduction progress

The meet-the-guides flow implies per-user helper enablement:

```python
class HelperState(Base):
    """per-(user, helper) relationship state: has this helper been met,
    is it enabled, and what identity did it take."""
    __tablename__ = 'helper_states'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    helper_id = Column(String, nullable=False)      # persona card id
    status = Column(String, default='not_met')      # not_met | introducing | active | declined | disabled
    persona_name = Column(String, nullable=True)    # chosen name ('Ember'); denormalized from the identity core memory
    persona_form = Column(String, nullable=True)    # 'red panda' | 'no character' | ...
    introduced_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('user_uuid', 'helper_id', name='uq_helper_state_user_helper'),
        {'sqlite_autoincrement': True},
    )
```

The director's cast = helpers with `status='active'` (chordial is always
active — it's the front door). A declined/disabled helper never speaks and
never gets scheduled; re-enabling is one chat sentence away ("could i meet
the fitness one after all?").

### Rendering a multi-party conversation

The event log already attributes every message. The prompt renderer changes
from binary user/assistant to **speaker-labeled turns**: the *acting* helper's
own messages render as `assistant` turns (verbatim, no prefix — the
timestamp-echo lesson stands), and **siblings' group messages fold into
user-side turns with a speaker label**, the same pattern action blocks
already use:

```
[tempo: nice, logging that run 🏃]
[current time - …]
@aria what should i practice tonight?
```

This keeps each helper's history prefix append-only and cache-stable *from
its own point of view* (sibling lines are frozen bytes, like action lines).

---

## 3. The introduction overhaul (launches WITH the helpers)

The v1-era onboarding (`onboarding_service.py` state machine) is retired and
rewritten. The new flow is **agent-driven storytelling with a thin state
spine**: the prose, pacing, and interpretation are the model's job (v2
already proved it interprets identity intent well); code only tracks *which
stage* each helper relationship is in (`HelperState.status`) and provides
tools to advance it. No brittle question-by-question state machine.

### Act 1 — entering the forest (first contact, chordial's bot)

A brand-new user's first `/start` triggers chordial in **introduction mode**:
the briefing carries `kind='introduction'` and the persona's `intro_block` +
shared intro instructions ride in the volatile turn. The narrative frame:

> *you follow a half-remembered path into the chordial forest. the trees hum
> quietly, like they're tuned to something. by a lantern-lit clearing, a
> friendly figure looks up — as if it was waiting for you.*

Then, conversationally (not as a form):
1. **name** — what should they call you?
2. **a few personality questions** — fully freeform (locked): no fixed
   questionnaire, the model weaves 3–5 questions in naturally (how do days
   usually feel? what does "productive" mean to you? encouragement: loud or
   quiet?) and follows the conversation where it goes. Answers are saved as
   ordinary memories/preferences via existing tools — *this is calibration
   data for every helper, so these save as shared.*
3. **the representation ritual** — "how would you like me to appear to you?"
   User picks form/gender/vibe, *or* "surprise me", *or* "no character,
   please" — all three land as a shared core memory; the helper proposes a
   name from the chosen form (or the user names them).
4. The helper calls a new tool:

```python
@tool(terminal=False, record_event=True)
async def complete_introduction(
    user_uuid: str,
    helper_id: str,
    persona_name: str | None,       # None = user declined a character
    persona_form: str | None,
    accepted: bool,                 # False = user declined this helper entirely
) -> str:
    """stamp HelperState (status=active|declined, names, introduced_at).
    identity core memories are saved separately via save_memory - this just
    records the relationship state the director reads."""
```

### Act 2 — meeting the other guides

Once chordial's intro completes, it offers: *"a few other guides live in this
forest — each helps with something different. want to meet any of them?"*
Then, per accepted guide, chordial sends the deep link
(`t.me/tempo_bot?start=meet`). Tapping it:

1. opens that bot's DM channel (satisfying Telegram's can't-DM-first rule,
   permanently — this is why the flow rides deep links),
2. the `/start meet` payload + already-known telegram id flips
   `HelperState.status` to `introducing`,
3. the helper introduces itself **in DM, in its own voice** (its
   `intro_block`): what it helps with, its vibe, one good question —
4. then the same representation ritual, ending in `complete_introduction`
   (accepted or declined — declining is warm and consequence-free).

**Existing users (Dain) get Act 2 standalone:** chordial announces the crew
in conversation and offers introductions — same flow, no forest prologue
(he's already home).

### What replaces `onboarding_service.py`

- The old service is deleted. `ChatService` loses its onboarding state
  machine; a new-user first-contact simply produces an `introduction`
  stimulus.
- The orchestrator routes `introduction` stimuli to the relevant helper with
  an intro briefing; `HelperState.status` is the only persisted state.
- All copy lives in persona YAML (`intro_block` + one shared
  `intro_instructions` text), not in Python string constants.
- Mid-intro interruptions are fine by construction: the model has the event
  window and the status flag; there is no rigid step counter to desync.

---

## 4. Exemplars: example interactions the user loved

When Dain loves an interaction, it should become durable steering. These are
**runtime-grown** (unlike persona cards), so they live in the DB and render
into system block 2 (the per-user zone — changes rarely, sits behind the
frozen persona, exactly where core memories already go).

```python
class Exemplar(Base):
    """a captured interaction pattern the user explicitly liked. rendered as
    few-shot guidance in the owning helper's per-user system zone."""
    __tablename__ = 'exemplars'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    helper_id = Column(String, nullable=True)   # None = applies to every helper
    situation = Column(String, nullable=False)  # when this pattern applies
    exchange = Column(String, nullable=False)   # the (condensed) exchange itself
    why_loved = Column(String, nullable=True)   # what the user said about it
    source = Column(String, default='user_praised')  # user_praised | hand_authored
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
```

Capture is a tool, not a pipeline: every helper gets `save_exemplar`. When the
user says "i love when you do that" / "more of this please", the responding
helper condenses the exchange (situation + a short quoted excerpt + the
user's reaction) and saves it. Rendered like:

```
interactions dain has loved (match the pattern, don't repeat the words):
- when he finishes something he's downplaying → you replayed his own words
  back as a win list. he said "ok that actually made me feel accomplished"
```

Cap the rendered set (say, 8 most recent active per helper) and let the
curator merge/expire them like memories. Hand-authored seed exemplars ship in
each persona's YAML (`seed_exemplars:`) for day-one behavior.

---

## 5. Notion redesign: from task tracker to shared workspace

Today's dainframe (Tasks / Projects / Cycles) tracks *work*. v3's schema
tracks *the collaboration*: helpers set plans with the user, break them into
goals, schedule flexible days, and — critically — build a durable record of
accomplishment to counter the "i haven't really done anything" spiral.

### The shape

```
Plans ──< Goals ──< Tasks >── Cycles     (Cycles survive as the balancing lever)
  │                   │
  └───< Wins >────────┘                  Check-ins ──> Plans (optional relation)
```

**1. Plans** — a helper-led arc (evolves Projects; can be lofty, multi-month).
| property | type | notes |
|---|---|---|
| Plan | title | e.g. "couch to 5k", "finish the EP", "write the novella" |
| Helper | select: chordial/tempo/aria/pep/mochi/poet | who stewards it |
| Status | status: Proposed / Active / Paused / Complete | proposed = pitched, not yet agreed |
| Why | rich_text | the user's own motivation, in their words — helpers quote it back |
| Success looks like | rich_text | concrete finish line, negotiated up front |
| Horizon | date (range) | soft, renegotiable — months is fine |
| Cadence | select: daily / weekly / loose | how often the helper checks in on it |

**2. Goals** — milestones inside a plan.
| property | type |
|---|---|
| Goal | title |
| Plan | relation → Plans |
| Status | status: Not started / In progress / Done / Renegotiated |
| Target | date |
| Done means | rich_text (concrete criteria — the anti-vagueness field) |

**3. Tasks** — evolves the existing db in place (existing tooling and the
completion reconciler keep working). Added properties:
| new property | type | why |
|---|---|---|
| Goal | relation → Goals | ladders every task up to meaning |
| Helper | select | who assigned/owns the nudging |
| Window | select: morning / afternoon / evening / anytime | day-scheduling without rigid clock times |
| Reschedules | number | bumped each time it slips — helpers see the count and *gently* renegotiate at 2–3, never shame |
*(existing Status/Priority/Scheduled/pom estimate stay; the **Sprint relation
stays** — it's how tasks join Cycles.)*

**4. Cycles** — **kept, promoted to the balancing lever.** Bi-weekly, drawing
tasks from across many Plans. Because Plans can be months long, the Cycle is
where "i only have so much time" gets enforced:
- at each cycle boundary, **pep runs the balance pass**: proposes the next
  cycle's task mix across active Plans, flags a Plan hogging the cycle or one
  starving, and negotiates trade-offs with the user ("aria's EP got 70% of
  last cycle — protect two evenings for poet's novella this time?").
- the orchestrator's scheduled ticks weight helper proactivity by cycle
  content: a helper whose plan has tasks in the active cycle gets more turns.
- schema: existing Cycles db + one added property, `Focus` (rich_text — the
  cycle's negotiated balance statement, written by pep).

**5. Wins** — the reinforcement ledger; **the load-bearing anti-diminishment db.**
| property | type | notes |
|---|---|---|
| Win | title | concrete, past-tense: "ran 2k without stopping" |
| Date | date | |
| Helper | select | who witnessed/logged it |
| Plan | relation → Plans | optional |
| Evidence | rich_text | the user's own words at the time — quoted back later verbatim, which is much harder to dismiss than a paraphrase |
| Weight | select: spark / solid / milestone | so digests can headline the big ones |

Helpers log wins *liberally* (finishing anything, showing up at all, a hard
conversation, resting on purpose). The reconciler pattern extends here: a
cheap pass that notices accomplishments mentioned in passing and logs them.

**6. Check-ins** — the shared daily journal.
| property | type | notes |
|---|---|---|
| Check-in | title | auto: "tue jul 09 — morning" |
| Date | date | |
| Kind | select: morning / evening / adhoc | |
| Energy | select: low / ok / good / great | asked, never demanded |
| Notes | rich_text | what the user said about the day |
| Plans touched | relation → Plans | |

### How the day flows through this schema

- **morning:** chordial assembles today from Task `Scheduled` + `Window`,
  framed as a *menu, not a contract* ("here's the shape of today — what feels
  right first?"). Writes a Check-in row.
- **during:** helpers nudge per plan `Cadence` (weighted by the active
  cycle); completions mark Tasks done AND log Wins where warranted; slips
  bump `Reschedules` and trigger renegotiation ("shrink it or move it?")
  instead of a guilt loop.
- **evening:** reflection check-in + **the wins replay** — the digest's
  `done_today` + today's Wins rows, quoted with Evidence. The direct counter
  to accomplishment-diminishing: the record is in the user's own words from
  the moment it happened.
- **bi-weekly:** pep's cycle-boundary balance pass (above).

### One-time migration: Projects → Plans

All existing Projects migrate, each assigned to a helper. Since this is a
one-time human-in-the-loop event (future Plans are born assigned), the script
is deliberately interactive:

1. `scripts/migrate_projects_to_plans.py --dry-run` proposes assignments from
   the existing `Area` multi-select — proposed mapping:
   `Health & Fitness → tempo`, `music → aria`, `Writing → poet`,
   `Code / job search / content creation → pep`,
   `Personal / cooking / Art / Other → chordial` —
   and prints a review table (project → helper, status mapping, why).
2. Dain edits the table (a simple yaml the script emits), reruns with
   `--apply`. `Why` / `Success looks like` start blank — each steward helper
   raises them *in conversation* during its first check-in on an inherited
   plan ("i've taken over 'couch to 5k' — what made you start it?"), which
   doubles as a natural way for the new helpers to earn context.
3. Projects db is left read-only-by-convention afterwards and retired once
   nothing references it.

### Code impact

`schema.py` grows `plans_db()/goals_db()/wins_db()/checkins_db()`,
`build_plan_properties(...)` etc. — same builder/formatter pattern, one file.
New tools (`notion_plans`, `notion_wins`, …) slot into the registry; persona
cards allowlist them (mochi gets read-only wins + check-ins, no task tools —
the ESA never assigns work). The agenda snapshot digest v2 adds: active plans
per helper, the active cycle's `Focus` statement, wins-this-week count, and
today's window layout. `scripts/setup_notion_v3.py` (mirroring
NOTION_SETUP.md) creates the new databases.

---

## 6. Master orchestrator: the director, delivery, privacy, and memory

### The director (the one genuinely new piece of logic)

`Orchestrator._select`/`_brief` become AI-driven for group-chat stimuli. A
**utility-model (haiku) call** — cheap, fast, `thinking=False`/`effort=None`
per the standing invariant — produces a *script*:

```python
@dataclass
class ScriptLine:
    speaker: str                 # helper id
    cue: str                     # one-line direction ("react briefly to tempo's plan")
    style: str = "full"          # 'full' | 'brief' (a reaction, one or two lines)
    venue: str = "group"         # 'group' | 'dm' — proactive nudges may land in the speaker's own DM

@dataclass
class Script:
    lines: list[ScriptLine]      # ordered; delivered sequentially
```

Director rules (hard constraints applied in code, not left to the model):
- DM stimulus → script is exactly `[the DM'd helper]`; no director call at all.
- @mentioned helpers are always in the script, first, in mention order.
- Cast = `HelperState.status == 'active'` only.
- Group, no mention → director picks **1 primary** (specialty match), and may
  add **at most 1** brief reactor. Six bots piling on is noise; scarcity
  keeps interjections delightful.
- `scheduled_tick` → **the ProactivityGate runs first** (pure code, §"the
  non-interaction guard" below); only helpers it clears are candidates. The
  director then picks who reaches out — weighted by persona `proactivity`
  (chordial most often) × active-cycle involvement — and picks the venue:
  specialty plan-cadence nudges default to that helper's own **DM**; general
  presence, morning menus, and anything multi-helper go to the **group**.
- Output is JSON, validated; malformed/unknown/disabled speakers → fall back
  to `[chordial]`. The director can never break the conversation.

The director's system prompt renders the cast **with chosen identities**
(from `HelperState.persona_name`), so "ember, thoughts?" routes to chordial:

```
cast (id — goes by — specialty):
- chordial — "ember" (red panda) — friendly generalist, default voice
- tempo — "?" — fitness: training, movement, recovery
...
rules: pick 1 speaker, or 2 when a brief reaction adds warmth. prefer the
specialty match. no second speaker for transactional messages.
respond ONLY with json: {"lines":[{"speaker":"tempo","cue":"...","style":"full"}]}
```

### Sequential delivery

`Orchestrator.handle` loops the script; **each line is briefed *after* the
previous line was recorded**, so speaker 2 genuinely reacts to speaker 1
(their line is in the event window). Delivery goes out per line through the
router (v2's `deliver` hook already enables this), with per-bot typing
indicators and a natural gap:

```python
async def handle(self, stimulus: Stimulus) -> None:
    log = EventLog(stimulus.user_uuid)
    # …record inbound message (unchanged)…
    script = await self._direct(stimulus, log)          # rules + haiku director
    for line in script.lines:
        agent = self.agents[line.speaker]
        briefing = await self._brief(agent, stimulus, log, cue=line.cue, style=line.style)
        outcome = await agent.act(briefing)
        await self._record(log, agent, outcome, stimulus)   # unchanged
        if outcome.text:
            await self.deliver_as(line.speaker, stimulus, outcome.text)
            if line is not script.lines[-1]:
                await asyncio.sleep(random.uniform(2.0, 5.0))   # let it breathe
```

(The `cue` rides into the volatile current turn of the speaker's prompt —
after every cache breakpoint, so it costs nothing.)

### DM privacy: private transcripts, shared gossip

**DM conversations are truly private.** Every event gets a scope
(`metadata={'scope': 'dm', 'with': 'aria'}` vs `{'scope': 'group'}`), and the
briefing window for helper X = **group-scope events + X's own DM-scope
events**. Siblings never see the transcript; the director doesn't either (it
never needs to — DMs bypass it).

The cross-helper channel is **memory**: important things from a DM get saved
as *shared* memories (existing `save_memory` habit), and shared memories
carry `created_by`. Rendering makes attribution natural — a shared memory
created by a sibling renders with its source:

```
- (from aria) dain is writing a song for his sister's wedding in september
```

…which is exactly what lets tempo say *"i heard from aria that you've got a
wedding song cooking — want training scheduled around studio days?"* The
persona blocks carry one line of guidance: *"what you learn in DMs is
private; save what your crew genuinely needs as a shared memory — that's how
you talk to each other."*

One consequence to accept: `EventLog.recent()` grows a scope filter, and the
scheduler's `active_platform()` logic learns that a DM reply belongs to the
DM'd helper only.

### Proactive outreach and the non-interaction guard

Helpers **can initiate in their own DMs** (Dain's call) — tempo's morning
movement nudge lands in tempo's DM; the group is for shared presence. With
six voices allowed to reach out, the guard against becoming a noisy app is a
hard requirement, and it's **built in phase 1, as pure code that runs before
any model call** — a denied tick costs one DB read and zero tokens. Never
generate a message just to throw it away.

Three stacked rules, all computed from the event log (proactive = agent
`kind='message'` with `message_type='scheduled'`; `note` rows never count,
per the existing invariant):

1. **per-helper cap:** a helper with `GATE_PER_HELPER_CAP` (3) unanswered
   proactive messages since the user's last message goes silent.
2. **crew cap:** `GATE_CREW_CAP` (4) unanswered proactive messages *across
   all helpers and venues* silences the whole crew — being ignored is a
   signal to everyone, not a budget each helper spends separately.
3. **exponential backoff:** each unanswered proactive message doubles the
   required quiet period before the next one, from any helper:
   `GATE_BASE_INTERVAL * 2^unanswered` (3h → 6h → 12h → 24h → capped).

Any user message, anywhere, resets all three. The whole thing is ~40 lines:

```python
@dataclass
class OutreachDecision:
    allowed: bool
    reason: str                      # logged, and later useful to the director

class ProactivityGate:
    """pure event-log arithmetic, consulted by the scheduler BEFORE the
    director or any generation. an ignored crew goes quiet; one user
    message resets everything."""

    def check(self, log: EventLog, helper_id: str) -> OutreachDecision:
        events = log.recent(Config.MAX_HISTORY_MESSAGES)
        unanswered = _proactive_since_last_user(events)   # oldest→newest

        if len(unanswered) >= Config.GATE_CREW_CAP:
            return OutreachDecision(False, "crew cap: quiet until they speak")
        if sum(e.author == helper_id for e in unanswered) >= Config.GATE_PER_HELPER_CAP:
            return OutreachDecision(False, f"{helper_id} cap: their turn is over")

        required = Config.GATE_BASE_INTERVAL * (2 ** len(unanswered))
        anchor = unanswered[-1].created_at if unanswered else _last_user_at(events)
        if anchor and utc_now() - anchor < required:
            return OutreachDecision(False, f"backoff: {required} not yet elapsed")
        return OutreachDecision(True, "clear")
```

This subsumes the v2 fixed-interval scheduler behavior (and the never-built
haiku "check-in gate" from the v2 backlog): the scheduler tick becomes
*gate → director → generate → deliver*, and the first arrow is free.

### Memory: shared pool + per-helper privates

⚠️ **This revises the pre-kickoff locked decision** ("shared only, no per-bot
pools") — Dain's kickoff specifies both. One table, two columns, no
partitioning:

```python
# additions to Memory:
created_by = Column(String, default='chordial')   # helper id (attribution, always)
visibility = Column(String, default='shared')     # 'shared' | 'private'
```

- **shared** (the default): visible to every helper's search + core-memory
  rendering, attributed when created by a sibling (the gossip mechanism
  above). Facts about the user's life — and all identity/personality-
  calibration memories from introductions — belong here.
- **private**: visible only to `created_by`. Relationship texture — an inside
  joke with mochi, how the user likes tempo to push.
- Queries filter `(visibility == 'shared') | (created_by == helper_id)`. The
  curator curates per-scope (never merges a private memory into a shared one).
- `UsageLog`/`AgentTrace` gain `helper_id` for per-helper cost visibility.

### Config & migration summary

- `bots.yaml`: per-helper telegram token + handle; helpers absent from it
  simply don't exist at runtime (global kill-switch, distinct from per-user
  `HelperState`).
- Alembic migrations (all additive, zero-downtime like v2's):
  `memories.created_by`/`visibility` (backfill `'chordial'`/`'shared'`),
  `exemplars`, `helper_states`, `usage_log.helper_id`,
  `agent_traces.helper_id`, telegram-group identity row.
- `conversation_events` needs **no schema change** — author was built for
  this; scope rides in `event_metadata`.

---

## 7. Phasing

Onboarding launches *with* the helpers (Dain's call), so the release train is
phases 1–3 together, with 4–5 fast-follow:

| phase | what | why this order |
|---|---|---|
| **1. persona infrastructure + the gate** | `PersonaCard` + YAML loader (6 cards); `PromptService` takes a persona; `CompanionAgent` → `HelperAgent`; memory `created_by`/`visibility` + `helper_states` migrations; **`ProactivityGate` wired into the existing scheduler** | zero behavior change except the gate — which immediately improves v2's fixed-interval check-ins, and MUST exist before six helpers can speak ("have this guard early") |
| **2. multi-bot telegram + introductions** | N tokens/interfaces, dedupe, mention parsing, `deliver_as`, DM scope + privacy filter; **onboarding rewrite** (forest intro, representation ritual, meet-the-guides deep links, `complete_introduction`); **rules-only director** (mentions + DM routing + chordial default) | the ensemble becomes real, meetable, and testable with deterministic routing — no AI director in the failure surface yet |
| **3. the AI director** | haiku script generation for unaddressed group messages + scheduled ticks (gate-cleared candidates only); venue-aware proactive delivery (DM nudges vs group presence); sequential multi-speaker delivery with reaction briefings; cast list with chosen identities | the magic layer, added once the plumbing is boring. **← launch line: 1+2+3 ship together** |
| **4. notion v3** | Plans/Goals/Wins/Check-ins dbs + Cycles `Focus`, schema.py v2, new tools, digest v2, wins-replay evening flow, Projects→Plans interactive migration, pep's cycle balance pass | independent of the launch train (chordial alone could use it); biggest product value |
| **5. exemplars + polish** | `exemplars` table + `save_exemplar` + rendering; YAML seed exemplars; curator learns exemplars; per-helper cost dashboards | steering quality, once there are helpers to steer |

Rough sizes: 1 and 5 are small; 2 is now the big one (multi-bot plumbing +
the onboarding rewrite); 3 is medium but needs prompt iteration; 4 is big but
independent.

## 8. Resolved questions + remaining tunables

All kickoff questions are resolved (see the locked-decision list up top):
freeform personality questions, `poet`, the migration mapping, and proactive
DMs are all go. On declined helpers ("no preference"), the design picks:
**don't add declined bots to the group** — fewer duplicate update streams to
dedupe, and enabling a guide later naturally includes adding its bot then.

Remaining knobs are config, not questions — ship with these defaults and tune
by feel:
- `GATE_PER_HELPER_CAP = 3` unanswered proactive messages per helper
- `GATE_CREW_CAP = 4` unanswered proactive messages across the whole crew
- `GATE_BASE_INTERVAL = 3h`, doubling per ignored message (3h → 6h → 12h → 24h)
- inter-speaker delivery gap 2–5s; max 2 speakers per scripted turn

## Sources (telegram facts)

- [Telegram Bots FAQ](https://core.telegram.org/bots/faq) — bots never see other bots' messages; privacy-mode behavior
- [Telegram Bot Features](https://core.telegram.org/bots/features) — privacy mode, group behavior
- [TeleMe: group privacy mode](https://www.teleme.io/articles/group_privacy_mode_of_telegram_bots?hl=en) — /setprivacy + re-add requirement
