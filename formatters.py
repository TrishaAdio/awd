"""
Export formatters for the Yor chat-exporter userbot.

Turns extracted Telegram messages into two artifacts:

  1. A **feed-ready Markdown datasheet** — the format the yor-assistant actually
     eats. The assistant's chunker (knowledge.py) splits feed docs on blank
     lines, keeps any block <= 320 chars whole, and strips HTML tags. So every
     message becomes its own self-contained, blank-line-separated block:

         [2024-05-01 14:23] Alice: the event starts at 6pm on Friday

     Drop the file in knowledge/feed/ and it is auto-indexed. No code changes.

  2. A **structured JSONL** — one JSON object per message (id, date, sender,
     sender_id, reply_to, text). For general AI use: embeddings, fine tuning,
     or your own pipeline.

Text only: media messages (photos, voice, video, stickers, documents ...) are
dropped entirely; only messages with actual text survive.

This module is deliberately free of Telethon imports so it can be unit-tested
with plain objects.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, asdict, field

# Keep single feed blocks at/under the assistant's "keep whole" threshold so a
# message is indexed as one coherent chunk rather than being sentence-split.
FEED_BLOCK_SOFT_LIMIT = 320

_WS = re.compile(r"[ \t\u00a0]+")
_MULTINL = re.compile(r"\n{2,}")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Msg:
    """A single extracted text message, normalized and transport-agnostic."""
    id: int
    date: str = ""                 # ISO 8601, UTC
    sender: str = "Unknown"        # display name or @username
    sender_id: int | None = None
    text: str = ""                 # message text
    reply_to: int | None = None    # id of the message this replies to

    def as_record(self) -> dict:
        return asdict(self)


def clean_text(text: str | None) -> str:
    """Collapse whitespace and drop control chars, but keep newlines within
    a message folded to single spaces so each message stays one feed block."""
    if not text:
        return ""
    text = _CTRL.sub("", text)
    text = text.replace("\r", "\n")
    # fold internal newlines into spaces so one message == one block
    text = text.replace("\n", " ")
    text = _WS.sub(" ", text).strip()
    return text


def slugify(value: str, fallback: str = "chat") -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:48] or fallback


def feed_block(msg: Msg) -> str:
    """Render one message as a single feed block (one line, blank-line framed)."""
    stamp = msg.date.replace("T", " ")[:16] if msg.date else "?"
    body = clean_text(msg.text)
    return f"[{stamp}] {msg.sender}: {body}"


def build_feed_doc(meta: dict, messages: list[Msg]) -> str:
    """Full feed-ready Markdown datasheet for one chat."""
    title = meta.get("title") or "Telegram chat"
    chat_id = meta.get("chat_id", "?")
    exported = meta.get("exported_at", "")
    total = len(messages)
    kind = meta.get("chat_type", "chat")

    out = io.StringIO()
    out.write(f"# Chat export — {title} ({chat_id})\n\n")
    out.write(
        f"> Feed doc for the yor-assistant. Source: {kind} {chat_id}. "
        f"Exported {exported}. {total} messages.\n\n"
    )
    out.write("## Conversation\n\n")
    for m in messages:
        if not clean_text(m.text):
            continue
        out.write(feed_block(m))
        out.write("\n\n")
    return out.getvalue().rstrip() + "\n"


def build_jsonl(messages: list[Msg]) -> str:
    lines = [json.dumps(m.as_record(), ensure_ascii=False) for m in messages]
    return "\n".join(lines) + ("\n" if lines else "")


def feed_filename(meta: dict) -> str:
    return f"chat_{meta.get('chat_id', 'unknown')}_{slugify(meta.get('title', ''))}.md"


def jsonl_filename(meta: dict) -> str:
    return f"chat_{meta.get('chat_id', 'unknown')}_{slugify(meta.get('title', ''))}.jsonl"
