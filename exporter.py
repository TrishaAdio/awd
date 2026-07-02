#!/usr/bin/env python3
"""
Yor training-data extractor userbot (Telethon).

Runs on YOUR Telegram account. You register the "girls" (whose messages become
Yor's voice / the assistant turns), then point it at a chat. It extracts
prompt -> response pairs where the RESPONSE is always a girl:

    boy  -> girl   (someone not in the girls set asks, a girl answers)
    girl -> girl   (a girl asks, a different girl answers)

Because the response must be a girl, boy -> boy pairs never appear. It drops the
resulting .jsonl back into the chat with an "extracted successfully" note.

Commands (send from an allowed account, default = you):
    /addids 12345 user678 ...   register girl ids (accepts "user123" too)
    /rmids 12345 ...            unregister ids
    /ids                        show the current girls set
    /clearids                   clear the set
    /export <chat> [limit]      build pairs from a chat (id / @username / link)
    /cancel                     stop a running export
    /help                       show help

CLI:
    python exporter.py              start the userbot (interactive login)
    python exporter.py --gen-session  print a reusable StringSession
"""
from __future__ import annotations

import asyncio
import datetime as dt
import getpass
import json
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
PAIR_LIMIT = int(_env("PAIR_LIMIT", "500") or 500)   # max pairs emitted
MIN_WORDS = int(_env("MIN_WORDS", "3") or 3)
MAX_CHARS = int(_env("MAX_CHARS", "300") or 300)
PAIR_WINDOW = int(_env("PAIR_WINDOW", "600") or 600)  # seconds
DROP_LINK_MSGS = str(_env("DROP_LINK_MSGS", "true")).lower() in ("1", "true", "yes")
SAMPLE = str(_env("SAMPLE", "true")).lower() in ("1", "true", "yes")
OUTPUT_FORMAT = _env("OUTPUT_FORMAT", "messages")     # messages | prompt_response
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
        if progress and count % 1000 == 0:
            await progress(count)
    messages.reverse()  # oldest -> newest for correct pairing
    return messages


async def do_export(client, status_msg, target_raw, pair_limit):
    if not GIRLS:
        await status_msg.edit(f"No girls registered. Add ids first: {PREFIX}addids <id> <id> ...")
        return

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

    messages = await extract_chat(client, entity, FETCH_LIMIT, progress)
    if _BUSY["cancel"]:
        await status_msg.edit("Cancelled.")
        return

    records, stats = formatters.build_training_pairs(
        messages, GIRLS,
        window=PAIR_WINDOW, min_words=MIN_WORDS, max_chars=MAX_CHARS,
        drop_links=DROP_LINK_MSGS, limit=pair_limit, sample=SAMPLE,
        system_prompt=SYSTEM_PROMPT, output_format=OUTPUT_FORMAT,
    )
    jsonl = formatters.records_to_jsonl(records)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    out_path = os.path.join(EXPORT_DIR, f"yor_group_{chat_id}.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(jsonl)

    took = time.monotonic() - started
    caption = (
        f"Extracted successfully — {stats['emitted']} pairs from {title}.\n"
        f"boy->girl {stats['boy_to_girl']} · girl->girl {stats['girl_to_girl']} · "
        f"girls {stats['girls']} · {took:.1f}s"
    )

    if records:
        try:
            await client.send_file(status_msg.chat_id, out_path, caption=caption,
                                   force_document=True)
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit(f"{caption}\n(saved to {out_path}; upload failed: {e})")
    else:
        await status_msg.edit(
            f"No pairs matched. {stats['pairs_total']} candidate girl replies found "
            f"but none passed the filters (min_words {MIN_WORDS}, max_chars {MAX_CHARS})."
        )


HELP_TEXT = (
    "Yor training extractor\n\n"
    f"{PREFIX}addids <id> <id> ...  register girls (accepts user123 form)\n"
    f"{PREFIX}rmids <id> ...         unregister ids\n"
    f"{PREFIX}ids                    show the girls set\n"
    f"{PREFIX}clearids               clear the set\n"
    f"{PREFIX}export <chat> [limit]  build pairs (id / @username / t.me link)\n"
    f"{PREFIX}cancel                 stop a running export\n\n"
    "Response turns come only from the girls (boy->girl and girl->girl); "
    "boy->boy is never produced. Output: a training .jsonl dropped here."
)


# --------------------------------------------------------------------------- #
# Command handling
# --------------------------------------------------------------------------- #
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

    if cmd == "addids":
        added = []
        for tok in args:
            gid = _parse_id_token(tok)
            if gid is not None and gid not in GIRLS:
                GIRLS.add(gid)
                added.append(gid)
        save_girls(GIRLS)
        await event.reply(f"Girls: {len(GIRLS)} ids ({len(added)} added)\n" + _ids_block())
        return

    if cmd == "rmids":
        removed = []
        for tok in args:
            gid = _parse_id_token(tok)
            if gid is not None and gid in GIRLS:
                GIRLS.discard(gid)
                removed.append(gid)
        save_girls(GIRLS)
        await event.reply(f"Girls: {len(GIRLS)} ids ({len(removed)} removed)\n" + _ids_block())
        return

    if cmd == "ids":
        await event.reply(f"Girls: {len(GIRLS)} ids\n" + _ids_block())
        return

    if cmd == "clearids":
        GIRLS.clear()
        save_girls(GIRLS)
        await event.reply("Girls: 0 ids")
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
        if _BUSY["running"]:
            await event.reply(f"An export is already running. Send {PREFIX}cancel first.")
            return
        _BUSY["running"] = True
        _BUSY["cancel"] = False
        status = await event.reply("Starting ...")
        try:
            await do_export(client, status, target_raw, limit)
        except Exception as e:
            await status.edit(f"Export failed: {e}")
        finally:
            _BUSY["running"] = False
            _BUSY["cancel"] = False
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
