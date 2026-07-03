import os
from datetime import datetime
from bot.clients import bot, BOT_INFO, store
from bot.config import AI_API_KEY, AI_BASE_URL, AI_MODEL,SYSTEM_PROMPT, COMMIT_SHA, HF_SPACE_ID, HOSTING_LABEL, MODEL, RATE_LIMIT
from bot.ai import ask_ai
from bot.helpers import is_allowed, keep_typing, send_reply, should_respond
from bot.history import clear_history
from bot.preferences import get_provider, set_provider
from bot.rate_limit import is_rate_limited
import random
import json

import requests

# Verbose console logging for local dev and teaching. Enabled by
# BOT_VERBOSE_LOG=1 (run_local.py sets this automatically). Prints one
# line per inbound/outbound message so kids and teachers can see the
# conversation flow in their terminal while the bot is running.
VERBOSE_LOG = os.environ.get("BOT_VERBOSE_LOG", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# /clear sweeps backwards from its own message id, deleting each message it
# still can. Telegram only lets a bot delete messages younger than 48h, so
# CLEAR_MAX_SCAN bounds how many ids we probe, and we stop early once we've
# seen CLEAR_STOP_AFTER_MISSES failures in a row (we've hit the 48h boundary).
CLEAR_MAX_SCAN = 100
CLEAR_STOP_AFTER_MISSES = 20


def _log(message, direction: str, text: str) -> None:
    """Print a one-line trace of a message in verbose mode.

    direction is "in" (user → bot) or "out" (bot → user). Text is
    truncated to 500 characters so long AI replies don't flood the
    terminal. Newlines are collapsed for single-line readability.
    """
    if not VERBOSE_LOG:
        return
    user = message.from_user
    user_name = (
        f"@{user.username}" if user.username else (user.first_name or f"user:{user.id}")
    )
    bot_name = f"@{BOT_INFO.username}"
    snippet = (text or "").replace("\n", " ").replace("\r", " ")
    if len(snippet) > 500:
        snippet = snippet[:500] + "..."
    if direction == "in":
        sender, receiver = user_name, bot_name
    else:
        sender, receiver = bot_name, user_name
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {sender} → {receiver}: {snippet}", flush=True)

# start
@bot.message_handler(commands=["start"], func=is_allowed)
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "Hello! I'm your AI assistant. I will help you with choosing the right car for you to match your needs and budget. You can ask me anything about cars, and I'll do my best to provide you with accurate and helpful information. Let's get started!",
    )

# help
@bot.message_handler(commands=["help"], func=is_allowed)
def cmd_help(message):
    lines = [
        "/start — welcome message",
        "/help  — show this message",
        "/reset — clear conversation history",
        "/clear — clear the messages",
        "/about — about this bot",
        "/sha   — show the live git commit SHA",
        "/joke - tell some funny joke",
        "/quote — tell some quote",
        "/fact — tell kind of interesting fact",
        "/compliment — compliment the user",
        "/roll — roll a dice",
        "/roast — roast the name",
        "/remember — remembers the note",
        "/recall — shows saved notes",
        "/forget — delete all notes",
        "/compare — compares two or more models with each other"
    ]
    if HF_SPACE_ID:
        lines.append("/model — switch AI provider")
    bot.send_message(message.chat.id, "\n".join(lines))

# reset
@bot.message_handler(commands=["reset"], func=is_allowed)
def cmd_reset(message):
    clear_history(message.from_user.id)
    bot.send_message(message.chat.id, "Conversation cleared. Starting fresh!")

# clear — delete recent messages from the chat, then wipe the AI's memory.
@bot.message_handler(commands=["clear"], func=is_allowed)
def cmd_clear(message):
    chat_id = message.chat.id
    deleted = 0
    # Only sweep history in private chats. In a group the bot may be an admin,
    # and blindly deleting a range of ids would wipe *other* people's messages,
    # so there we just remove the /clear command itself and reset memory.
    if message.chat.type == "private":
        misses = 0
        for mid in range(
            message.message_id, max(message.message_id - CLEAR_MAX_SCAN, 0), -1
        ):
            try:
                bot.delete_message(chat_id, mid)
                deleted += 1
                misses = 0
            except Exception:
                # >48h old, already deleted, or an id that never existed.
                misses += 1
                if misses >= CLEAR_STOP_AFTER_MISSES:
                    break
    else:
        try:
            bot.delete_message(chat_id, message.message_id)
            deleted = 1
        except Exception:
            pass
    clear_history(message.from_user.id)  # also forget the conversation
    # This confirmation is a brand-new message, so it survives the sweep above.
    bot.send_message(chat_id, f"Cleared {deleted} message(s) and reset my memory.")


# about
@bot.message_handler(commands=["about"], func=is_allowed)
def cmd_about(message):
    if HF_SPACE_ID:
        provider = get_provider(message.from_user.id)
        model_line = f"{MODEL} (main)" if provider == "main" else f"{HF_SPACE_ID} (hf)"
    else:
        model_line = MODEL
    storage_line = "SQLite" if store is not None else "stateless (no memory)"
    facts = [
        f"Model  : {model_line}",
        f"Storage: {storage_line}",
        f"Hosting: {HOSTING_LABEL}",
    ]
    if COMMIT_SHA:
        facts.append(f"Version: {COMMIT_SHA}")

    facts_text = "\n".join(facts)

    response = requests.post(
        f"{AI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {AI_API_KEY}"},
        json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Generate a short, friendly '/about' message... \n{facts_text}"},
            ],
        },
    )

    answer = response.json()["choices"][0]["message"]["content"]
    bot.send_message(message.chat.id, answer)

# joke
@bot.message_handler(commands=["joke"], func=is_allowed)
def cmd_joke(message):
 reply = ask_ai(message.from_user.id, "Tell one short, clean programming joke.")
 bot.send_message(message.chat.id, reply)

# quote
@bot.message_handler(commands=["quote"], func=is_allowed)
def cmd_quote(message):
 reply = ask_ai(message.from_user.id, "Tell one wise quote not always about cars")
 bot.send_message(message.chat.id, reply)

# fact
@bot.message_handler(commands=["fact"], func=is_allowed)
def cmd_fact(message):
 reply = ask_ai(message.from_user.id, "Tell one interesting fact about some random car ")
 bot.send_message(message.chat.id, reply)

# compliment
@bot.message_handler(commands=["compliment"], func=is_allowed)
def cmd_compliment(message):
 reply = ask_ai(message.from_user.id, "Give me one genuine compliment, grounded in what you actually know about me and my preferences. If you don't have enough to be  specific, say so instead of making something up.")
 bot.send_message(message.chat.id, reply)

# roll
@bot.message_handler(commands=["roll"], func=is_allowed)
def cmd_roll(message):
 result = random.randint(1,6)
 bot.send_message(message.chat.id, f"Rolled result: 🎲{result}!")

# roast
@bot.message_handler(commands=["roast"], func=is_allowed)
def cmd_roast(message):
 name = message.text.split(maxsplit=1)[1] if " " in message.text else "you"
 reply = ask_ai(message.from_user.id, f"Write a short, playful, friendly roast of {name}.")
 bot.send_message(message.chat.id, reply)

# remember
@bot.message_handler(commands=["remember"], func=is_allowed)
def cmd_remember(message):
      note = message.text.split(maxsplit=1)[1] if " " in message.text else ""
      key = f"note:{message.from_user.id}"
      old = store.get(key) # what's already saved (or None)
      combined = f"{old}\n{note}" if old else note   # append to it
      store.set(key, combined)
      bot.send_message(message.chat.id, "Saved!")

@bot.message_handler(commands=["recall"], func=is_allowed)
def cmd_recall(message):
      raw = store.get(f"note:{message.from_user.id}")
      notes = raw.split("\n") if raw else []
      listing = "\n".join(f"{i}. {n}" for i, n in enumerate(notes, start=1))
      bot.send_message(message.chat.id, listing or "Nothing saved yet.")

@bot.message_handler(commands=["forget"], func=is_allowed)
def cmd_forget(message):
      raw = store.get(f"note:{message.from_user.id}")
      key = f"note:{message.from_user.id}"
      raw = ""
      store.set(key, raw)
      bot.send_message(message.chat.id, "All notes were deleted")


@bot.message_handler(commands=["compare"], func=is_allowed)
def cmd_compare(message):
    compare_text = message.text.split(maxsplit=1)[1] if " " in message.text else ""
    reply = ask_ai(message.from_user.id, f"Compare this car models between each other. Write main characteristics like price, hp, torque, engine and etc: {compare_text}.")
    bot.send_message(message.from_user.id, reply)


@bot.message_handler(commands=["sha"], func=is_allowed)
def cmd_sha(message):
    sha = COMMIT_SHA or "unknown"
    bot.send_message(message.chat.id, f"Live SHA: {sha}")


if HF_SPACE_ID:

    @bot.message_handler(commands=["model"], func=is_allowed)
    def cmd_model(message):
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            current = get_provider(message.from_user.id)
            bot.send_message(
                message.chat.id,
                f"Current provider: {current}\n\n"
                "Options:\n"
                "/model main — Cerebras (fast, multilingual, with memory)\n"
                "/model hf — ArmGPT (Armenian only, slow, no memory)",
            )
            return
        choice = parts[1].strip().lower()
        if choice not in ("main", "hf"):
            bot.send_message(
                message.chat.id, "Invalid choice. Use: /model main or /model hf"
            )
            return
        if not set_provider(message.from_user.id, choice):
            bot.send_message(
                message.chat.id, "Could not save preference. Try again later."
            )
            return
        if choice == "hf":
            bot.send_message(
                message.chat.id,
                "Switched to hf (ArmGPT).\n\n"
                "Note: this is a tiny base completion model trained only on Armenian text. "
                "It will continue whatever you write rather than answer questions, "
                "and it does not understand English. Replies take ~30-60s and there is no memory.",
            )
        else:
            bot.send_message(message.chat.id, "Switched to Main Provider.")


@bot.message_handler(content_types=["text"], func=is_allowed)
def handle_message(message):
    if not should_respond(message):
        return
    text = (message.text or "").replace(f"@{BOT_INFO.username}", "").strip()
    if not text:
        # Edited messages, forwards, or stickers-with-empty-caption can
        # arrive with no usable text. Don't burn rate-limit / AI calls on them.
        return
    _log(message, "in", text)
    if is_rate_limited(message.from_user.id):
        limit_msg = f"You've reached the daily limit of {RATE_LIMIT} messages. Try again tomorrow."
        bot.send_message(message.chat.id, limit_msg)
        _log(message, "out", f"[rate limited] {limit_msg}")
        return
    try:
        with keep_typing(message.chat.id):
            reply = ask_ai(message.from_user.id, text)
        send_reply(message, reply)
        _log(message, "out", reply)
    except Exception as e:
        print(f"Error in handle_message: {e}")
        bot.send_message(message.chat.id, "Something went wrong. Please try again.")
        _log(message, "out", f"[error] {e}")
