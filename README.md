# Yor training-data extractor userbot

A **Telethon userbot** that runs on *your* Telegram account. Add it to a group,
then an allowed user registers the **target users** (whose messages become Yor's
voice — the assistant turns) right in that group. It extracts `prompt -> response`
pairs where the response is always a target user:

```
other  -> target   someone NOT registered asks, a target user answers
target -> target   a target asks, a different target answers
other  -> other    never produced (the response must be a target user)
```

The result is dropped back into the chat as a file, with a per-user stats
caption.

```
(in your group)  /addusers 8339524472
                            6615872523
                            7558095919
       │
       ▼
registers the ids AND immediately extracts THIS group
       │
       └─►  yor_<chatid>.txt  uploaded into the chat, caption:

              user Priya chat total : 812
              user Ananya chat total : 640
              user Meera chat total : 291
              total tokens extracted : 48120
              thanks for data
```

`chat total` is each user's message count in the chat; `total tokens extracted`
is an approximate token count of the emitted training data.

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
ignored — this matters because the bot sits in a group and reads everything.

## Commands

| Command | What it does |
|---|---|
| `/addusers <id> <id> ...` | register targets (ids on separate lines are fine) **and extract the current chat** |
| `/rmusers <id> ...` | unregister ids |
| `/users` | show the registered set |
| `/clearusers` | clear the set |
| `/export <chat> [limit]` | extract a specific chat instead of the current one (`id` / `@username` / `t.me` link) |
| `/cancel` | stop a running extraction |
| `/help` | help |

Ids accept plain `8339524472`, `-100…`, or the export-style `user8339524472`.
The set is stored in `girls.json` and survives restarts. `/addids`, `/rmids`,
`/ids` remain as aliases.

## Output format

Default `OUTPUT_FORMAT=messages` — OpenAI-style chat, the girl is the assistant
turn (optionally prefixed with a system prompt):

```json
{"messages": [{"role": "user", "content": "are you two free this weekend?"}, {"role": "assistant", "content": "yeah friday works for me totally"}]}
```

Set `OUTPUT_FORMAT=prompt_response` for `{"prompt": ..., "response": ...}` records.

Combine with your curated persona set and train, e.g.:

```bash
cat data/yor_waifu.jsonl exports/yor_-1001234567890.txt > data/yor_all.jsonl
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
| `OUTPUT_EXT` | `txt` | extension of the file sent to the chat (content is JSONL) |
| `SYSTEM_PROMPT` | *(empty)* | system prompt added to each `messages` record |
| `EXPORT_DIR` | `./exports` | where the file is written |

## Notes & safety

- This is a **userbot**: it acts as you over MTProto. Keep your `.session` /
  `STRING_SESSION` and `girls.json` private (all are git-ignored).
- You can only export chats **your account can already read**.
- Text messages only; media and service messages are skipped.
- Respect Telegram's Terms of Service and the privacy of the people whose
  messages you extract.
