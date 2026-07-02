# Yor chat-exporter userbot

A tiny **Telethon userbot** that runs on *your* Telegram account. You DM it a
chat id (in your Saved Messages by default) and in seconds it extracts the whole
chat history and writes a **feed-ready datasheet** the yor-assistant can eat —
plus a structured JSONL for any other AI pipeline.

```
you (Saved Messages):  .export -1001234567890
        │
        ▼
userbot iter_messages(entity)  ── fast, batched history pull
        │
        ├─►  <FEED_DIR>/chat_<id>_<slug>.md      ← feed datasheet (point FEED_DIR at knowledge/feed to auto-index)
        └─►  <EXPORT_DIR>/chat_<id>_<slug>.jsonl ← structured records
```

## Why this format?

The yor-assistant indexes every `*.md`/`*.txt` in its `knowledge/feed/`. Its
chunker (`knowledge.py`) splits a feed doc on blank lines and keeps any block of
320 characters or fewer whole. So the exporter writes **one message per
blank-line-separated block**, giving the retriever clean, self-contained chunks:

```markdown
# Chat export — Night Owls (-1001234567890)

> Feed doc for the yor-assistant. Source: supergroup -1001234567890. Exported 2026-07-02 12:00 UTC. 3 messages.

## Conversation

[2026-07-01 18:04] Alice: the meetup is Friday at 6pm, bring your laptop

[2026-07-01 18:05] Bob: sounds good, I'll drive

[2026-07-01 18:06] Alice: parking is free after 5
```

Drop it in `knowledge/feed/` (the exporter does this for you) and it is indexed
on the assistant's next load — no code changes.

**Text only:** media messages (photos, voice notes, video, stickers,
documents) and service messages (joins, pins) are dropped — only messages with
actual text are exported.

The JSONL mirror is one object per message for embeddings / fine-tuning:

```json
{"id": 42, "date": "2026-07-01T18:04:00+00:00", "sender": "Alice", "sender_id": 111, "text": "the meetup is Friday...", "reply_to": null}
```

## Setup

1. Get `API_ID` and `API_HASH` from <https://my.telegram.org> → *API development tools*.
2. Install and configure:

   ```bash
   pip install -r requirements.txt
   cp .env.example .env      # then fill in API_ID / API_HASH
   ```

3. (Optional) generate a portable string session instead of a `.session` file:

   ```bash
   python exporter.py --gen-session   # logs in once, prints STRING_SESSION
   ```

4. Start it:

   ```bash
   python exporter.py
   ```

The first run asks for your phone number + login code (Telethon standard).

## Using it

Send these to your **Saved Messages** (or whatever `CONTROL_CHAT` you set):

| Command | What it does |
|---|---|
| `.export -1001234567890` | export a whole chat by id |
| `.export @publicgroup 500` | export the last 500 messages |
| `.export https://t.me/group` | a public t.me link works too |
| `-1001234567890` | a bare id / `@username` / link on its own also exports |
| `.cancel` | stop a running export |
| `.help` | show help |

When it finishes it edits the status message with the paths and (by default)
uploads both files back to your chat.

### Getting a chat id

Yor's own `/id` command returns chat ids, or forward a message from the chat to
a userinfo bot. Group/supergroup ids are negative (often `-100…`).

## Configuration (`.env`)

| Var | Default | Meaning |
|---|---|---|
| `API_ID`, `API_HASH` | — | your Telegram app credentials (required) |
| `SESSION_NAME` | `yor_exporter` | file session name |
| `STRING_SESSION` | *(empty)* | use a string session instead of a file |
| `CONTROL_CHAT` | `me` | where it listens: `me` (Saved Messages) or a user id |
| `PREFIX` | `.` | command prefix |
| `FEED_DIR` | `./feed` | where the feed datasheet is written (point at your yor-assistant `knowledge/feed` to auto-index) |
| `EXPORT_DIR` | `./exports` | where the JSONL is written |
| `DEFAULT_LIMIT` | `0` | default max messages (`0` = whole history) |
| `SEND_FILES_BACK` | `true` | upload the files back to the control chat |

## Notes & safety

- This is a **userbot**: it acts as you, over MTProto. Only you (the control
  chat) can command it. Keep your `.session` / `STRING_SESSION` private.
- You can only export chats **your account can already read**.
- Only **text messages** are exported; media and service messages are skipped.
- Respect Telegram's Terms of Service and the privacy of the people in the
  chats you export.
