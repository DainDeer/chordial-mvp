# chordial.ai

**chordial** is an ai companion chatbot designed for proactive, scheduled check-ins with users. it currently supports discord as a platform interface and uses openai as the ai backend.

### core features

#### multi-platform architecture
- abstract base classes for platform interfaces (`BaseInterface`) and ai providers (`BaseAIProvider`)
- currently implemented: discord bot interface
- designed to easily add new platforms (telegram, web, etc.)

#### intelligent scheduling system
- proactive check-in messages at configurable intervals (default: 60 minutes)
- quiet hours support (default: 9pm-8am) — no scheduled messages during these times
- smart backoff: if a scheduled message is ignored, waits 24 hours before trying again
- only sends scheduled messages to users who have completed onboarding

#### user onboarding flow
- two-step onboarding for new users:
  1. asks for preferred name
  2. asks for something important to remember (stored as a "core memory")
- creates a persistent user record linked across platforms via uuid

#### memory system
- three memory types: `PREFERENCE`, `FACT`, `EPISODIC`
- three sources: `USER_EXPLICIT`, `AI_INFERRED`, `SYSTEM_GENERATED`
- core memories (always included in prompts) vs. regular memories
- keyword-based search, weighting system, and ttl support for temporary memories
- access tracking (counts and timestamps)
- memories are injected into the ai system prompt

#### message compression
- uses a secondary ai model (gpt-3.5-turbo by default) to compress older messages
- hybrid context strategy: keeps n most recent messages full, older ones compressed
- stores compression stats (ratio, original/compressed lengths)
- configurable minimum length threshold before compression kicks in

#### temporal context awareness
- relative time strings for messages ("5 minutes ago", "yesterday afternoon", "on june 15th")
- time-of-day awareness (morning/afternoon/evening/night)
- special context for certain times (friday afternoon vibes, monday morning, late night)
- context injected into prompts so the ai can respond naturally to time

#### conversation management
- in-memory conversation cache with configurable limits
- database persistence via sqlalchemy (sqlite by default)
- automatic cleanup of old messages
- unified message format across platforms

#### prompt engineering
- custom prompt service with a defined personality ("cozy, friendly, lowercase")
- separate prompt builders for conversations vs. scheduled messages
- optional prompt logging to files for debugging/tuning

### database models
- `User` — core user record with preferences (name, timezone, personality, schedule settings)
- `PlatformIdentity` — links platform-specific ids to internal uuids
- `ConversationHistory` — raw message storage
- `CompressedMessage` — compressed versions of messages
- `Memory` — persistent memories about users

### configuration (via environment variables)
- discord token and target user
- openai api key and model selection
- scheduling intervals, quiet hours, backoff delays
- compression thresholds
- feature flags (enable/disable discord, web)

### project structure
```
├── main.py                    # entry point
├── config.py                  # environment config
├── src/
│   ├── database/              # sqlalchemy models & db setup
│   ├── managers/              # conversation, user, memory managers
│   ├── models/                # message, unifiedmessage data classes
│   ├── providers/
│   │   ├── ai/                # openai provider (and base class)
│   │   └── platforms/         # discord bot (and base class)
│   ├── services/              # chat, scheduler, compressor, onboarding, prompt services
│   └── utils/                 # temporal context, string utils, context builder
```
