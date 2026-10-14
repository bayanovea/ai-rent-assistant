---
name: messages
description: Sync the Messenger conversations with owners/agents, send the first message (viewing + pets), classify the replies — rent-assistant messages sync → classify-batch → classify-apply → export, or messages send on the user's choice. Triggers ONLY on an explicit request: "check the messages", "write to the shortlist owners", "what is in the messenger", "/messages". A non-empty queue in `messages status` is not a reason by itself — the sync goes to the user's real Messenger, the sending goes to real people.
---

# Messenger conversations

This is the only pass that touches the user's real Messenger — other
people read what is sent here. Work at a measured pace, show every message
in full before sending, and send nothing without the user's explicit
confirmation of the whole batch at once.

**Only you classify the replies — the code computes the conversation
state.** `messages.derive_state` in `messages.py` is the only place where
facts turn into `state`/`needs_action`. Your result is facts only:
`pets_answer`, `viewing_proposed`, `proposed_times`, `listing_gone`,
`unusual`. Do not try to decide yourself what goes into the "Needs me"
column — that is `derive_state`'s job, and with the same facts the output
is always the same.

**There is no fan-out, on purpose.** Threads are short (a dozen lines of
text), unlike photos or a web search per building — pushing them through
subagents just to hide the context would be pure overhead. Read the batch
and write the result yourself, without `Task`/`Agent`.

## Part A — sync and classify the replies

### 1. Sync

```sh
rent-assistant messages sync
```

Pulls the Marketplace folder from the real Messenger (headless) and never
opens a thread without a positive date at or after the cutoff in
`messages.py` — see `CLAUDE.md`. At the end: how many synced, how many
skipped as old, how many with a new unclassified reply, how many not
matched to a listing (`no_listing_match` — usually means the seller took
the listing down: the card is no longer in the thread).

If the command stopped on a message about the end-to-end encryption PIN,
that is not a bug, that is Messenger's real protection. **Do not try to
get around it yourself.** Tell the user: a headed session is needed, they
enter the PIN by hand (see `docs/messages.md`), and stop — there is no way
forward without them.

### 2. Build the classification batch

```sh
rent-assistant messages classify-batch
```

Prints a path like `.../ai/messages-batch-YYYYMMDD-HHMMSS.json`. Empty —
say so and stop; part B does not belong to this step.

### 3. Read and classify

Read the whole batch. Each element is `{thread_id, reply_hash, fb_id,
listing_title, messages}`. `messages` is a list of `{direction, text}` in
chronological order; `direction: "out"` is what the user wrote (or the
auto-message), `"in"` is the reply from the seller/agent.

For each element determine the facts **only from the text of the `in`
messages**, inventing nothing:

- `pets_answer`: `"allowed"` / `"not_allowed"` / `"unclear"` — an explicit
  answer to the pets question. No question or no answer — `"unclear"`.
- `viewing_proposed`: `true` if the seller named a time or asked when it
  is convenient to view. `proposed_times` — a list of what they named
  (free text, as in the message, do not parse into dates).
- `listing_gone`: `true` if the reply says the place is already let or
  taken down.
- `unusual`: `true` for anything that does not fit "yes/no about pets" and
  "proposed a time" — a request for extra information (such as "send a
  photo of the dog"), a request to move to WhatsApp, an off-topic counter
  question, a refusal for another reason. This is what really needs to be
  shown to the user.

Do not set several mutually exclusive flags thoughtlessly — if the reply
both names a time and says "but no pets", set both
(`pets_answer: "not_allowed"`, `viewing_proposed: true`); `derive_state`
decides the priority itself (see `messages.derive_state` —
`unusual`/`listing_gone` outweighs everything, `not_allowed` outweighs a
proposed time).

Write the result into a JSON file next to the batch, an **array** of
objects:

```json
[
  {
    "thread_id": "8000000000000001",
    "reply_hash": "<verbatim from the batch>",
    "facts": {
      "pets_answer": "unclear",
      "viewing_proposed": false,
      "proposed_times": [],
      "listing_gone": false,
      "unusual": true
    },
    "summary": "Seller asked for a photo of the dog"
  }
]
```

`reply_hash` — copy verbatim from the batch, do not recompute: `apply`
checks it against the current state of the thread and skips the entry if
the thread received a new reply after the batch was built.

### 4. Apply

```sh
rent-assistant messages classify-apply <path to the result>
```

### 5. Export and report

```sh
rent-assistant export
```

Report briefly: how many were classified, and for each one that landed in
needs-action — id, title, reason (`viewing_time` / `pets_declined` /
`unusual_reply` / `no_listing_match`), the last reply. For `pets_declined`
and `listing_gone`-like cases offer a ready command, do not run it:

```
rent-assistant reject <fb_id> --reason "owner: no pets"
```

The decision to reject or not is the user's, not yours: offer the command
and stop, do not execute it yourself.

## Part B — the first message

The user describes the selection in words ("the whole shortlist",
"shortlist and longlist in Bang Tao", a list of ids). Translate it into
`rent-assistant messages send` flags:

- "the whole shortlist" → `--status shortlist`
- "shortlist and longlist" → `--status shortlist --status longlist`
- "in Bang Tao" / "in district X" → add `--district "X"`
- a list of ids → `--ids <id1> --ids <id2> ...` (ignores `--status`)

### 1. Show the drafts

```sh
rent-assistant messages send --status shortlist
```

Without `--apply` the command sends nothing — it only prints every draft in
full (who, listing, text) and the list of those already skipped (already
written to, already in the log, a conversation already exists). Show the
user exactly what the command printed — do not retell, do not shorten the
message texts.

### 2. Confirmation — required for the whole batch

Ask the user (`AskUserQuestion` or an explicit question in text) whether to
send the drafts shown — one confirmation for the whole batch, not one per
message. Without an explicit "yes" do not move to the next step.

### 3. Send

```sh
rent-assistant messages send --status shortlist --apply --yes
```

`--yes` skips the internal `click.confirm` — the confirmation was already
obtained in the previous step, from the user, not from the command. The
command itself keeps the jitter between sends and stops at `--cap` (10 per
run by default); if the session breaks (login expired), the messages
already sent are recorded in the log and in the stage "Messaged", and a
re-run does not duplicate them.

### 4. Report

How many were sent, how many failed (and what to do with them — usually
just re-run `messages send` with the same selection; the ones already sent
skip themselves). The stage "Messaged" and the "Stage" column appear in
the export automatically — remind the user to run `rent-assistant export`
if you have not run it at this step yet.
