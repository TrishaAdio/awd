# Yor training-data extractor userbot

A **Telethon userbot** that runs on *your* Telegram account. You register the
**girls** (whose messages become Yor's voice — the assistant turns), point it at
a chat, and it builds `prompt -> response` training pairs where the response is
always a girl:

```
boy  -> girl    someone NOT in the girls set asks, a girl answers
girl -> girl    a girl asks, a different girl answers
boy  -> boy     never produced (the response must be a girl)
```

It then drops the resulting `.jsonl` back into the chat with an
`extracted successfully` note.

```
you:  /addids 111 222 333          register the girls (persisted)
you:  /export -1001234567890       build pairs from a group
       │
       ▼
iter_messages -> pair by reply-link or nearest message within the window
       │
       └─►  exports/yor_group_<id>.jsonl   ← uploaded back into the chat
```

## Login (interactive)

Start the bot and it asks, in the terminal, for everything it needs:

```bash
pip install -r requirements.txt
cp .env.example .env        # optional: pre-fill API_ID / API_HASH / PHONE
python exporter.py
```

Prompts:
- **API ID** and **API hash** — from <https://my.telegram.org> → *API development tools* (or set them in `.env`).
- **Phone number** — your account's number.
- **OTP code** — the login code Telegram sends you.
- **2FA password** — only if your account has one (leave blank otherwise).

The session is cached in a `.session` file so you only log in once. You can also
mint a portable string session with `python exporter.py --gen-session`.

## Access control

Only **allowed users** can command the bot. The account owner is always allowed;
add more ids with `ALLOWED_USERS` in `.env`. Commands from anyone else are
ignored.

## Commands

| Command | What it does |
|---|---|
| `/addids <id> <id> ...` | register girls (accepts `111`, `-100…`, or the export-style `user123456789`) |
| `/rmids <id> ...` | unregister ids |
| `/ids` | show the current girls set |
| `/clearids` | clear the set |
| `/export <chat> [limit]` | build pairs from a chat (`id` / `@username` / `t.me` link); `limit` caps pairs |
| `/cancel` | stop a running export |
| `/help` | help |

The girls set is stored in `girls.json` and survives restarts.

## Output format

Default `OUTPUT_FORMAT=messages` — OpenAI-style chat, the girl is the assistant
turn (optionally prefixed with a system prompt):

```json
{"messages": [{"role": "user", "content": "are you two free this weekend?"}, {"role": "assistant", "content": "yeah friday works for me totally"}]}
```

Set `OUTPUT_FORMAT=prompt_response` for `{"prompt": ..., "response": ...}` records.

Combine with your curated persona set and train, e.g.:

```bash
cat data/yor_waifu.jsonl exports/yor_group_-1001234567890.jsonl > data/yor_all.jsonl
BASE_MODEL=~/models/qwen2.5-3b DATA_FILE=data/yor_all.jsonl EPOCHS=12 python main.py
```

Keep the group slice a minority vs. the curated lines so Yor stays Yor while
picking up how the girls actually talk.

## Pairing & filtering

- **Prompt selection:** Telegram's reply-link when present, else the most recent
  different-author message within `PAIR_WINDOW` seconds.
- **Filters** (applied to both prompt and response): `MIN_WORDS`, `MAX_CHARS`,
  `DROP_LINK_MSGS`. Same-author self-pairs are skipped.
- **PII scrub:** emails and phone-number-like digit runs are stripped from the
  emitted text.
- **Sampling:** when more pairs than `PAIR_LIMIT` are found and `SAMPLE=true`,
  a random sample is taken.

## Config (`.env`)

| Var | Default | Meaning |
|---|---|---|
| `API_ID`, `API_HASH` | *(prompted)* | Telegram app credentials |
| `PHONE` | *(prompted)* | phone number for login |
| `STRING_SESSION` | *(empty)* | use a string session instead of a file |
| `ALLOWED_USERS` | *(empty)* | extra user ids allowed to command the bot |
| `PREFIX` | `/` | command prefix |
| `GIRLS_FILE` | `./girls.json` | where the girls set is stored |
| `FETCH_LIMIT` | `0` | messages to pull before pairing (`0` = all) |
| `PAIR_LIMIT` | `500` | max pairs emitted per export |
| `MIN_WORDS` | `3` | drop messages under this many words |
| `MAX_CHARS` | `300` | drop messages over this many chars |
| `PAIR_WINDOW` | `600` | seconds for time-window pairing |
| `DROP_LINK_MSGS` | `true` | drop messages containing links |
| `SAMPLE` | `true` | random-sample when over `PAIR_LIMIT` |
| `OUTPUT_FORMAT` | `messages` | `messages` or `prompt_response` |
| `SYSTEM_PROMPT` | *(empty)* | system prompt added to each `messages` record |
| `EXPORT_DIR` | `./exports` | where the `.jsonl` is written |

## Notes & safety

- This is a **userbot**: it acts as you over MTProto. Keep your `.session` /
  `STRING_SESSION` and `girls.json` private (all are git-ignored).
- You can only export chats **your account can already read**.
- Text messages only; media and service messages are skipped.
- Respect Telegram's Terms of Service and the privacy of the people whose
  messages you extract.
