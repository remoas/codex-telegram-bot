#!/usr/bin/env python3
"""
Codex Telegram Bot - Control OpenAI Codex CLI from Telegram.
Built for @heyb3n_ - simple, customizable, yours to modify.
"""

import asyncio
import json
import os
import logging
from pathlib import Path
from typing import Optional

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from dotenv import load_dotenv

# Load config
load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {int(uid.strip()) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()}
BASE_DIR = Path(os.environ.get("BASE_DIR", str(Path.home())))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "o3")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "danger-full-access")
MAX_MESSAGE_LENGTH = 4000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("codex-bot")


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def get_cwd(context: ContextTypes.DEFAULT_TYPE) -> Path:
    return Path(context.user_data.get("cwd", str(BASE_DIR)))


async def send_long(update: Update, text: str, parse_mode=None):
    """Send a message, splitting if too long."""
    if not text.strip():
        return
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        chunk = text[i : i + MAX_MESSAGE_LENGTH]
        try:
            await update.message.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            await update.message.reply_text(chunk)


# ── Commands ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    cwd = get_cwd(context)
    await update.message.reply_text(
        f"Codex Bot ready.\n\n"
        f"Model: {CODEX_MODEL}\n"
        f"Working dir: `{cwd}`\n\n"
        f"Just send a message to start coding.\n\n"
        f"Commands:\n"
        f"/new - fresh conversation\n"
        f"/repo - switch project folder\n"
        f"/cd <path> - change directory\n"
        f"/model <name> - switch model\n"
        f"/status - current settings",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    context.user_data.pop("session_history", None)
    await update.message.reply_text("Fresh conversation started.")


async def cmd_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    projects_dir = BASE_DIR / "Desktop" / "Projects"
    if not projects_dir.exists():
        projects_dir = BASE_DIR
    folders = sorted([d.name for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith(".")])
    if not folders:
        await update.message.reply_text("No project folders found.")
        return
    listing = "\n".join(f"  `{f}`" for f in folders)
    await update.message.reply_text(
        f"Projects in `{projects_dir}`:\n\n{listing}\n\nUse `/cd {projects_dir}/<name>` to switch.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.message.reply_text(f"Current: `{get_cwd(context)}`\nUsage: `/cd /path/to/project`", parse_mode=ParseMode.MARKDOWN)
        return
    target = Path(args).expanduser().resolve()
    if not target.exists():
        # Try relative to base
        target = (BASE_DIR / args).resolve()
    if not target.exists():
        # Try relative to Projects
        target = (BASE_DIR / "Desktop" / "Projects" / args).resolve()
    if target.exists() and target.is_dir():
        context.user_data["cwd"] = str(target)
        context.user_data.pop("session_history", None)
        await update.message.reply_text(f"Switched to `{target}`\nNew conversation started.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"Directory not found: `{args}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global CODEX_MODEL
    if context.args:
        CODEX_MODEL = context.args[0]
        await update.message.reply_text(f"Model switched to `{CODEX_MODEL}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            f"Current model: `{CODEX_MODEL}`\n\nUsage: `/model o3` or `/model o4-mini`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    cwd = get_cwd(context)
    history_len = len(context.user_data.get("session_history", []))
    await update.message.reply_text(
        f"Model: `{CODEX_MODEL}`\n"
        f"Directory: `{cwd}`\n"
        f"Sandbox: `{CODEX_SANDBOX}`\n"
        f"History: {history_len} messages\n"
        f"User: {update.effective_user.id}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Core: run Codex ──────────────────────────────────────────

async def run_codex(prompt: str, cwd: Path, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Run codex exec and stream results."""
    cmd = [
        "codex", "exec",
        "--json",
        "--sandbox", CODEX_SANDBOX,
        "--model", CODEX_MODEL,
        "-C", str(cwd),
        "--skip-git-repo-check",
        "--full-auto",
        prompt,
    ]

    log.info(f"Running: {' '.join(cmd[:6])}... '{prompt[:80]}'")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except FileNotFoundError:
        return "Error: `codex` CLI not found. Is it installed?"

    output_parts = []
    tool_calls = []

    try:
        async for line in proc.stdout:
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if event_type == "message" and event.get("role") == "assistant":
                content = event.get("content", [])
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            output_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "tool")
                            tool_input = block.get("input", {})
                            if isinstance(tool_input, dict):
                                # Show what codex is doing
                                if tool_name == "shell" or tool_name == "bash":
                                    cmd_str = tool_input.get("command", tool_input.get("cmd", ""))
                                    tool_calls.append(f"$ {cmd_str}")
                                elif tool_name in ("write_file", "create_file"):
                                    tool_calls.append(f"write: {tool_input.get('path', '?')}")
                                elif tool_name in ("read_file",):
                                    tool_calls.append(f"read: {tool_input.get('path', '?')}")
                                elif tool_name in ("edit_file", "apply_diff"):
                                    tool_calls.append(f"edit: {tool_input.get('path', '?')}")
                                else:
                                    tool_calls.append(f"{tool_name}")
                    elif isinstance(block, str):
                        output_parts.append(block)

            elif event_type == "message" and event.get("role") == "tool":
                # Tool results - skip verbose output
                pass

    except Exception as e:
        output_parts.append(f"\n[Error reading output: {e}]")

    await proc.wait()

    # Build response
    response = ""
    if tool_calls:
        response += "**Actions taken:**\n" + "\n".join(f"`{t}`" for t in tool_calls[-20:]) + "\n\n"
    if output_parts:
        response += "\n".join(output_parts)
    else:
        # Check stderr
        stderr = await proc.stderr.read()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            response = f"Codex finished but no text output.\n\nStderr:\n```\n{stderr_text[:2000]}\n```"
        else:
            response = "Codex finished with no output."

    return response.strip()


# ── Message handler ───────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return

    prompt = update.message.text
    if not prompt:
        return

    cwd = get_cwd(context)

    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)

    # Run codex
    try:
        result = await asyncio.wait_for(
            run_codex(prompt, cwd, context),
            timeout=int(os.environ.get("CODEX_TIMEOUT", "3600")),
        )
    except asyncio.TimeoutError:
        result = "Codex timed out."
    except Exception as e:
        result = f"Error: {e}"

    await send_long(update, result, parse_mode=ParseMode.MARKDOWN)


# ── Main ──────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Initialize bot"),
        BotCommand("new", "Fresh conversation (no context)"),
        BotCommand("repo", "List project folders"),
        BotCommand("cd", "Change working directory"),
        BotCommand("model", "Switch AI model"),
        BotCommand("status", "Current settings"),
    ])
    log.info("Bot commands registered")


def main():
    log.info("Starting Codex Telegram Bot")
    log.info(f"Allowed users: {ALLOWED_USERS}")
    log.info(f"Base dir: {BASE_DIR}")
    log.info(f"Model: {CODEX_MODEL}")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("repo", cmd_repo))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
