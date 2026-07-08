import os
from datetime import datetime

from bot.clients import bot, BOT_INFO, store
from bot.config import COMMIT_SHA, HF_SPACE_ID, HOSTING_LABEL, MODEL, RATE_LIMIT
from bot.ai import ask_ai
from bot.helpers import is_allowed, keep_typing, send_reply, should_respond
from bot.history import clear_history
from bot.preferences import get_provider, set_provider
from bot.rate_limit import is_rate_limited
from bot.components import build_menu

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

# /clear deletes recent messages by scanning backwards from its own message id
# (message ids are sequential within a chat). Telegram only lets a bot delete
# messages younger than 48h, so we stop early after CLEAR_STOP_AFTER_MISSES
# consecutive failures, and CLEAR_MAX_SCAN caps how many ids a single /clear may
# probe (so "/clear 999999" can't hammer the Telegram API).
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


def _command_arg(message) -> str:
    """Return the text after a command, or "" if there is none.

    Uses split(maxsplit=1) so a trailing space ("/spec ") yields "" instead
    of raising IndexError like the old `[1] if " " in text` idiom did.
    """
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


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
        "/help — show this message",
        "/reset — clear conversation history",
        "/clear — delete recent messages (try /clear 20)",
        "/about — about this bot",
        "/sha — show the live git commit SHA",
        "/compare — compare two or more car models",
        "/spec — key specs of a car",
        "/review — pros, cons & who it's for",
        "/fact — a random car fact",
        "/quote — an inspiring car quote",
        "/carjoke — a car-themed joke",
        "/story — a real-life car story",
        "/remember — save a note",
        "/recall — show saved notes",
        "/forget — delete all saved notes",
        "/car - shows car body types to pick one of them"
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

    # Default number of messages to scan/delete when the user just types /clear.
    scan_limit = 10

    # Let the user pass a count, e.g. "/clear 5".
    command_parts = message.text.split()
    if len(command_parts) > 1:
        try:
            scan_limit = int(command_parts[1])
        except ValueError:
            bot.reply_to(message, "Please provide a valid number. Example: /clear 5")
            return

    # Keep the count sane: no negatives, and cap it so "/clear 999999" can't
    # hammer the Telegram API.
    scan_limit = max(0, min(scan_limit, CLEAR_MAX_SCAN))

    deleted = 0
    if message.chat.type == "private":
        misses = 0
        for mid in range(message.message_id, max(message.message_id - scan_limit, 0), -1):
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
        # In groups the bot may be an admin; a blind id sweep would wipe other
        # people's messages, so there we only remove the /clear command itself.
        try:
            bot.delete_message(chat_id, message.message_id)
            deleted = 1
        except Exception:
            pass

    clear_history(message.from_user.id)  # also forget the conversation
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
    # /about is a status probe — send the facts directly (no AI call) so it
    # stays fast and can't be broken by a provider hiccup.
    bot.send_message(message.chat.id, "\n".join(facts))


# compare <cars>
@bot.message_handler(commands=["compare"], func=is_allowed)
def cmd_compare(message):
    cars = _command_arg(message)
    if not cars:
        bot.send_message(
            message.chat.id,
            "Usage: /compare <car A> vs <car B> — e.g. /compare Civic vs Corolla",
        )
        return
    with keep_typing(message.chat.id):
        reply = ask_ai(
            message.from_user.id,
            "Compare these car models against each other. Cover the main characteristics "
            f"like price, horsepower, torque, engine, and fuel economy: {cars}.",
        )
    send_reply(message, reply)


# spec <car>
@bot.message_handler(commands=["spec"], func=is_allowed)
def cmd_spec(message):
    car = _command_arg(message)
    if not car:
        bot.send_message(
            message.chat.id, "Usage: /spec <car> — e.g. /spec Toyota Corolla 2020"
        )
        return
    with keep_typing(message.chat.id):
        reply = ask_ai(
            message.from_user.id,
            f"Give the key specs of the {car}: typical price range, horsepower, engine, "
            "transmission, drivetrain, and fuel economy. Use a short bulleted list and note "
            "that the figures are approximate.",
        )
    send_reply(message, reply)


# review <car>
@bot.message_handler(commands=["review"], func=is_allowed)
def cmd_review(message):
    car = _command_arg(message)
    if not car:
        bot.send_message(
            message.chat.id, "Usage: /review <car> — e.g. /review Honda Civic"
        )
        return
    with keep_typing(message.chat.id):
        reply = ask_ai(
            message.from_user.id,
            f"Give a short, balanced review of the {car}: three pros, three cons, and who "
            "it's best for.",
        )
    send_reply(message, reply)


# fact
@bot.message_handler(commands=["fact"], func=is_allowed)
def cmd_fact(message):
    with keep_typing(message.chat.id):
        reply = ask_ai(
            message.from_user.id,
            "Tell one short, interesting fact about a random car or car brand.",
        )
    send_reply(message, reply)


# quote
@bot.message_handler(commands=["quote"], func=is_allowed)
def cmd_quote(message):
    with keep_typing(message.chat.id):
        reply = ask_ai(
            message.from_user.id,
            "Share one short, inspiring quote about cars, driving, or the open road.",
        )
    send_reply(message, reply)


# carjoke — the on-theme replacement for the old /joke
@bot.message_handler(commands=["carjoke"], func=is_allowed)
def cmd_carjoke(message):
    with keep_typing(message.chat.id):
        reply = ask_ai(message.from_user.id, "Tell one short, clean, car-themed joke.")
    send_reply(message, reply)


# story
@bot.message_handler(commands=["story"], func=is_allowed)
def cmd_story(message):
    with keep_typing(message.chat.id):
        reply = ask_ai(
            message.from_user.id,
            "Tell a short, true-to-life story about a memorable car or a car-culture "
            "moment. Keep it family-friendly and about 4-6 sentences.",
        )
    send_reply(message, reply)


# remember <note>
@bot.message_handler(commands=["remember"], func=is_allowed)
def cmd_remember(message):
    if store is None:
        bot.send_message(message.chat.id, "Memory isn't enabled on this bot.")
        return
    note = _command_arg(message)
    if not note:
        bot.send_message(
            message.chat.id,
            "Usage: /remember <note> — e.g. /remember I prefer SUVs under $30k",
        )
        return
    key = f"note:{message.from_user.id}"
    try:
        old = store.get(key)  # what's already saved (or None)
        combined = f"{old}\n{note}" if old else note  # append to it
        store.set(key, combined)
        bot.send_message(message.chat.id, "Saved!")
    except Exception as e:
        print(f"Store error (remember): {e}")
        bot.send_message(message.chat.id, "Couldn't save that right now. Try again later.")


# recall
@bot.message_handler(commands=["recall"], func=is_allowed)
def cmd_recall(message):
    if store is None:
        bot.send_message(message.chat.id, "Memory isn't enabled on this bot.")
        return
    try:
        raw = store.get(f"note:{message.from_user.id}")
    except Exception as e:
        print(f"Store error (recall): {e}")
        bot.send_message(
            message.chat.id, "Couldn't read your notes right now. Try again later."
        )
        return
    notes = raw.split("\n") if raw else []
    listing = "\n".join(f"{i}. {n}" for i, n in enumerate(notes, start=1))
    bot.send_message(message.chat.id, listing or "Nothing saved yet.")


# forget
@bot.message_handler(commands=["forget"], func=is_allowed)
def cmd_forget(message):
    if store is None:
        bot.send_message(message.chat.id, "Memory isn't enabled on this bot.")
        return
    try:
        store.delete(f"note:{message.from_user.id}")
        bot.send_message(message.chat.id, "All notes were deleted.")
    except Exception as e:
        print(f"Store error (forget): {e}")
        bot.send_message(
            message.chat.id, "Couldn't clear your notes right now. Try again later."
        )


# sha
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

@bot.message_handler(commands=["car"], func=is_allowed)
def cmd_car(message):
    keyboard = build_menu(
        items=["SUV", "Sedan", "Crossover", "Hatchback", "Sport"],
        columns=2
    )
    bot.send_message(
        message.chat.id,
        "Choose car type:",
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: True)
def on_button_tap(call):
    picked = call.data
    bot.edit_message_text(
        f"You picked: {picked}",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )