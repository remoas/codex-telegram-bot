#!/usr/bin/env python3
"""
Codex Telegram Bot — Control OpenAI Codex CLI from Telegram.
Best-in-class integration: streaming, copy buttons, voice, inline mode, and more.
"""

import asyncio
import hashlib
import html as _html
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, RetryAfter, TimedOut
from dotenv import load_dotenv

# Optional: copy-to-clipboard buttons (python-telegram-bot >= 21.9)
try:
    from telegram import CopyTextButton

    HAS_COPY = True
except ImportError:
    HAS_COPY = False

# Optional: OpenAI Whisper for voice transcription
try:
    import openai as _openai

    HAS_OPENAI = bool(os.environ.get("OPENAI_API_KEY"))
except ImportError:
    HAS_OPENAI = False


# ── Config ────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {
    int(u.strip())
    for u in os.environ.get("ALLOWED_USERS", "").split(",")
    if u.strip()
}
BASE_DIR = Path(os.environ.get("BASE_DIR", str(Path.home())))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "danger-full-access")
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "3600"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
MAX_TG = 4096
STREAM_INTERVAL = 2.0  # seconds between message edits
DATA_DIR = Path(__file__).parent / "data"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("codex-bot")


# ── State ─────────────────────────────────────────────────────

_active_procs: dict[int, asyncio.subprocess.Process] = {}
_user_locks: dict[int, asyncio.Lock] = {}


# ── Database ──────────────────────────────────────────────────


def _init_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DATA_DIR / "bot.db"), check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id  INTEGER PRIMARY KEY,
            cwd      TEXT DEFAULT '',
            model    TEXT DEFAULT '',
            updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            chat_id    INTEGER,
            bot_msg_id INTEGER,
            prompt     TEXT,
            response   TEXT,
            created    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_hist ON history(chat_id, bot_msg_id);
    """
    )
    conn.commit()
    return conn


db = _init_db()


def db_get_user(uid: int) -> dict:
    r = db.execute("SELECT cwd, model FROM users WHERE user_id=?", (uid,)).fetchone()
    if r:
        return {"cwd": r[0] or str(BASE_DIR), "model": r[1] or ""}
    return {"cwd": str(BASE_DIR), "model": ""}


def db_set_user(uid: int, **kw):
    cur = db_get_user(uid)
    cwd = kw.get("cwd", cur["cwd"])
    model = kw.get("model", cur["model"])
    db.execute(
        "INSERT INTO users(user_id,cwd,model) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET cwd=?,model=?,updated=CURRENT_TIMESTAMP",
        (uid, cwd, model, cwd, model),
    )
    db.commit()


def db_save(chat_id, bot_msg_id, uid, prompt, response):
    db.execute(
        "INSERT INTO history(user_id,chat_id,bot_msg_id,prompt,response) VALUES(?,?,?,?,?)",
        (uid, chat_id, bot_msg_id, prompt, response),
    )
    db.commit()


def db_get(chat_id, bot_msg_id) -> Optional[dict]:
    r = db.execute(
        "SELECT prompt, response FROM history WHERE chat_id=? AND bot_msg_id=?",
        (chat_id, bot_msg_id),
    ).fetchone()
    return {"prompt": r[0], "response": r[1]} if r else None


# ── Helpers ───────────────────────────────────────────────────


def allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS


def get_cwd(uid: int) -> Path:
    return Path(db_get_user(uid)["cwd"])


def get_model(uid: int) -> str:
    return db_get_user(uid)["model"] or CODEX_MODEL


def esc(text: str) -> str:
    """HTML-escape text for Telegram."""
    return _html.escape(str(text))


def strip_shell_wrapper(cmd: str) -> str:
    """Strip /bin/zsh -lc wrapper from codex commands."""
    if " -lc " in cmd:
        cmd = cmd.split(" -lc ", 1)[1].strip("'\"")
    return cmd


# ── HTML Formatting ───────────────────────────────────────────


def md_to_html(text: str) -> str:
    """Convert codex markdown output to Telegram-safe HTML.

    Handles code fences (with syntax highlighting), inline code,
    bold, italic, and links. Everything else is escaped.
    """
    if not text:
        return ""
    # Close unclosed code fences
    if text.count("```") % 2 != 0:
        text += "\n```"

    result = []
    parts = re.split(r"(```\w*\n.*?```)", text, flags=re.DOTALL)

    for part in parts:
        m = re.match(r"```(\w*)\n(.*?)```", part, re.DOTALL)
        if m:
            lang, code = m.group(1), m.group(2).rstrip("\n")
            cls = f' class="language-{esc(lang)}"' if lang else ""
            result.append(f"<pre><code{cls}>{esc(code)}</code></pre>")
        else:
            result.append(_inline_md(part))
    return "".join(result)


def _inline_md(text: str) -> str:
    """Convert inline markdown (code, bold, italic, links) to HTML."""
    phs: dict[str, str] = {}
    n = [0]

    def ph(html_content: str) -> str:
        key = f"\x00{n[0]}\x00"
        n[0] += 1
        phs[key] = html_content
        return key

    # Inline code
    text = re.sub(r"`([^`\n]+)`", lambda m: ph(f"<code>{esc(m.group(1))}</code>"), text)
    # Links [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        lambda m: ph(f'<a href="{m.group(2)}">{esc(m.group(1))}</a>'),
        text,
    )
    # Bold **text**
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: ph(f"<b>{esc(m.group(1))}</b>"), text)
    # Italic *text*
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        lambda m: ph(f"<i>{esc(m.group(1))}</i>"),
        text,
    )

    # Escape everything else
    text = esc(text)

    # Restore placeholders (\x00 not touched by html.escape)
    for k, v in phs.items():
        text = text.replace(k, v)
    return text


def extract_code_blocks(text: str) -> list[dict]:
    """Extract code blocks from raw markdown for copy buttons."""
    return [
        {"lang": m.group(1) or "code", "code": m.group(2).rstrip("\n")}
        for m in re.finditer(r"```(\w*)\n(.*?)```", text, flags=re.DOTALL)
    ]


def format_actions(calls: list[str]) -> str:
    """Format tool calls as a collapsed expandable blockquote."""
    if not calls:
        return ""
    items = "\n".join(f"<code>{esc(c)}</code>" for c in calls[-25:])
    return f"<blockquote expandable>🔧 <b>Actions ({len(calls)})</b>\n{items}</blockquote>\n\n"


def format_streaming(
    parts: list[str], calls: list[str], action: str, errors: list[str]
) -> str:
    """Build an in-progress streaming message."""
    out = []
    if errors:
        out.append(f"❌ {esc(errors[-1])}\n\n")
    if calls:
        out.append(format_actions(calls))
    text = "\n".join(parts)
    if text:
        out.append(md_to_html(text))
    if action:
        out.append(f"\n\n⏳ <code>{esc(action)}</code>")
    elif not text and not errors:
        out.append("⏳ <i>Thinking...</i>")
    return "".join(out) or "⏳ <i>Thinking...</i>"


def format_final(
    parts: list[str], calls: list[str], errors: list[str]
) -> str:
    """Build the completed response message."""
    out = []
    if errors:
        for e in errors:
            out.append(f"❌ <b>Error:</b> {esc(e)}")
        out.append("")
    if calls:
        out.append(format_actions(calls))
    text = "\n".join(parts)
    if text:
        out.append(md_to_html(text))
    elif not errors:
        out.append("<i>Codex finished with no output.</i>")
    return "\n".join(out).strip()


# ── Message Sending ───────────────────────────────────────────


async def safe_edit(msg, text: str, markup=None, retries: int = 2):
    """Edit a message with robust error handling."""
    if len(text) > MAX_TG:
        text = text[: MAX_TG - 40] + "\n\n<i>… (truncated)</i>"

    for attempt in range(retries + 1):
        try:
            await msg.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
            return
        except BadRequest as e:
            err = str(e).lower()
            if "not modified" in err:
                return
            if "can't parse" in err and attempt < retries:
                text = re.sub(r"<[^>]+>", "", text)
                continue
            log.warning(f"Edit failed: {e}")
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except (TimedOut, Exception) as e:
            if attempt < retries:
                await asyncio.sleep(1)
                continue
            log.warning(f"Edit failed: {e}")
            return


async def typing_loop(chat_id: int, bot):
    """Send typing indicator every 4 seconds until cancelled."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ── Action Buttons ────────────────────────────────────────────


def build_buttons(raw_text: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for a completed response."""
    rows = []

    # Copy buttons for short code blocks
    if HAS_COPY:
        blocks = extract_code_blocks(raw_text)
        copy_row = []
        for i, b in enumerate(blocks[:3]):
            if len(b["code"]) <= 256:
                copy_row.append(
                    InlineKeyboardButton(
                        f"📋 {b['lang'][:12]}",
                        copy_text=CopyTextButton(text=b["code"]),
                    )
                )
        if copy_row:
            rows.append(copy_row)

    # Action row
    rows.append(
        [
            InlineKeyboardButton("🔄 Rerun", callback_data="rr"),
            InlineKeyboardButton("💡 Explain", callback_data="ex"),
            InlineKeyboardButton("📄 File", callback_data="fl"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def cancel_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Cancel", callback_data="cancel")]]
    )


# ── Codex Runner ──────────────────────────────────────────────


async def run_codex_streaming(
    prompt: str,
    cwd: Path,
    model: str,
    uid: int,
    status_msg,
    bot,
) -> tuple[str, str]:
    """Run codex exec with real-time streaming updates.

    Returns (final_html, raw_text).
    """
    cmd = [
        "codex", "exec", "--json",
        "--sandbox", CODEX_SANDBOX,
        "-C", str(cwd),
        "--skip-git-repo-check",
        "--full-auto",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    log.info(f"Running: codex exec ... '{prompt[:80]}'")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except FileNotFoundError:
        msg = "❌ <code>codex</code> CLI not found. Is it installed?"
        await safe_edit(status_msg, msg)
        return msg, "codex CLI not found"

    _active_procs[uid] = proc

    output_parts: list[str] = []
    tool_calls: list[str] = []
    errors: list[str] = []
    current_action = ""
    last_edit = 0.0
    prev_html = ""

    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            item = event.get("item", {}) if isinstance(event.get("item"), dict) else {}
            itype = item.get("type", "")
            changed = False

            if etype == "item.started" and itype == "command_execution":
                current_action = f"Running: {strip_shell_wrapper(item.get('command', ''))[:100]}"
                changed = True

            elif etype == "item.completed":
                if itype == "agent_message":
                    text = item.get("text", "")
                    if text:
                        output_parts.append(text)
                        current_action = ""
                        changed = True
                elif itype == "command_execution":
                    tool_calls.append(f"$ {strip_shell_wrapper(item.get('command', ''))}")
                    current_action = ""
                    changed = True

            elif etype == "error":
                msg = event.get("message", "Unknown error")
                try:
                    msg = json.loads(msg).get("error", {}).get("message", msg)
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
                errors.append(msg)
                changed = True

            elif etype == "turn.failed":
                err = event.get("error", {})
                msg = err.get("message", "Turn failed") if isinstance(err, dict) else str(err)
                try:
                    msg = json.loads(msg).get("error", {}).get("message", msg)
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
                if msg not in errors:
                    errors.append(msg)
                changed = True

            # Stream update every STREAM_INTERVAL seconds
            if changed:
                now = time.time()
                if now - last_edit >= STREAM_INTERVAL:
                    html_text = format_streaming(output_parts, tool_calls, current_action, errors)
                    if html_text != prev_html:
                        await safe_edit(status_msg, html_text, cancel_button())
                        prev_html = html_text
                    last_edit = now

    except Exception as e:
        errors.append(f"Error reading output: {e}")
    finally:
        _active_procs.pop(uid, None)

    # Wait for process to exit
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()

    # Check stderr if no output and no errors
    if not output_parts and not errors:
        try:
            stderr = await proc.stderr.read()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                errors.append(stderr_text[:500])
        except Exception:
            pass

    raw_text = "\n".join(output_parts)
    final_html = format_final(output_parts, tool_calls, errors)
    return final_html, raw_text


async def run_codex_simple(prompt: str, cwd: Path, model: str) -> str:
    """Run codex without streaming (for inline mode). Returns raw text."""
    cmd = [
        "codex", "exec", "--json",
        "--sandbox", CODEX_SANDBOX,
        "-C", str(cwd),
        "--skip-git-repo-check",
        "--full-auto",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except FileNotFoundError:
        return "codex CLI not found"

    parts = []
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {}) if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "")
            if text:
                parts.append(text)

    await proc.wait()
    return "\n".join(parts) or "No output."


# ── Core Logic ────────────────────────────────────────────────


async def _run_and_respond(
    prompt: str,
    uid: int,
    chat_id: int,
    status_msg,
    context: ContextTypes.DEFAULT_TYPE,
    image_path: str = None,
):
    """Run codex, stream into status_msg, finalize with buttons."""
    cwd = get_cwd(uid)
    model = get_model(uid)

    # If an image was provided, add -i flag to prompt
    actual_prompt = prompt
    if image_path:
        actual_prompt = f"-i {image_path} {prompt}"

    typing_task = asyncio.create_task(typing_loop(chat_id, context.bot))

    try:
        final_html, raw_text = await asyncio.wait_for(
            run_codex_streaming(actual_prompt, cwd, model, uid, status_msg, context.bot),
            timeout=CODEX_TIMEOUT,
        )
    except asyncio.TimeoutError:
        final_html = "⏰ <b>Codex timed out.</b>"
        raw_text = "Codex timed out."
    except asyncio.CancelledError:
        final_html = "🛑 <b>Cancelled.</b>"
        raw_text = "Cancelled."
    except Exception as e:
        final_html = f"❌ <b>Error:</b> {esc(str(e))}"
        raw_text = str(e)
    finally:
        typing_task.cancel()

    buttons = build_buttons(raw_text)

    if len(final_html) > MAX_TG:
        # Send full response as file
        try:
            await context.bot.send_document(
                chat_id=chat_id,
                document=BytesIO(raw_text.encode()),
                filename="response.md",
                caption="📄 Full response attached",
            )
        except Exception as e:
            log.warning(f"Failed to send file: {e}")
        truncated = final_html[: MAX_TG - 60] + "\n\n📄 <i>Full response sent as file.</i>"
        await safe_edit(status_msg, truncated, buttons)
    else:
        await safe_edit(status_msg, final_html, buttons)

    # Persist for rerun/explain
    db_save(chat_id, status_msg.message_id, uid, prompt, raw_text)


# ── Command Handlers ──────────────────────────────────────────

MODELS = [
    ("gpt-5.4", "Flagship, 1M context"),
    ("gpt-5.4-mini", "Fast & cheap, 400K context"),
    ("gpt-5.3-codex", "Specialized coding"),
    ("gpt-5.3-codex-spark", "Near-instant coding (Pro)"),
    ("gpt-5.2-codex", "Advanced coding"),
    ("gpt-5.2", "General-purpose"),
    ("gpt-5.1-codex-max", "Long-horizon agentic"),
    ("gpt-5.1-codex", "Agentic coding"),
    ("gpt-5.1", "Coding/agentic"),
    ("o3", "Reasoning"),
    ("o4-mini", "Fast reasoning"),
]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        await update.message.reply_text("🔒 Not authorized.")
        return

    model = get_model(uid) or "<i>default</i>"
    cwd = get_cwd(uid)

    text = (
        f"🚀 <b>Codex Bot ready.</b>\n\n"
        f"📁 <code>{esc(str(cwd))}</code>\n"
        f"🤖 Model: <code>{model}</code>\n\n"
        f"Send any message to start coding.\n"
        f"Send 🎤 voice or 📷 photos too.\n\n"
        f"<b>Commands</b>\n"
        f"/new — Fresh conversation\n"
        f"/repo — Pick a project\n"
        f"/cd &lt;path&gt; — Change directory\n"
        f"/model — Switch model\n"
        f"/status — Current settings"
    )

    markup = None
    if WEBAPP_URL:
        from telegram import WebAppInfo

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🖥️ Open Terminal",
                        web_app=WebAppInfo(url=f"{WEBAPP_URL}/app/index.html"),
                    )
                ]
            ]
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        return
    context.user_data.clear()
    await update.message.reply_text("🆕 Fresh conversation started.")


async def cmd_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    projects_dir = BASE_DIR / "Desktop" / "Projects"
    if not projects_dir.exists():
        projects_dir = BASE_DIR

    try:
        folders = sorted(
            d.name
            for d in projects_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except PermissionError:
        await update.message.reply_text("❌ Can't read projects directory.")
        return

    if not folders:
        await update.message.reply_text("No project folders found.")
        return

    # Build button grid (2 columns)
    folder_map = {}
    rows = []
    row = []
    for f in folders[:30]:
        h = hashlib.md5(f.encode()).hexdigest()[:8]
        folder_map[h] = str(projects_dir / f)
        row.append(InlineKeyboardButton(f"📁 {f}", callback_data=f"cd:{h}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    context.user_data["repo_folders"] = folder_map

    await update.message.reply_text(
        f"📂 <b>Projects</b> in <code>{esc(str(projects_dir))}</code>\n\nTap to switch:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.message.reply_text(
            f"📁 <code>{esc(str(get_cwd(uid)))}</code>\n\n"
            f"Usage: <code>/cd /path/to/project</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    target = Path(args).expanduser().resolve()
    for base in [None, BASE_DIR, BASE_DIR / "Desktop" / "Projects"]:
        if base:
            target = (base / args).resolve()
        if target.exists() and target.is_dir():
            db_set_user(uid, cwd=str(target))
            await update.message.reply_text(
                f"📁 → <code>{esc(str(target))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    await update.message.reply_text(
        f"❌ Not found: <code>{esc(args)}</code>", parse_mode=ParseMode.HTML
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    if context.args:
        new_model = context.args[0]
        db_set_user(uid, model=new_model)
        await update.message.reply_text(
            f"🤖 Model → <code>{esc(new_model)}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        current = get_model(uid) or "default"
        lines = [f"🤖 Current: <code>{esc(current)}</code>\n"]
        for mid, desc in MODELS:
            marker = " ◀" if mid == current else ""
            lines.append(f"  <code>{esc(mid)}</code> — {esc(desc)}{marker}")
        lines.append(f"\n<code>/model gpt-5.4</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    await update.message.reply_text(
        f"🤖 Model: <code>{esc(get_model(uid) or 'default')}</code>\n"
        f"📁 Dir: <code>{esc(str(get_cwd(uid)))}</code>\n"
        f"🔒 Sandbox: <code>{esc(CODEX_SANDBOX)}</code>\n"
        f"🎤 Voice: {'✅' if HAS_OPENAI else '❌ needs OPENAI_API_KEY'}\n"
        f"👤 ID: <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
    )


# ── Message Handler ───────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        await update.message.reply_text("🔒 Not authorized.")
        return

    prompt = update.message.text
    if not prompt:
        return

    # Per-user lock to prevent overload
    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    if _user_locks[uid].locked():
        await update.message.reply_text(
            "⏳ <i>Still working on your previous request. "
            "Tap 🛑 Cancel to stop it.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    async with _user_locks[uid]:
        status_msg = await update.message.reply_text(
            "⏳ <i>Thinking...</i>", parse_mode=ParseMode.HTML
        )
        await _run_and_respond(prompt, uid, update.effective_chat.id, status_msg, context)


# ── Photo Handler ─────────────────────────────────────────────


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    if _user_locks[uid].locked():
        await update.message.reply_text(
            "⏳ <i>Still working…</i>", parse_mode=ParseMode.HTML
        )
        return

    # Download highest-res photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    DATA_DIR.mkdir(exist_ok=True)
    temp_path = str(DATA_DIR / f"photo_{uid}_{int(time.time())}.jpg")
    await tg_file.download_to_drive(temp_path)

    caption = update.message.caption or "Look at this image and help me with what you see."

    async with _user_locks[uid]:
        status_msg = await update.message.reply_text(
            "📷 <i>Analyzing image...</i>", parse_mode=ParseMode.HTML
        )

        # Build codex command with -i flag for image
        cwd = get_cwd(uid)
        model = get_model(uid)
        cmd = [
            "codex", "exec", "--json",
            "--sandbox", CODEX_SANDBOX,
            "-C", str(cwd),
            "--skip-git-repo-check",
            "--full-auto",
            "-i", temp_path,
        ]
        if model:
            cmd += ["--model", model]
        cmd.append(caption)

        typing_task = asyncio.create_task(typing_loop(update.effective_chat.id, context.bot))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            )
            _active_procs[uid] = proc

            output_parts, tool_calls, errors = [], [], []
            current_action, last_edit, prev_html = "", 0.0, ""

            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                item = event.get("item", {}) if isinstance(event.get("item"), dict) else {}
                itype = item.get("type", "")
                changed = False

                if etype == "item.completed" and itype == "agent_message":
                    t = item.get("text", "")
                    if t:
                        output_parts.append(t)
                        current_action = ""
                        changed = True
                elif etype == "item.completed" and itype == "command_execution":
                    tool_calls.append(f"$ {strip_shell_wrapper(item.get('command', ''))}")
                    current_action = ""
                    changed = True
                elif etype == "item.started" and itype == "command_execution":
                    current_action = f"Running: {strip_shell_wrapper(item.get('command', ''))[:100]}"
                    changed = True
                elif etype == "error":
                    msg = event.get("message", "Unknown error")
                    try:
                        msg = json.loads(msg).get("error", {}).get("message", msg)
                    except Exception:
                        pass
                    errors.append(msg)
                    changed = True

                if changed:
                    now = time.time()
                    if now - last_edit >= STREAM_INTERVAL:
                        html_text = format_streaming(output_parts, tool_calls, current_action, errors)
                        if html_text != prev_html:
                            await safe_edit(status_msg, html_text, cancel_button())
                            prev_html = html_text
                        last_edit = now

            _active_procs.pop(uid, None)
            await proc.wait()

            raw_text = "\n".join(output_parts)
            final_html = format_final(output_parts, tool_calls, errors)

        except Exception as e:
            final_html = f"❌ <b>Error:</b> {esc(str(e))}"
            raw_text = str(e)
        finally:
            typing_task.cancel()
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        buttons = build_buttons(raw_text)
        if len(final_html) > MAX_TG:
            try:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=BytesIO(raw_text.encode()),
                    filename="response.md",
                    caption="📄 Full response",
                )
            except Exception:
                pass
            truncated = final_html[: MAX_TG - 60] + "\n\n📄 <i>Full response sent as file.</i>"
            await safe_edit(status_msg, truncated, buttons)
        else:
            await safe_edit(status_msg, final_html, buttons)

        db_save(update.effective_chat.id, status_msg.message_id, uid, caption, raw_text)


# ── Document Handler ──────────────────────────────────────────


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    doc = update.message.document
    if not doc:
        return

    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    if _user_locks[uid].locked():
        await update.message.reply_text(
            "⏳ <i>Still working…</i>", parse_mode=ParseMode.HTML
        )
        return

    # Download to temp location
    DATA_DIR.mkdir(exist_ok=True)
    save_path = DATA_DIR / (doc.file_name or "uploaded_file")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(str(save_path))

    caption = update.message.caption or f"Review this file and help me: {doc.file_name}"
    prompt = f"{caption}\n\n[File saved at: {save_path}]"

    async with _user_locks[uid]:
        status_msg = await update.message.reply_text(
            f"📎 <i>Processing {esc(doc.file_name or 'file')}...</i>",
            parse_mode=ParseMode.HTML,
        )
        await _run_and_respond(prompt, uid, update.effective_chat.id, status_msg, context)

    # Cleanup
    try:
        save_path.unlink()
    except OSError:
        pass


# ── Voice Handler ─────────────────────────────────────────────


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return

    if not HAS_OPENAI:
        await update.message.reply_text(
            "🎤 Voice needs <code>OPENAI_API_KEY</code> in .env "
            "and <code>pip install openai</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    if _user_locks[uid].locked():
        await update.message.reply_text(
            "⏳ <i>Still working…</i>", parse_mode=ParseMode.HTML
        )
        return

    # Download
    tg_file = await voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        await tg_file.download_to_drive(f.name)
        temp_path = f.name

    # Transcribe
    try:
        client = _openai.OpenAI()
        with open(temp_path, "rb") as audio:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio)
        text = transcript.text.strip()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Transcription failed: {esc(str(e))}", parse_mode=ParseMode.HTML
        )
        return
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    if not text:
        await update.message.reply_text(
            "🎤 <i>Couldn't understand audio.</i>", parse_mode=ParseMode.HTML
        )
        return

    # Show transcription
    await update.message.reply_text(f"🎤 <i>{esc(text)}</i>", parse_mode=ParseMode.HTML)

    # Process as prompt
    async with _user_locks[uid]:
        status_msg = await update.message.reply_text(
            "⏳ <i>Thinking...</i>", parse_mode=ParseMode.HTML
        )
        await _run_and_respond(text, uid, update.effective_chat.id, status_msg, context)


# ── Callback Handler ──────────────────────────────────────────


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not allowed(uid):
        await query.answer("Not authorized.")
        return

    data = query.data
    msg = query.message

    # ── Cancel running process
    if data == "cancel":
        proc = _active_procs.get(uid)
        if proc:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            await query.answer("🛑 Cancelled.")
        else:
            await query.answer("Nothing running.")
        return

    # ── Rerun last prompt
    if data == "rr":
        saved = db_get(msg.chat_id, msg.message_id)
        if not saved:
            await query.answer("Original prompt not found.")
            return
        await query.answer("🔄 Rerunning...")

        if uid not in _user_locks:
            _user_locks[uid] = asyncio.Lock()
        if _user_locks[uid].locked():
            await query.answer("Still processing another request.")
            return

        async with _user_locks[uid]:
            await safe_edit(msg, "⏳ <i>Rerunning...</i>")
            cwd = get_cwd(uid)
            model = get_model(uid)
            typing_task = asyncio.create_task(typing_loop(msg.chat_id, context.bot))
            try:
                final_html, raw_text = await asyncio.wait_for(
                    run_codex_streaming(saved["prompt"], cwd, model, uid, msg, context.bot),
                    timeout=CODEX_TIMEOUT,
                )
            except asyncio.TimeoutError:
                final_html = "⏰ <b>Timed out.</b>"
                raw_text = "Timed out."
            except Exception as e:
                final_html = f"❌ <b>Error:</b> {esc(str(e))}"
                raw_text = str(e)
            finally:
                typing_task.cancel()

            buttons = build_buttons(raw_text)
            await safe_edit(msg, final_html, buttons)
            db_save(msg.chat_id, msg.message_id, uid, saved["prompt"], raw_text)
        return

    # ── Explain response
    if data == "ex":
        saved = db_get(msg.chat_id, msg.message_id)
        if not saved:
            await query.answer("Context not found.")
            return
        await query.answer("💡 Explaining...")

        explain_prompt = (
            f"Explain this simply and concisely. "
            f"Original question: {saved['prompt']}\n\n"
            f"Response to explain:\n{saved['response'][:2000]}"
        )

        if uid not in _user_locks:
            _user_locks[uid] = asyncio.Lock()
        if _user_locks[uid].locked():
            await query.answer("Still processing.")
            return

        async with _user_locks[uid]:
            status_msg = await context.bot.send_message(
                msg.chat_id,
                "💡 <i>Explaining...</i>",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg.message_id,
            )
            await _run_and_respond(
                explain_prompt, uid, msg.chat_id, status_msg, context
            )
        return

    # ── Send as file
    if data == "fl":
        saved = db_get(msg.chat_id, msg.message_id)
        if not saved:
            await query.answer("Content not found.")
            return
        await query.answer("📄 Sending...")
        try:
            await context.bot.send_document(
                chat_id=msg.chat_id,
                document=BytesIO(saved["response"].encode()),
                filename="response.md",
                caption=f"📄 {saved['prompt'][:100]}",
            )
        except Exception as e:
            log.warning(f"Send file failed: {e}")
        return

    # ── Project folder selection
    if data.startswith("cd:"):
        folder_hash = data[3:]
        folders = context.user_data.get("repo_folders", {})
        path = folders.get(folder_hash)
        if path and Path(path).exists():
            db_set_user(uid, cwd=path)
            name = Path(path).name
            await query.answer(f"→ {name}")
            await safe_edit(msg, f"📁 Switched to <code>{esc(path)}</code>")
        else:
            await query.answer("Folder not found.")
        return


# ── Inline Mode ───────────────────────────────────────────────


async def handle_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query
    uid = query.from_user.id
    if not allowed(uid):
        return

    text = query.query.strip()
    if len(text) < 3:
        return

    model = get_model(uid)
    try:
        result = await asyncio.wait_for(
            run_codex_simple(text, get_cwd(uid), model), timeout=25
        )
    except asyncio.TimeoutError:
        result = "⏰ Too slow for inline — try the bot's DM for complex tasks."
    except Exception as e:
        result = f"Error: {e}"

    html_result = md_to_html(result)[: MAX_TG]

    results = [
        InlineQueryResultArticle(
            id=hashlib.md5(f"{text}{time.time()}".encode()).hexdigest(),
            title="Codex Response",
            description=result[:150].replace("\n", " "),
            input_message_content=InputTextMessageContent(
                html_result, parse_mode=ParseMode.HTML
            ),
        )
    ]
    await query.answer(results, cache_time=0, is_personal=True)


# ── Main ──────────────────────────────────────────────────────


async def post_init(app: Application):
    cmds = [
        BotCommand("start", "Initialize bot"),
        BotCommand("new", "Fresh conversation"),
        BotCommand("repo", "Pick a project"),
        BotCommand("cd", "Change directory"),
        BotCommand("model", "Switch model"),
        BotCommand("status", "Current settings"),
    ]
    await app.bot.set_my_commands(cmds)

    if WEBAPP_URL:
        from telegram import MenuButtonWebApp, WebAppInfo

        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Terminal",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/app/index.html"),
                )
            )
            log.info(f"Menu button set → {WEBAPP_URL}")
        except Exception as e:
            log.warning(f"Failed to set menu button: {e}")

    log.info("Bot initialized")


def main():
    log.info("Starting Codex Telegram Bot")
    log.info(f"Allowed users: {ALLOWED_USERS or 'everyone'}")
    log.info(f"Base dir: {BASE_DIR}")
    log.info(f"Model: {CODEX_MODEL or '(default)'}")
    log.info(f"Voice: {'yes' if HAS_OPENAI else 'no'}")
    log.info(f"Mini App: {WEBAPP_URL or 'disabled'}")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("repo", cmd_repo))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))

    # Content handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Interactive
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(InlineQueryHandler(handle_inline))

    log.info("Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
