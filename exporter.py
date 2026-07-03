#!/usr/bin/env python3
"""
Yor training-data extractor userbot (Telethon).

Runs on YOUR Telegram account. Add it to a group, then an allowed user
registers the target users (whose messages become Yor's voice / the assistant
turns) right in that group:

    /addusers 8339524472 6615872523 7558095919 ...

That both registers the ids and immediately extracts the current group into
prompt -> response pairs where the RESPONSE is always one of those users:

    other -> target    someone else asks, a target user answers
    target -> target   a target asks, a different target answers

other -> other is never produced. The resulting file is dropped back into the
chat with a per-user stats caption (chat totals + total tokens extracted).

Commands (from an allowed account; the owner is always allowed):
    /addusers <id> <id> ...   register targets and extract this chat
    /rmusers <id> ...          unregister ids
    /users                     show the registered set
    /clearusers                clear the set
    /export <chat> [limit]     extract a specific chat instead of the current one
    /cancel                    stop a running extraction
    /help                      help

CLI:
    python exporter.py              start the userbot (interactive login)
    python exporter.py --gen-session  print a reusable StringSession
"""
from __future__ import annotations

import asyncio
import datetime as dt
import getpass
import html
import json
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:  # dotenv optional
    pass

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel

import formatters
from formatters import Msg

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


API_ID = _env("API_ID", "")
API_HASH = _env("API_HASH", "")
PHONE = _env("PHONE", "")
SESSION_NAME = _env("SESSION_NAME", "yor_exporter")
STRING_SESSION = _env("STRING_SESSION", "")
PREFIX = _env("PREFIX", "/")
EXPORT_DIR = os.path.abspath(os.path.join(HERE, _env("EXPORT_DIR", "./exports")))
GIRLS_FILE = os.path.abspath(os.path.join(HERE, _env("GIRLS_FILE", "./girls.json")))

# Extra user ids allowed to command the bot (comma/space separated). The
# account owner is always allowed.
ALLOWED_USERS = {
    int(x) for x in _env("ALLOWED_USERS", "").replace(",", " ").split() if x.strip().lstrip("-").isdigit()
}

# Pairing / filtering knobs (mirror the from_telegram.py flags).
FETCH_LIMIT = int(_env("FETCH_LIMIT", "0") or 0)     # messages to pull (0 = all)
PAIR_LIMIT = int(_env("PAIR_LIMIT", "0") or 0)       # max pairs emitted (0 = all)
MIN_WORDS = int(_env("MIN_WORDS", "1") or 1)         # 1 keeps short casual chat
MAX_CHARS = int(_env("MAX_CHARS", "500") or 500)
PAIR_WINDOW = int(_env("PAIR_WINDOW", "3600") or 3600)  # seconds
PAIR_LOOKBACK = int(_env("PAIR_LOOKBACK", "60") or 60)  # max messages to scan back
DROP_LINK_MSGS = str(_env("DROP_LINK_MSGS", "true")).lower() in ("1", "true", "yes")
SAMPLE = str(_env("SAMPLE", "true")).lower() in ("1", "true", "yes")
OUTPUT_FORMAT = _env("OUTPUT_FORMAT", "messages")     # messages | prompt_response
OUTPUT_EXT = _env("OUTPUT_EXT", "txt").lstrip(".")    # sent file extension
SYSTEM_PROMPT = _env("SYSTEM_PROMPT", "")

_BUSY = {"running": False, "cancel": False}


# --------------------------------------------------------------------------- #
# Girls set persistence
# --------------------------------------------------------------------------- #
def load_girls() -> set[int]:
    try:
        with open(GIRLS_FILE, encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except Exception:
        return set()


def save_girls(girls: set[int]) -> None:
    with open(GIRLS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(girls), f)


GIRLS: set[int] = load_girls()


def _parse_id_token(tok: str) -> int | None:
    """Accept 12345, -100123, or the export-style 'user123456789'."""
    tok = tok.strip()
    if tok.lower().startswith("user"):
        tok = tok[4:]
    tok = tok.strip()
    if tok.lstrip("-").isdigit():
        return int(tok)
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _display_name(entity) -> str:
    if entity is None:
        return "Unknown"
    if isinstance(entity, User):
        name = " ".join(p for p in (entity.first_name, entity.last_name) if p)
        if name:
            return name
        if entity.username:
            return "@" + entity.username
        return f"user {entity.id}"
    title = getattr(entity, "title", None)
    return title or f"chat {getattr(entity, 'id', '?')}"


def _parse_target(raw: str) -> str | int:
    raw = raw.strip()
    if raw.startswith("https://t.me/") or raw.startswith("t.me/"):
        slug = raw.split("t.me/", 1)[1].strip("/")
        return "@" + slug if slug and not slug.startswith(("+", "joinchat")) else raw
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
async def extract_chat(client, entity, limit, progress=None):
    """Pull the text history fast, normalized into chronological Msg records."""
    messages: list[Msg] = []
    count = 0
    tokens = 0
    async for m in client.iter_messages(entity, limit=(limit or None)):
        if _BUSY["cancel"]:
            break
        text = m.message or ""
        if not text.strip():
            continue  # media / service / empty
        messages.append(Msg(
            id=m.id,
            date=(m.date.astimezone(dt.timezone.utc).isoformat() if m.date else ""),
            sender=str(m.sender_id),
            sender_id=m.sender_id,
            text=text,
            reply_to=m.reply_to_msg_id,
        ))
        count += 1
        tokens += formatters.approx_tokens(text)
        if progress and count % 1000 == 0:
            await progress(count, tokens)
    messages.reverse()  # oldest -> newest for correct pairing
    return messages


_NAME_CACHE: dict[int, str] = {}


async def _safe_name(client, uid) -> str:
    if uid in _NAME_CACHE:
        return _NAME_CACHE[uid]
    try:
        ent = await client.get_entity(uid)
        name = _display_name(ent)
    except Exception:
        name = f"user {uid}"
    _NAME_CACHE[uid] = name
    return name


def _esc(s) -> str:
    return html.escape(str(s), quote=False)


async def _stats_caption(client, title, messages, stats) -> str:
    """HTML caption: per-user chat totals in a blockquote + pairs/tokens."""
    totals: dict[int, int] = {}
    for m in messages:
        if m.sender_id in GIRLS:
            totals[m.sender_id] = totals.get(m.sender_id, 0) + 1

    rows = []
    for uid in sorted(GIRLS, key=lambda u: totals.get(u, 0), reverse=True)[:25]:
        name = _esc(await _safe_name(client, uid))
        rows.append(f"{name} — <b>{totals.get(uid, 0):,}</b> msgs")
    if len(GIRLS) > 25:
        rows.append(f"(+{len(GIRLS) - 25} more)")

    return (
        f"<b>{_esc(title)}</b>\n"
        f"<blockquote>{chr(10).join(rows)}</blockquote>\n"
        f"pairs extracted: <b>{stats['emitted']:,}</b>\n"
        f"tokens collected: <b>{stats['tokens']:,}</b>\n"
        f"thanks for data"
    )


async def do_export(client, status_msg, target_raw, pair_limit):
    if not GIRLS:
        await status_msg.edit(f"No users registered. Add ids first: {PREFIX}addusers <id> <id> ...")
        return

    target = _parse_target(target_raw)
    try:
        entity = await client.get_entity(target)
    except Exception as e:
        await status_msg.edit(f"Could not resolve {target_raw}: {e}")
        return

    title = _display_name(entity)
    chat_id = getattr(entity, "id", target)
    await status_msg.edit(f"Extracting <b>{_esc(title)}</b> …", parse_mode="html")

    async def progress(n, toks):
        await status_msg.edit(
            f"Extracting <b>{_esc(title)}</b> … <b>{n:,}</b> messages\n"
            f"Tokens Achieved Till Now : <b>{toks:,}</b>",
            parse_mode="html")

    messages = await extract_chat(client, entity, FETCH_LIMIT, progress)
    if _BUSY["cancel"]:
        await status_msg.edit("Cancelled.")
        return

    records, stats = formatters.build_training_pairs(
        messages, GIRLS,
        window=PAIR_WINDOW, min_words=MIN_WORDS, max_chars=MAX_CHARS,
        drop_links=DROP_LINK_MSGS, limit=pair_limit, sample=SAMPLE,
        max_lookback=PAIR_LOOKBACK,
        system_prompt=SYSTEM_PROMPT, output_format=OUTPUT_FORMAT,
    )
    jsonl = formatters.records_to_jsonl(records)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    out_path = os.path.join(EXPORT_DIR, f"yor_{chat_id}.{OUTPUT_EXT}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(jsonl)

    if not records:
        await status_msg.edit(
            f"No pairs matched. {stats['pairs_total']} candidate replies found but "
            f"none passed the filters (min_words {MIN_WORDS}, max_chars {MAX_CHARS})."
        )
        return

    caption = await _stats_caption(client, title, messages, stats)
    try:
        await client.send_file(status_msg.chat_id, out_path, caption=caption,
                               parse_mode="html", force_document=True)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit(f"{caption}\n<blockquote>saved to {_esc(out_path)}; "
                              f"upload failed: {_esc(e)}</blockquote>", parse_mode="html")


HELP_TEXT = (
    "Yor training extractor\n\n"
    f"{PREFIX}addusers <id> <id> ...  register target users and extract this chat\n"
    f"{PREFIX}rmusers <id> ...         unregister ids\n"
    f"{PREFIX}users                    show the registered users\n"
    f"{PREFIX}clearusers               clear the set\n"
    f"{PREFIX}export <chat> [limit]    extract a specific chat (id / @username / link)\n"
    f"{PREFIX}cancel                   stop a running extraction\n\n"
    "Add the bot to a group, then /addusers the ids there. Reply turns come only "
    "from those users (others -> them, and them -> each other); never other->other."
)


# --------------------------------------------------------------------------- #
# Command handling
# --------------------------------------------------------------------------- #
async def run_export(client, event, target_raw, pair_limit):
    if _BUSY["running"]:
        await event.reply(f"An extraction is already running. Send {PREFIX}cancel first.")
        return
    _BUSY["running"] = True
    _BUSY["cancel"] = False
    status = await event.reply("Starting ...")
    try:
        await do_export(client, status, target_raw, pair_limit)
    except Exception as e:
        await status.edit(f"Extraction failed: {e}")
    finally:
        _BUSY["running"] = False
        _BUSY["cancel"] = False


async def _handle(event, client, allowed_ids):
    if event.sender_id not in allowed_ids:
        return
    raw = (event.raw_text or "").strip()
    if not raw or not raw.startswith(PREFIX):
        return
    parts = raw.split()
    cmd = parts[0][len(PREFIX):].lower()
    args = parts[1:]

    if cmd == "help":
        await event.reply(HELP_TEXT)
        return

    if cmd in ("addusers", "addids"):
        added = []
        for tok in args:
            gid = _parse_id_token(tok)
            if gid is not None and gid not in GIRLS:
                GIRLS.add(gid)
                added.append(gid)
        save_girls(GIRLS)
        if not GIRLS:
            await event.reply(f"Usage: {PREFIX}addusers <id> <id> ...")
            return
        # register, then immediately extract THIS chat
        await run_export(client, event, str(event.chat_id), PAIR_LIMIT)
        return

    if cmd in ("rmusers", "rmids"):
        removed = []
        for tok in args:
            gid = _parse_id_token(tok)
            if gid is not None and gid in GIRLS:
                GIRLS.discard(gid)
                removed.append(gid)
        save_girls(GIRLS)
        await event.reply(f"Users: {len(GIRLS)} ({len(removed)} removed)\n" + _ids_block())
        return

    if cmd in ("users", "ids"):
        await event.reply(f"Users: {len(GIRLS)}\n" + _ids_block())
        return

    if cmd in ("clearusers", "clearids"):
        GIRLS.clear()
        save_girls(GIRLS)
        await event.reply("Users: 0")
        return

    if cmd == "cancel":
        if _BUSY["running"]:
            _BUSY["cancel"] = True
            await event.reply("Cancelling ...")
        else:
            await event.reply("Nothing is running.")
        return

    if cmd == "export":
        if not args:
            await event.reply(f"Usage: {PREFIX}export <chat id / @username / link> [limit]")
            return
        target_raw = args[0]
        limit = int(args[1]) if len(args) >= 2 and args[1].isdigit() else PAIR_LIMIT
        await run_export(client, event, target_raw, limit)
        return


def _ids_block() -> str:
    if not GIRLS:
        return "(none)"
    return " ".join(str(g) for g in sorted(GIRLS))


# --------------------------------------------------------------------------- #
# Login / wiring
# --------------------------------------------------------------------------- #
def _resolve_credentials():
    """API id/hash from env, or prompt in the terminal."""
    api_id = API_ID or input("API ID: ").strip()
    api_hash = API_HASH or input("API hash: ").strip()
    if not api_id or not api_hash:
        sys.exit("API ID and API hash are required (my.telegram.org).")
    return int(api_id), api_hash


def _build_client():
    api_id, api_hash = _resolve_credentials()
    if STRING_SESSION:
        return TelegramClient(StringSession(STRING_SESSION), api_id, api_hash)
    return TelegramClient(os.path.join(HERE, SESSION_NAME), api_id, api_hash)


async def _start_client(client):
    """Interactive login: phone number + OTP (and 2FA password if set)."""
    await client.start(
        phone=(lambda: PHONE or input("Phone number (e.g. +15551234567): ").strip()),
        code_callback=(lambda: input("OTP code you received on Telegram: ").strip()),
        password=(lambda: getpass.getpass("2FA password (blank if none): ")),
    )


async def _main():
    client = _build_client()
    await _start_client(client)
    me = await client.get_me()
    allowed_ids = {me.id} | ALLOWED_USERS

    print(f"Logged in as {_display_name(me)} (id {me.id}).")
    print(f"Allowed commanders: {sorted(allowed_ids)}")
    print(f"Girls registered: {len(GIRLS)}  ->  {GIRLS_FILE}")
    print(f"JSONL exports -> {EXPORT_DIR}")

    @client.on(events.NewMessage())
    async def _on(event):
        await _handle(event, client, allowed_ids)

    await client.run_until_disconnected()


def _gen_session():
    api_id, api_hash = _resolve_credentials()
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        client.start(
            phone=(lambda: PHONE or input("Phone number: ").strip()),
            code_callback=(lambda: input("OTP code: ").strip()),
            password=(lambda: getpass.getpass("2FA password (blank if none): ")),
        )
        print("\nYour STRING_SESSION (keep it secret, put it in .env):\n")
        print(client.session.save())


if __name__ == "__main__":
    if "--gen-session" in sys.argv:
        _gen_session()
    else:
        asyncio.run(_main())
