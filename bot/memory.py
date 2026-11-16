"""Memory operations (ported from WhatsBot's llm.py + prompts.py).

Two-tier memory on top of SessionStore:
- a **rolling summary** per session — older turns are compressed into a
  compact summary that is injected into the system prompt; the recent
  ``verbatim_turns`` stay in context as-is;
- **persistent facts** per user — extracted from their messages and
  injected into the system prompt until pruned or deleted.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from .facts import FactsStore
from .store import SessionStore

log = logging.getLogger("bot.memory")

# Rolling summarizer: compresses older session turns into a compact summary.
SUMMARY_SYSTEM_PROMPT = """
You are a conversation summarizer. Your task is to update a rolling summary of a conversation.

Rules:
- Write in the same language as the conversation.
- Keep the summary under 200 words.
- Focus on: key facts, decisions, preferences, ongoing topics, and unresolved threads.
- Drop trivial pleasantries and small talk.
- If there is an existing summary, MERGE it with the new messages. Do not lose important prior context.
- If there is no existing summary, just summarize the provided messages.
- Be concise and factual. No fluff.

Existing summary:
-----------------
{existing_summary}
-----------------

New messages to incorporate:
-----------------
{new_messages}
-----------------

Write the updated summary below:
"""

# Fact extractor: pulls durable facts from each user message.
FACT_EXTRACTION_PROMPT = """
You are a fact extractor. Your task is to identify durable, verifiable facts from a user message.

Rules:
- Extract 0 to 5 facts. Return an empty list if there are no clear facts.
- A "fact" is a specific piece of information about the user or someone they mention:
  names, preferences, relationships, events, plans, jobs, locations, habits.
- Do NOT extract: opinions, questions, general knowledge, pleasantries, or things the bot said.
- Write each fact as a short, self-contained sentence (max 15 words).
- The "entity" field is the person/thing the fact is about (e.g. "user", a person's name).
  Use lowercase, no punctuation.
- The "confidence" field is a number from 0.0 to 1.0.
  Use 0.9+ for explicit statements ("my name is X"), 0.5-0.8 for implied facts.

Respond ONLY with a JSON array. No other text.
Format: [{{"fact": "...", "entity": "...", "confidence": 0.8}}]

User message:
-----------------
{user_message}
-----------------
"""

# How often the background cleanup prunes old messages and stale facts.
CLEANUP_INTERVAL_SECONDS = 6 * 3600


def summarize_session(store: SessionStore, model, session_id: str,
                      verbatim_turns: int) -> bool:
    """Summarize messages that fall outside the verbatim-tail window of this
    session. Merges with the existing summary and stores the result."""
    tail_ts = store.oldest_tail_timestamp(session_id, verbatim_turns)
    if tail_ts is None:
        return False

    existing_summary, last_summarized_ts = store.get_summary(session_id)
    effective_since = last_summarized_ts if last_summarized_ts else 0
    messages_to_summarize = [
        m for m in store.messages_since(session_id, effective_since)
        if m[1] < tail_ts
    ]
    if not messages_to_summarize:
        return False

    new_lines = [
        f"{'ASSISTANT' if role == 'assistant' else 'USER'}: {content}"
        for content, _ts, role in messages_to_summarize
    ]
    system_prompt = SUMMARY_SYSTEM_PROMPT.format(
        existing_summary=existing_summary or "(none)",
        new_messages="\n".join(new_lines),
    )
    msg = model.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Update the summary."},
    ])
    result = (msg.get("content") or "").strip()
    if not result:
        return False

    result = result[:1200]
    last_summarized = max(m[1] for m in messages_to_summarize)
    store.save_summary(session_id, result, last_summarized)
    log.info("Updated summary for session %s (%d messages).", session_id, len(new_lines))
    return True


def normalize_entity(entity: str) -> str:
    """Normalize an entity string: lowercase, no punctuation, single spaces."""
    if not entity:
        return "unknown"
    entity = entity.strip().lower()
    entity = re.sub(r"[^\w\s]", "", entity)
    entity = re.sub(r"\s+", " ", entity)
    return entity or "unknown"


def extract_facts(facts: FactsStore, model, user_id: str, user_message: str) -> int:
    """Extract durable facts from a user message. Returns number saved."""
    system_prompt = FACT_EXTRACTION_PROMPT.format(user_message=user_message)
    msg = model.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Extract facts from the message above."},
    ])
    result = (msg.get("content") or "").strip()
    if not result:
        return 0

    # Strip markdown code fences if present
    if result.startswith("```"):
        result = re.sub(r"^```(?:json)?\s*", "", result)
        result = re.sub(r"\s*```$", "", result)
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError as e:
        log.error("Failed to parse fact extraction JSON: %s", e)
        return 0
    if not isinstance(parsed, list):
        return 0

    saved = 0
    for item in parsed[:5]:
        if not isinstance(item, dict):
            continue
        fact = (item.get("fact") or "").strip()
        if not fact:
            continue
        entity = normalize_entity(item.get("entity"))
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        facts.save_fact(user_id, fact, entity, confidence)
        saved += 1
    return saved


def facts_text(facts: FactsStore, user_id: str, limit: int) -> str:
    """Durable facts about this user, formatted for the prompt. '' if none."""
    rows = facts.get_facts(user_id, limit)
    if not rows:
        return ""
    lines = [f"- {fact}" for (_id, fact, _entity, _conf, _created, _confirmed) in rows]
    return "[FACTS about this user]\n" + "\n".join(lines)


def summary_text(store: SessionStore, session_id: str) -> str:
    """Rolling summary of this session, formatted for the prompt. '' if none."""
    text, _ts = store.get_summary(session_id)
    if not text:
        return ""
    return f"[CONVERSATION SUMMARY]\n{text}"


def user_text_from_parts(parts: list) -> str:
    """Plain text of a content-parts list (for fact extraction). '' if none."""
    texts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
    ]
    return " ".join(t.strip() for t in texts if t.strip())


def _cleanup_loop(store: SessionStore, facts: FactsStore,
                  memory_cutoff_days: int, fact_cutoff_days: int) -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        pruned_msgs = store.prune_old_messages(cutoff_days=memory_cutoff_days)
        pruned_facts = facts.prune_stale_facts(cutoff_days=fact_cutoff_days)
        log.info(
            "[CLEANUP] Pruned %d old messages, %d stale facts. Total facts: %d.",
            pruned_msgs, pruned_facts, facts.get_fact_count(),
        )


def start_cleanup_thread(store: SessionStore, facts: FactsStore,
                         memory_cutoff_days: int, fact_cutoff_days: int) -> threading.Thread:
    """Background thread that prunes old messages and stale facts every 6 hours."""
    t = threading.Thread(
        target=_cleanup_loop,
        args=(store, facts, memory_cutoff_days, fact_cutoff_days),
        name="memory-cleanup",
        daemon=True,
    )
    t.start()
    return t
