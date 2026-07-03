"""
Formatters for the Yor extractor userbot.

Primary path — **training-pair extraction** (see build_training_pairs):
turns a chronological message list into prompt -> response records where the
response is always authored by a "girl", so girls become Yor's assistant turns.

Secondary path — a **feed-ready Markdown datasheet** + per-message JSONL
(build_feed_doc / build_jsonl), kept for retrieval-style feeding of a chat's
whole text into an assistant.

Text only: media messages are dropped upstream; only text survives here.

This module is deliberately free of Telethon imports so it can be unit-tested
with plain objects.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import random
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



# --------------------------------------------------------------------------- #
# Training-pair extraction (girls == Yor's voice / assistant turns)
#
# We turn a chronological message list into prompt -> response pairs where the
# RESPONSE is always authored by a "girl" (an id in the girls set). The prompt
# is whoever they were answering:
#
#   boy  -> girl   (prompt author NOT in girls)   ... a "cross" pair
#   girl -> girl   (prompt author IS  in girls, but a different person)
#
# Because the response must be a girl, boy -> boy pairs can never appear.
# Pairing uses Telegram's reply-link when present, else the most recent
# different-author message within `window` seconds.
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)\S+", re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# 7+ digit runs (optionally +, spaces, dashes) -> looks like a phone number
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s\-]{6,}\d(?!\w)")


def scrub_pii(text: str) -> str:
    """Light PII scrub: strip emails and phone-number-like digit runs."""
    text = _EMAIL_RE.sub("", text)
    text = _PHONE_RE.sub("", text)
    return _WS.sub(" ", text).strip()


def _word_count(text: str) -> int:
    return len(text.split())


def approx_tokens(text: str) -> int:
    """Rough GPT-style token estimate (~4 chars/token)."""
    return max(1, round(len(text) / 4)) if text else 0


def _epoch(iso: str) -> float | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def passes_filters(text: str, *, min_words: int, max_chars: int,
                   drop_links: bool) -> bool:
    """Quality gate for a single message (applied to prompt AND response)."""
    clean = clean_text(text)
    if not clean:
        return False
    if drop_links and _URL_RE.search(clean):
        return False
    if _word_count(clean) < min_words:
        return False
    if len(clean) > max_chars:
        return False
    return True


def _to_record(prompt: str, response: str, *, output_format: str,
               system_prompt: str) -> dict:
    if output_format == "prompt_response":
        return {"prompt": prompt, "response": response}
    # default: OpenAI-style chat "messages" (girl == assistant turn)
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": prompt})
    msgs.append({"role": "assistant", "content": response})
    return {"messages": msgs}


def build_training_pairs(
    messages: list[Msg],
    girl_ids: set[int],
    *,
    window: int = 3600,
    min_words: int = 1,
    max_chars: int = 500,
    drop_links: bool = True,
    limit: int = 0,
    sample: bool = True,
    max_lookback: int = 60,
    system_prompt: str = "",
    output_format: str = "messages",
    seed: int | None = None,
) -> tuple[list[dict], dict]:
    """Build prompt->response pairs where the response author is a girl.

    `messages` must be chronological (oldest first). Returns (records, stats).

    Timestamps are parsed once up front (not per comparison) and the reply
    search looks back at most `max_lookback` messages, so this stays roughly
    O(n) even on very large chats.
    """
    girl_ids = {int(g) for g in girl_ids}
    by_id = {m.id: m for m in messages}
    epochs = [_epoch(m.date) for m in messages]   # parse dates ONCE
    filt = dict(min_words=min_words, max_chars=max_chars, drop_links=drop_links)

    pairs: list[tuple[Msg, Msg]] = []
    for i, resp in enumerate(messages):
        if resp.sender_id not in girl_ids:
            continue
        if not passes_filters(resp.text, **filt):
            continue

        prompt = None
        # 1) explicit reply-link
        if resp.reply_to and resp.reply_to in by_id:
            cand = by_id[resp.reply_to]
            if cand.sender_id != resp.sender_id and passes_filters(cand.text, **filt):
                prompt = cand
        # 2) most recent different-author message within the time/step window
        if prompt is None:
            ts_r = epochs[i]
            start = i - 1
            stop = max(-1, i - 1 - max_lookback)
            for j in range(start, stop, -1):
                ts_c = epochs[j]
                if ts_r is not None and ts_c is not None and (ts_r - ts_c) > window:
                    break
                cand = messages[j]
                if cand.sender_id == resp.sender_id:
                    continue
                if not passes_filters(cand.text, **filt):
                    continue
                prompt = cand
                break
        if prompt is not None:
            pairs.append((prompt, resp))

    cross = sum(1 for p, _ in pairs if p.sender_id not in girl_ids)
    same = len(pairs) - cross

    if sample:
        rng = random.Random(seed)
        rng.shuffle(pairs)
    if limit and limit > 0:
        pairs = pairs[:limit]

    prompts = [scrub_pii(clean_text(p.text)) for p, _ in pairs]
    replies = [scrub_pii(clean_text(r.text)) for _, r in pairs]
    records = [
        _to_record(pr, rp, output_format=output_format, system_prompt=system_prompt)
        for pr, rp in zip(prompts, replies)
    ]
    tokens = sum(approx_tokens(pr) + approx_tokens(rp) for pr, rp in zip(prompts, replies))

    by_user: dict[int, int] = {}
    for _, r in pairs:
        by_user[r.sender_id] = by_user.get(r.sender_id, 0) + 1

    stats = {
        "girls": len(girl_ids),
        "pairs_total": cross + same,
        "boy_to_girl": cross,
        "girl_to_girl": same,
        "emitted": len(records),
        "tokens": tokens,
        "by_user": by_user,
    }
    return records, stats


def records_to_jsonl(records: list[dict]) -> str:
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    return "\n".join(lines) + ("\n" if lines else "")
