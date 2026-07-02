#!/usr/bin/env python3
"""
Yor chat-exporter userbot (Telethon).

Runs on YOUR Telegram account. You DM it a chat id (in your Saved Messages by
default) and in seconds it extracts the whole chat history and writes a
feed-ready datasheet the yor-assistant can eat, plus a structured JSONL.

Usage in Telegram (send to your Saved Messages / control chat):
    .export -1001234567890            export a whole chat by id
    .export @somepublicgroup 500      export the last 500 messages
    .export https://t.me/somegroup    a t.me link works too
    -1001234567890                    a bare id/username/link auto-exports
    .help                             show help
    .cancel                           stop a running export

CLI:
    python exporter.py                 start the userbot
    python exporter.py --gen-session   log in once and print a StringSession
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import time

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


API_ID = int(_env("API_ID", "0") or 0)
API_HASH = _env("API_HASH", "")
SESSION_NAME = _env("SESSION_NAME", "yor_exporter")
STRING_SESSION = _env("STRING_SESSION", "")
CONTROL_CHAT = _env("CONTROL_CHAT", "me")
PREFIX = _env("PREFIX", ".")
FEED_DIR = os.path.abspath(os.path.join(HERE, _env("FEED_DIR", "./feed")))
EXPORT_DIR = os.path.abspath(os.path.join(HERE, _env("EXPORT_DIR", "./exports")))
DEFAULT_LIMIT = int(_env("DEFAULT_LIMIT", "0") or 0)
SEND_FILES_BACK = str(_env("SEND_FILES_BACK", "true")).lower() in ("1", "true", "yes")

# One export at a time; lets .cancel work cleanly.
_BUSY = {"running": False, "cancel": False}


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


def _chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private chat"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    return "chat"


def _parse_target(raw: str) -> str | int:
    """Turn a user-typed target into something client.get_entity accepts."""
    raw = raw.strip()
    if raw.startswith("https://t.me/") or raw.startswith("t.me/"):
        slug = raw.split("t.me/", 1)[1].strip("/")
        # joinchat / + invite links are not resolvable as entities to read
        return "@" + slug if slug and not slug.startswith(("+", "joinchat")) else raw
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw  # @username or plain username


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
async def extract_chat(client, entity, limit, progress=None):
    """Pull the text history fast and normalize into Msg records (text only)."""
    messages: list[Msg] = []
    sender_cache: dict[int, str] = {}
    count = 0
    it = client.iter_messages(entity, limit=(limit or None))
    async for m in it:
        if _BUSY["cancel"]:
            break
        text = m.message or ""
        if not text.strip():
            # media, service messages (joins/pins), and empty messages: skip.
            continue

        sid = m.sender_id
        if sid in sender_cache:
            sender = sender_cache[sid]
        else:
            ent = None
            try:
                ent = m.sender or (await m.get_sender())
            except Exception:
                ent = None
            sender = _display_name(ent) if ent else (f"user {sid}" if sid else "Unknown")
            if sid:
                sender_cache[sid] = sender

        when = m.date.astimezone(dt.timezone.utc).isoformat() if m.date else ""
        messages.append(Msg(
            id=m.id,
            date=when,
            sender=sender,
            sender_id=sid,
            text=text,
            reply_to=m.reply_to_msg_id,
        ))
        count += 1
        if progress and count % 500 == 0:
            await progress(count)

    # oldest -> newest reads naturally as a conversation
    messages.reverse()
    return messages


# --------------------------------------------------------------------------- #
# Export orchestration
# --------------------------------------------------------------------------- #
async def do_export(client, status_msg, target_raw, limit):
    target = _parse_target(target_raw)
    try:
        entity = await client.get_entity(target)
    except Exception as e:
        await status_msg.edit(f"Could not resolve {target_raw}: {e}")
        return

    title = _display_name(entity)
    chat_id = getattr(entity, "id", target)
    await status_msg.edit(f"Extracting {title} ({chat_id}) ...")

    started = time.monotonic()

    async def progress(n):
        await status_msg.edit(f"Extracting {title} ({chat_id}) ... {n} messages")

    messages = await extract_chat(client, entity, limit, progress)
    if _BUSY["cancel"]:
        await status_msg.edit("Export cancelled.")
        return

    meta = {
        "title": title,
        "chat_id": chat_id,
        "chat_type": _chat_type(entity),
        "exported_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    feed_doc = formatters.build_feed_doc(meta, messages)
    jsonl = formatters.build_jsonl(messages)

    os.makedirs(FEED_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    feed_path = os.path.join(FEED_DIR, formatters.feed_filename(meta))
    jsonl_path = os.path.join(EXPORT_DIR, formatters.jsonl_filename(meta))

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_doc)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(jsonl)

    took = time.monotonic() - started
    summary = (
        f"Done. {len(messages)} messages from {title} in {took:.1f}s.\n\n"
        f"Feed datasheet: {feed_path}\n"
        f"Structured JSONL: {jsonl_path}\n\n"
        f"The feed doc is in knowledge/feed/ and the assistant will index it."
    )
    await status_msg.edit(summary)

    if SEND_FILES_BACK:
        try:
            await client.send_file(status_msg.chat_id, [feed_path, jsonl_path],
                                   caption=f"{title} — feed datasheet + JSONL")
        except Exception as e:
            await client.send_message(status_msg.chat_id, f"(could not upload files: {e})")


HELP_TEXT = (
    "Yor chat exporter\n\n"
    f"{PREFIX}export <chat> [limit]  — export a chat by id, @username or t.me link\n"
    f"{PREFIX}export -1001234567890  — whole history\n"
    f"{PREFIX}export @group 500      — last 500 messages\n"
    "A bare id / @username / t.me link on its own also exports.\n\n"
    f"{PREFIX}cancel  — stop a running export\n"
    f"{PREFIX}help    — this help\n\n"
    "Output: a feed-ready .md datasheet in knowledge/feed/ (auto-indexed by the "
    "assistant) and a structured .jsonl in the export dir. Text messages only."
)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def _build_client():
    if not API_ID or not API_HASH:
        sys.exit("Set API_ID and API_HASH (see .env.example).")
    if STRING_SESSION:
        return TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    return TelegramClient(os.path.join(HERE, SESSION_NAME), API_ID, API_HASH)


async def _resolve_control_id(client):
    if CONTROL_CHAT == "me":
        me = await client.get_me()
        return me.id
    try:
        return int(CONTROL_CHAT)
    except ValueError:
        ent = await client.get_entity(CONTROL_CHAT)
        return ent.id


async def _handle(event, client, control_id):
    # Only obey commands from the control chat (your Saved Messages / your id).
    if event.chat_id != control_id and event.sender_id != control_id:
        return
    raw = (event.raw_text or "").strip()
    if not raw:
        return

    low = raw.lower()
    if low == f"{PREFIX}help":
        await event.reply(HELP_TEXT)
        return
    if low == f"{PREFIX}cancel":
        if _BUSY["running"]:
            _BUSY["cancel"] = True
            await event.reply("Cancelling ...")
        else:
            await event.reply("Nothing is running.")
        return

    target_raw, limit = None, DEFAULT_LIMIT
    if low.startswith(f"{PREFIX}export"):
        parts = raw.split()
        if len(parts) < 2:
            await event.reply(f"Usage: {PREFIX}export <chat id / @username / link> [limit]")
            return
        target_raw = parts[1]
        if len(parts) >= 3 and parts[2].isdigit():
            limit = int(parts[2])
    elif raw.lstrip("-").isdigit() or raw.startswith("@") or "t.me/" in low:
        target_raw = raw  # bare target auto-exports
    else:
        return  # not a command for us

    if _BUSY["running"]:
        await event.reply("An export is already running. Send .cancel first.")
        return

    _BUSY["running"] = True
    _BUSY["cancel"] = False
    status = await event.reply("Starting export ...")
    try:
        await do_export(client, status, target_raw, limit)
    except Exception as e:
        await status.edit(f"Export failed: {e}")
    finally:
        _BUSY["running"] = False
        _BUSY["cancel"] = False


async def _main():
    client = _build_client()
    await client.start()
    control_id = await _resolve_control_id(client)
    me = await client.get_me()
    print(f"Logged in as {_display_name(me)}. Listening for commands in "
          f"{'Saved Messages' if CONTROL_CHAT == 'me' else CONTROL_CHAT}.")
    print(f"Feed datasheets -> {FEED_DIR}")
    print(f"JSONL exports   -> {EXPORT_DIR}")

    @client.on(events.NewMessage())
    async def _on(event):
        await _handle(event, client, control_id)

    await client.run_until_disconnected()


def _gen_session():
    if not API_ID or not API_HASH:
        sys.exit("Set API_ID and API_HASH first (see .env.example).")
    with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        client.start()
        print("\nYour STRING_SESSION (keep it secret, put it in .env):\n")
        print(client.session.save())


if __name__ == "__main__":
    if "--gen-session" in sys.argv:
        _gen_session()
    else:
        asyncio.run(_main())
