# Telegram Setup

How to give chordial her second door. One-time setup, ~5 minutes.

## 1. Create the bot with @BotFather

1. In Telegram, message **@BotFather** and send `/newbot`.
2. Pick a display name (e.g. `chordial`), then a unique username ending in
   `bot` (e.g. `chordial_dm_bot`). BotFather replies with the **API token** —
   treat it like a password.
3. `/setjoingroups` → **Disable**. chordial is a personal DM companion; this
   closes the group-add surface entirely. (Privacy mode doesn't matter for
   DMs — bots always see direct messages.)
4. Optional cosmetics: `/setdescription`, `/setabouttext`.

## 2. Configure chordial

Add to `.env`:

```
TELEGRAM_TOKEN=123456:ABC-your-token-here
TELEGRAM_BOT_USERNAME=chordial_dm_bot   # no @ - used to build deep links
ENABLE_TELEGRAM=true
```

Restart the service. Startup fails loudly if the flag is on but token or
username is missing, and logs a warning if the username doesn't match the
token's actual bot (which would break link-code deep links).

## 3. Link your account

You don't need to do anything special on Telegram first — the link flow
handles Telegram's "user must message the bot before it can reply" rule
automatically:

1. On Discord, ask chordial: *"can we chat on telegram?"*
2. She replies with a one-time code and a tappable `t.me/...` link.
3. Tap the link → Telegram opens the bot → hit START. Done — the `/start`
   both introduces you to the bot and redeems the code in one step. (Pasting
   the bare code as a message works too.)

Codes expire after 15 minutes (`LINK_CODE_TTL_MINUTES`) and are single-use.
Strangers who find the bot get a polite one-liner and are never onboarded.

## 4. Dev bot (important!)

**Never run two chordial processes against the same Telegram token** — Telegram
allows exactly one poller per token; the second gets `409 Conflict` and both
misbehave. Mirror the Discord dev-bot pattern: create a **second** bot via
BotFather for local testing, and put its token/username in your local `.env`
(alongside your dev `DATABASE_URL` and dev Discord token).

## Behavior notes

- One conversation across platforms: chordial has the same memory and context
  on Telegram as on Discord.
- When you switch platforms mid-conversation, she leaves a one-time
  `*(pssst — we're chatting over on telegram now...)*` note on the platform
  you left, and won't repeat it until you talk there again.
- Scheduled check-ins go to whichever platform you used most recently; if
  that link dies (e.g. you block the bot), she falls back to the other one.
- If you block/stop the Telegram bot, that link is deactivated automatically
  on the next send attempt; ask for a fresh link code to reconnect.
