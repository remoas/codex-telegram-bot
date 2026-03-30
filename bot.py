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
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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
        CREATE TABLE IF NOT EXISTS skills (
            user_id   INTEGER,
            skill_id  TEXT,
            enabled   INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, skill_id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            user_id    INTEGER,
            cwd        TEXT,
            thread_id  TEXT,
            updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, cwd)
        );
        CREATE TABLE IF NOT EXISTS custom_skills (
            user_id  INTEGER,
            skill_id TEXT,
            name     TEXT,
            icon     TEXT,
            prompt   TEXT,
            PRIMARY KEY (user_id, skill_id)
        );
        CREATE TABLE IF NOT EXISTS pins (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            cwd      TEXT,
            text     TEXT,
            created  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_pins ON pins(user_id, cwd);
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


# ── Sessions (conversation context) ───────────────────────────


def db_get_session(uid: int, cwd: str) -> Optional[str]:
    """Get the codex thread_id for a user+cwd pair."""
    r = db.execute(
        "SELECT thread_id FROM sessions WHERE user_id=? AND cwd=?", (uid, cwd)
    ).fetchone()
    return r[0] if r else None


def db_set_session(uid: int, cwd: str, thread_id: str):
    db.execute(
        "INSERT INTO sessions(user_id,cwd,thread_id) VALUES(?,?,?) "
        "ON CONFLICT(user_id,cwd) DO UPDATE SET thread_id=?,updated=CURRENT_TIMESTAMP",
        (uid, cwd, thread_id, thread_id),
    )
    db.commit()


def db_clear_session(uid: int, cwd: str = None):
    """Clear session(s) for a user. If cwd is None, clear all."""
    if cwd:
        db.execute("DELETE FROM sessions WHERE user_id=? AND cwd=?", (uid, cwd))
    else:
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    db.commit()


# ── Pins (per-project persistent context) ─────────────────────


def db_add_pin(uid: int, cwd: str, text: str) -> int:
    cur = db.execute(
        "INSERT INTO pins(user_id,cwd,text) VALUES(?,?,?)", (uid, cwd, text)
    )
    db.commit()
    return cur.lastrowid


def db_get_pins(uid: int, cwd: str) -> list[tuple[int, str]]:
    rows = db.execute(
        "SELECT id, text FROM pins WHERE user_id=? AND cwd=? ORDER BY id",
        (uid, cwd),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def db_del_pin(uid: int, pin_id: int):
    db.execute("DELETE FROM pins WHERE id=? AND user_id=?", (pin_id, uid))
    db.commit()


def build_pin_context(uid: int, cwd: str) -> str:
    """Build context string from all pins for this project."""
    pins = db_get_pins(uid, cwd)
    if not pins:
        return ""
    lines = ["[Project context — always follow these instructions:]"]
    for _, text in pins:
        lines.append(f"- {text}")
    return "\n".join(lines) + "\n\n"


# ── URL Fetching ──────────────────────────────────────────────


async def fetch_url_text(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return plain text content."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CodexTelegramBot/1.0"})
        result = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
        )
        # Strip HTML tags for a rough plain text extraction
        text = re.sub(r"<script[^>]*>.*?</script>", "", result, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[Failed to fetch {url}: {e}]"


def extract_urls(text: str) -> list[str]:
    """Extract HTTP(S) URLs from text."""
    return re.findall(r"https?://[^\s<>\"')\]]+", text)


# ── Shell Execution ───────────────────────────────────────────


async def run_shell(cmd: str, cwd: Path, timeout: int = 60) -> str:
    """Run a shell command and return output."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace").strip()
        code = proc.returncode
        return f"{output}\n\n[exit {code}]" if output else f"[exit {code}]"
    except asyncio.TimeoutError:
        proc.kill()
        return "[timed out]"
    except Exception as e:
        return f"[error: {e}]"


# ── Skills ────────────────────────────────────────────────────

BUILT_IN_SKILLS = {
    "review": {
        "name": "Review",
        "icon": "🔍",
        "desc": "Review code changes",
        "prompt": "Review the recent code changes in this repository. Focus on bugs, security, performance, and readability. Be thorough but concise.",
    },
    "test": {
        "name": "Tests",
        "icon": "🧪",
        "desc": "Run and analyze tests",
        "prompt": "Run the test suite for this project. Report results, analyze any failures, and suggest fixes.",
    },
    "git": {
        "name": "Git Status",
        "icon": "📊",
        "desc": "Git overview",
        "prompt": "Show git status, recent commits (last 5), current branch, and any uncommitted changes. Summarize concisely.",
    },
    "deploy": {
        "name": "Deploy",
        "icon": "🚀",
        "desc": "Help deploy project",
        "prompt": "Help me deploy this project. Check for any issues first, then walk me through the deployment steps.",
    },
    "docs": {
        "name": "Docs",
        "icon": "📝",
        "desc": "Generate documentation",
        "prompt": "Generate or update documentation for this project. Focus on README, API docs, and inline comments.",
    },
    "security": {
        "name": "Security",
        "icon": "🔐",
        "desc": "Security audit",
        "prompt": "Audit this codebase for security vulnerabilities. Check for common issues: injection, auth flaws, exposed secrets, dependency vulns.",
    },
    "refactor": {
        "name": "Refactor",
        "icon": "🎨",
        "desc": "Suggest improvements",
        "prompt": "Analyze this codebase and suggest refactoring improvements. Focus on code quality, DRY principles, and maintainability.",
    },
    "explain": {
        "name": "Explain",
        "icon": "💬",
        "desc": "Explain the codebase",
        "prompt": "Explain how this codebase works at a high level. Describe the architecture, key files, and data flow.",
    },
    "debug": {
        "name": "Debug",
        "icon": "🐛",
        "desc": "Debug recent errors",
        "prompt": "Help me debug the most recent error or issue in this project. Check logs, recent changes, and common failure points.",
    },
    "deps": {
        "name": "Dependencies",
        "icon": "📦",
        "desc": "Check dependencies",
        "prompt": "Check for outdated or vulnerable dependencies in this project. Suggest updates and flag any breaking changes.",
    },
    "pr": {
        "name": "PR Prep",
        "icon": "✅",
        "desc": "Prepare pull request",
        "prompt": "Prepare a pull request: summarize all changes since the base branch, check for issues, and draft a PR description.",
    },
    "scaffold": {
        "name": "Scaffold",
        "icon": "🏗️",
        "desc": "Create new components",
        "prompt": "Help me scaffold a new component or module for this project. Ask me what I need, then generate the boilerplate.",
    },
}

# Default skills enabled for new users
DEFAULT_SKILLS = ["review", "test", "git", "explain", "debug", "pr"]


def db_get_enabled_skills(uid: int) -> list[str]:
    """Get list of enabled skill IDs for a user."""
    rows = db.execute(
        "SELECT skill_id FROM skills WHERE user_id=? AND enabled=1", (uid,)
    ).fetchall()
    if rows:
        return [r[0] for r in rows]
    # First time: enable defaults
    for sid in DEFAULT_SKILLS:
        db.execute(
            "INSERT OR IGNORE INTO skills(user_id,skill_id,enabled) VALUES(?,?,1)",
            (uid, sid),
        )
    db.commit()
    return list(DEFAULT_SKILLS)


def db_toggle_skill(uid: int, skill_id: str) -> bool:
    """Toggle a skill on/off. Returns new enabled state."""
    r = db.execute(
        "SELECT enabled FROM skills WHERE user_id=? AND skill_id=?",
        (uid, skill_id),
    ).fetchone()
    new_state = 0 if (r and r[0]) else 1
    db.execute(
        "INSERT INTO skills(user_id,skill_id,enabled) VALUES(?,?,?) "
        "ON CONFLICT(user_id,skill_id) DO UPDATE SET enabled=?",
        (uid, skill_id, new_state, new_state),
    )
    db.commit()
    return bool(new_state)


def db_get_custom_skills(uid: int) -> dict:
    """Get user's custom skills."""
    rows = db.execute(
        "SELECT skill_id, name, icon, prompt FROM custom_skills WHERE user_id=?",
        (uid,),
    ).fetchall()
    return {
        r[0]: {"name": r[1], "icon": r[2], "desc": "Custom skill", "prompt": r[3]}
        for r in rows
    }


def db_save_custom_skill(uid: int, skill_id: str, name: str, icon: str, prompt: str):
    db.execute(
        "INSERT OR REPLACE INTO custom_skills(user_id,skill_id,name,icon,prompt) VALUES(?,?,?,?,?)",
        (uid, skill_id, name, icon, prompt),
    )
    # Auto-enable
    db.execute(
        "INSERT OR REPLACE INTO skills(user_id,skill_id,enabled) VALUES(?,?,1)",
        (uid, skill_id),
    )
    db.commit()


def db_delete_custom_skill(uid: int, skill_id: str):
    db.execute(
        "DELETE FROM custom_skills WHERE user_id=? AND skill_id=?", (uid, skill_id)
    )
    db.execute(
        "DELETE FROM skills WHERE user_id=? AND skill_id=?", (uid, skill_id)
    )
    db.commit()


def get_all_skills(uid: int) -> dict:
    """Get all skills (built-in + custom) for a user."""
    skills = dict(BUILT_IN_SKILLS)
    skills.update(db_get_custom_skills(uid))
    return skills


# ── Reply Keyboard ────────────────────────────────────────────


def build_quick_keyboard(uid: int) -> ReplyKeyboardMarkup:
    """Build the persistent reply keyboard with enabled skills."""
    enabled = db_get_enabled_skills(uid)
    all_skills = get_all_skills(uid)

    buttons = []
    row = []
    for sid in enabled:
        skill = all_skills.get(sid)
        if not skill:
            continue
        row.append(KeyboardButton(f"{skill['icon']} {skill['name']}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Bottom row: always-available actions
    buttons.append([
        KeyboardButton("🛠️ Skills"),
        KeyboardButton("⚙️ Settings"),
    ])

    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Message or tap a skill...",
    )


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
    thread_id: str = None,
    image_path: str = None,
) -> tuple[str, str, str]:
    """Run codex exec with real-time streaming updates.

    Returns (final_html, raw_text, thread_id).
    Uses session resume if thread_id is provided for conversation context.
    """
    if thread_id:
        # Resume existing conversation
        cmd = [
            "codex", "exec", "resume", "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            thread_id,
        ]
        if model:
            cmd += ["--model", model]
        if image_path:
            cmd += ["-i", image_path]
        cmd.append(prompt)
        log.info(f"Resuming session {thread_id[:12]}... '{prompt[:80]}'")
    else:
        # New conversation
        cmd = [
            "codex", "exec", "--json",
            "-C", str(cwd),
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if model:
            cmd += ["--model", model]
        if image_path:
            cmd += ["-i", image_path]
        cmd.append(prompt)
        log.info(f"New session: codex exec ... '{prompt[:80]}'")

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
        return msg, "codex CLI not found", ""

    _active_procs[uid] = proc

    output_parts: list[str] = []
    tool_calls: list[str] = []
    errors: list[str] = []
    current_action = ""
    last_edit = 0.0
    prev_html = ""
    result_thread_id = thread_id or ""

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

            # Capture thread_id for session persistence
            if etype == "thread.started":
                tid = event.get("thread_id", "")
                if tid:
                    result_thread_id = tid
                continue

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
    return final_html, raw_text, result_thread_id


async def run_codex_simple(prompt: str, cwd: Path, model: str) -> str:
    """Run codex without streaming (for inline mode). Returns raw text."""
    cmd = [
        "codex", "exec", "--json",
        "-C", str(cwd),
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
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
    cwd_str = str(cwd)

    # Prepend pinned context for this project
    pin_ctx = build_pin_context(uid, cwd_str)
    if pin_ctx:
        prompt = pin_ctx + prompt

    # Fetch any URLs in the message and include as context
    urls = extract_urls(prompt)
    if urls:
        url_parts = []
        for url in urls[:3]:  # Max 3 URLs
            content = await fetch_url_text(url)
            if content and not content.startswith("[Failed"):
                url_parts.append(f"[Content from {url}:]\n{content}\n")
        if url_parts:
            prompt = "\n".join(url_parts) + "\n" + prompt

    # Look up existing session for conversation context
    thread_id = db_get_session(uid, cwd_str)

    typing_task = asyncio.create_task(typing_loop(chat_id, context.bot))
    new_thread_id = ""

    try:
        final_html, raw_text, new_thread_id = await asyncio.wait_for(
            run_codex_streaming(
                prompt, cwd, model, uid, status_msg, context.bot,
                thread_id=thread_id,
                image_path=image_path,
            ),
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

    # Save session thread_id for conversation continuity
    if new_thread_id:
        db_set_session(uid, cwd_str, new_thread_id)

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
    n_skills = len(db_get_enabled_skills(uid))

    text = (
        f"🚀 <b>Codex Bot ready.</b>\n\n"
        f"📁 <code>{esc(str(cwd))}</code>\n"
        f"🤖 Model: <code>{model}</code>\n"
        f"🛠️ {n_skills} skills active\n\n"
        f"Send any message, 🎤 voice, or 📷 photos.\n"
        f"Tap a skill button below for quick actions.\n\n"
        f"/setup — Guided setup wizard\n"
        f"/skills — Manage skills\n"
        f"/repo — Pick project · /model — Switch model"
    )

    # Show reply keyboard with quick actions
    kb = build_quick_keyboard(uid)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    # Also show Mini App button if configured
    if WEBAPP_URL:
        from telegram import WebAppInfo

        await update.message.reply_text(
            "🖥️ Or open the full terminal:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "Open Terminal",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/app/index.html"),
                )]]
            ),
        )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    cwd_str = str(get_cwd(uid))
    db_clear_session(uid, cwd_str)
    context.user_data.pop("creating_skill", None)
    await update.message.reply_text(
        "🆕 Fresh conversation started.\n"
        "<i>Codex will forget previous context in this project.</i>",
        parse_mode=ParseMode.HTML,
    )


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

    # Try resolving against known bases
    candidates = [
        Path(args).expanduser().resolve(),
        (BASE_DIR / args).resolve(),
        (BASE_DIR / "Desktop" / "Projects" / args).resolve(),
    ]

    for target in candidates:
        if target.exists() and target.is_dir():
            # Security: confirm path is under BASE_DIR (prevent traversal)
            try:
                target.resolve().relative_to(BASE_DIR.resolve())
            except ValueError:
                await update.message.reply_text(
                    f"🔒 Access denied: must be within <code>{esc(str(BASE_DIR))}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

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
        f"🔓 Sandbox: <code>fully unrestricted</code>\n"
        f"🎤 Voice: {'✅' if HAS_OPENAI else '❌ needs OPENAI_API_KEY'}\n"
        f"👤 ID: <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skill shop: toggle built-in + custom skills."""
    uid = update.effective_user.id
    if not allowed(uid):
        return
    await _show_skill_shop(uid, update.message, context)


async def _show_skill_shop(uid: int, target, context, edit: bool = False):
    """Render the skill shop — tap a skill to see what it does."""
    enabled = set(db_get_enabled_skills(uid))
    all_skills = get_all_skills(uid)
    custom_ids = set(db_get_custom_skills(uid).keys())

    rows = []
    # Built-in skills (one per row for readability)
    for sid, skill in BUILT_IN_SKILLS.items():
        status = "✅" if sid in enabled else "➖"
        rows.append([
            InlineKeyboardButton(
                f"{status} {skill['icon']} {skill['name']} — {skill['desc']}",
                callback_data=f"ski:{sid}",
            )
        ])

    # Custom skills
    if custom_ids:
        for sid in custom_ids:
            skill = all_skills[sid]
            status = "✅" if sid in enabled else "➖"
            rows.append([
                InlineKeyboardButton(
                    f"{status} {skill['icon']} {skill['name']} — {skill['desc']}",
                    callback_data=f"ski:{sid}",
                )
            ])

    # Action buttons
    rows.append([
        InlineKeyboardButton("➕ Create Skill", callback_data="sknew"),
        InlineKeyboardButton("✅ Done", callback_data="skdone"),
    ])

    text = (
        "🛠️ <b>Skill Shop</b>\n\n"
        "Tap a skill to see details and enable/disable it.\n"
        "Enabled skills appear as quick-action buttons below your chat.\n"
    )

    markup = InlineKeyboardMarkup(rows)
    if edit and hasattr(target, "edit_text"):
        await safe_edit(target, text, markup)
    else:
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _show_skill_detail(uid: int, skill_id: str, msg, context):
    """Show a skill's detail view with description, prompt preview, and toggle."""
    all_skills = get_all_skills(uid)
    skill = all_skills.get(skill_id)
    if not skill:
        return

    enabled = skill_id in set(db_get_enabled_skills(uid))
    is_custom = skill_id in db_get_custom_skills(uid)
    toggle_label = "❌ Disable" if enabled else "✅ Enable"

    # Truncate prompt preview
    prompt_preview = skill["prompt"]
    if len(prompt_preview) > 200:
        prompt_preview = prompt_preview[:200] + "…"

    text = (
        f"{skill['icon']} <b>{esc(skill['name'])}</b>\n\n"
        f"{esc(skill['desc'])}\n\n"
        f"<b>What it does:</b>\n"
        f"<blockquote>{esc(prompt_preview)}</blockquote>\n\n"
        f"Status: {'✅ Enabled' if enabled else '➖ Disabled'}"
    )

    rows = [
        [InlineKeyboardButton(toggle_label, callback_data=f"sk:{skill_id}")],
    ]
    if is_custom:
        rows[0].append(
            InlineKeyboardButton("🗑️ Delete", callback_data=f"skdel:{skill_id}")
        )
    rows.append([InlineKeyboardButton("← Back to Skills", callback_data="skback")])

    await safe_edit(msg, text, InlineKeyboardMarkup(rows))


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guided setup wizard — phone-friendly configuration."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    cwd = get_cwd(uid)
    model = get_model(uid) or "default"
    n_skills = len(db_get_enabled_skills(uid))

    rows = [
        [InlineKeyboardButton(f"📁 Project: {Path(str(cwd)).name}", callback_data="setup:repo")],
        [InlineKeyboardButton(f"🤖 Model: {model}", callback_data="setup:model")],
        [InlineKeyboardButton(f"🛠️ Skills: {n_skills} active", callback_data="setup:skills")],
        [InlineKeyboardButton("✅ Done — Show Quick Actions", callback_data="setup:done")],
    ]

    await update.message.reply_text(
        "🚀 <b>Setup Wizard</b>\n\n"
        "Configure your bot from right here.\n"
        "Tap any option to change it:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle the reply keyboard on/off."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    if context.user_data.get("keyboard_hidden"):
        context.user_data["keyboard_hidden"] = False
        kb = build_quick_keyboard(uid)
        await update.message.reply_text("⌨️ Keyboard shown.", reply_markup=kb)
    else:
        context.user_data["keyboard_hidden"] = True
        await update.message.reply_text(
            "⌨️ Keyboard hidden. Use /kb to bring it back.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ── Pin Commands ──────────────────────────────────────────────


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pin persistent context for the current project."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "📌 Usage: <code>/pin Always use TypeScript strict mode</code>\n\n"
            "Pinned instructions are sent to codex with every message in this project.",
            parse_mode=ParseMode.HTML,
        )
        return

    cwd_str = str(get_cwd(uid))
    pin_id = db_add_pin(uid, cwd_str, text)
    count = len(db_get_pins(uid, cwd_str))
    await update.message.reply_text(
        f"📌 Pinned (#{pin_id}):\n<i>{esc(text)}</i>\n\n"
        f"{count} pin{'s' if count != 1 else ''} active for this project.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_pins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all pins for the current project."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    cwd_str = str(get_cwd(uid))
    pins = db_get_pins(uid, cwd_str)
    if not pins:
        await update.message.reply_text(
            "📌 No pins for this project.\n"
            "Use <code>/pin your instruction here</code> to add one.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📌 <b>Pins for</b> <code>{esc(Path(cwd_str).name)}</code>\n"]
    for pin_id, text in pins:
        lines.append(f"  #{pin_id}: <i>{esc(text)}</i>")
    lines.append(f"\nRemove with <code>/unpin #</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a pin by ID."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/unpin 3</code>", parse_mode=ParseMode.HTML)
        return

    try:
        pin_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("❌ Pin ID must be a number.")
        return

    db_del_pin(uid, pin_id)
    await update.message.reply_text(f"📌 Pin #{pin_id} removed.")


# ── Run Command (direct shell) ────────────────────────────────


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a shell command directly without codex."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    cmd_text = " ".join(context.args) if context.args else ""
    if not cmd_text:
        await update.message.reply_text(
            "⚡ Usage: <code>/run npm test</code>\n\n"
            "Runs the command directly in your project directory. No AI involved.",
            parse_mode=ParseMode.HTML,
        )
        return

    cwd = get_cwd(uid)
    status_msg = await update.message.reply_text(
        f"⚡ <code>$ {esc(cmd_text)}</code>", parse_mode=ParseMode.HTML
    )

    output = await run_shell(cmd_text, cwd, timeout=120)

    # Format output
    if len(output) > MAX_TG - 200:
        # Send as file
        try:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=BytesIO(output.encode()),
                filename="output.txt",
                caption=f"$ {cmd_text}",
            )
        except Exception:
            pass
        short = output[:500] + "\n…\n" + output[-200:]
        await safe_edit(status_msg, f"⚡ <code>$ {esc(cmd_text)}</code>\n\n<pre>{esc(short)}</pre>")
    else:
        await safe_edit(status_msg, f"⚡ <code>$ {esc(cmd_text)}</code>\n\n<pre>{esc(output)}</pre>")


# ── Search History ────────────────────────────────────────────


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search conversation history."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "🔍 Usage: <code>/search auth bug</code>\n\n"
            "Searches your conversation history.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Search prompts and responses
    pattern = f"%{query}%"
    rows = db.execute(
        "SELECT prompt, response, created FROM history "
        "WHERE user_id=? AND (prompt LIKE ? OR response LIKE ?) "
        "ORDER BY created DESC LIMIT 5",
        (uid, pattern, pattern),
    ).fetchall()

    if not rows:
        await update.message.reply_text(f"🔍 No results for <i>{esc(query)}</i>", parse_mode=ParseMode.HTML)
        return

    lines = [f"🔍 <b>Results for</b> <i>{esc(query)}</i>\n"]
    for prompt, response, created in rows:
        date = created[:10] if created else "?"
        prompt_snip = (prompt or "")[:80].replace("\n", " ")
        resp_snip = (response or "")[:100].replace("\n", " ")
        lines.append(
            f"<b>{date}</b> — <code>{esc(prompt_snip)}</code>\n"
            f"  <i>{esc(resp_snip)}{'…' if len(response or '') > 100 else ''}</i>\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Git Workflow Commands ─────────────────────────────────────


async def cmd_commit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Git add + commit with AI-generated message."""
    uid = update.effective_user.id
    if not allowed(uid):
        return

    cwd = get_cwd(uid)

    # Get diff summary
    diff = await run_shell("git diff --stat && echo '---' && git diff --staged --stat", cwd)
    status = await run_shell("git status --short", cwd)

    if not status.strip() or status.strip() == "[exit 0]":
        await update.message.reply_text("✅ Nothing to commit — working tree clean.")
        return

    # Ask codex to generate commit message
    status_msg = await update.message.reply_text("📝 <i>Generating commit message...</i>", parse_mode=ParseMode.HTML)

    msg_override = " ".join(context.args) if context.args else ""
    if msg_override:
        commit_msg = msg_override
    else:
        # Use codex to generate message
        prompt = (
            f"Generate a concise git commit message (1-2 lines) for these changes. "
            f"Return ONLY the commit message, nothing else.\n\n"
            f"Status:\n{status}\n\nDiff:\n{diff[:2000]}"
        )
        commit_msg = await run_codex_simple(prompt, cwd, get_model(uid))
        commit_msg = commit_msg.strip().strip('"').strip("'").strip("`")

    # Stage and commit
    result = await run_shell(f'git add -A && git commit -m "{commit_msg}"', cwd)

    await safe_edit(
        status_msg,
        f"📝 <b>Committed:</b>\n<code>{esc(commit_msg)}</code>\n\n<pre>{esc(result[-500:])}</pre>",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Push", callback_data="git:push"),
                InlineKeyboardButton("✅ Create PR", callback_data="git:pr"),
            ]
        ]),
    )


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Git push."""
    uid = update.effective_user.id
    if not allowed(uid):
        return
    cwd = get_cwd(uid)
    result = await run_shell("git push", cwd, timeout=30)
    await update.message.reply_text(f"🚀 <pre>{esc(result)}</pre>", parse_mode=ParseMode.HTML)


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show git diff."""
    uid = update.effective_user.id
    if not allowed(uid):
        return
    cwd = get_cwd(uid)
    diff = await run_shell("git diff", cwd)
    if not diff.strip() or diff.strip() == "[exit 0]":
        diff = await run_shell("git diff --staged", cwd)
    if not diff.strip() or diff.strip() == "[exit 0]":
        await update.message.reply_text("✅ No changes.")
        return

    if len(diff) > MAX_TG - 100:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=BytesIO(diff.encode()),
            filename="changes.diff",
            caption="📋 Git diff",
        )
    else:
        await update.message.reply_text(f"📋 <pre>{esc(diff)}</pre>", parse_mode=ParseMode.HTML)


# ── Message Handler ───────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        await update.message.reply_text("🔒 Not authorized.")
        return

    prompt = update.message.text
    if not prompt:
        return

    # ── Check for reply keyboard triggers ──
    # "⚙️ Settings" → show status
    if prompt == "⚙️ Settings":
        return await cmd_status(update, context)
    # "🛠️ Skills" → open skill shop
    if prompt == "🛠️ Skills":
        return await cmd_skills(update, context)

    # ── Custom skill creation flow ──
    creating = context.user_data.get("creating_skill")
    if creating:
        step = creating.get("step")
        if step == "name":
            creating["name"] = prompt[:50]
            creating["step"] = "prompt"
            await update.message.reply_text(
                f"Got it: <b>{esc(prompt[:50])}</b>\n\n"
                "Step 2/3: What should this skill do?\n"
                "<i>Describe the prompt codex will receive.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        elif step == "prompt":
            creating["prompt"] = prompt
            creating["step"] = "icon"
            icons = ["🔧", "⚡", "🎯", "🔮", "📌", "🌟", "💎", "🏷️", "🧩", "🔬", "📐", "🎲"]
            rows = [
                [InlineKeyboardButton(ic, callback_data=f"skicon:{ic}") for ic in icons[i : i + 4]]
                for i in range(0, len(icons), 4)
            ]
            await update.message.reply_text(
                "Step 3/3: Pick an icon:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

    # Check if this matches a skill button (e.g., "🔍 Review")
    all_skills = get_all_skills(uid)
    skill_prompt = None
    for sid, skill in all_skills.items():
        trigger = f"{skill['icon']} {skill['name']}"
        if prompt == trigger:
            skill_prompt = skill["prompt"]
            break

    if skill_prompt:
        prompt = skill_prompt

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
        await _run_and_respond(
            caption, uid, update.effective_chat.id, status_msg, context,
            image_path=temp_path,
        )

    # Cleanup
    try:
        os.unlink(temp_path)
    except OSError:
        pass


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
                final_html, raw_text, _ = await asyncio.wait_for(
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

    # ── Skill detail view (tap to see info)
    if data.startswith("ski:"):
        skill_id = data[4:]
        await query.answer()
        await _show_skill_detail(uid, skill_id, msg, context)
        return

    # ── Skill toggle (from detail view)
    if data.startswith("sk:"):
        skill_id = data[3:]
        if skill_id in BUILT_IN_SKILLS or skill_id in db_get_custom_skills(uid):
            new_state = db_toggle_skill(uid, skill_id)
            skill = get_all_skills(uid).get(skill_id, {})
            status = "enabled ✅" if new_state else "disabled ➖"
            await query.answer(f"{skill.get('icon', '')} {skill.get('name', skill_id)} {status}")
            # Refresh the detail view to show new state
            await _show_skill_detail(uid, skill_id, msg, context)
        return

    # ── Back to skill shop from detail view
    if data == "skback":
        await query.answer()
        await _show_skill_shop(uid, msg, context, edit=True)
        return

    # ── Delete custom skill
    if data.startswith("skdel:"):
        skill_id = data[6:]
        customs = db_get_custom_skills(uid)
        if skill_id in customs:
            db_delete_custom_skill(uid, skill_id)
            await query.answer(f"Deleted {customs[skill_id]['name']}")
            await _show_skill_shop(uid, msg, context, edit=True)
        else:
            await query.answer("Skill not found.")
        return

    # ── Create custom skill (start flow)
    if data == "sknew":
        await query.answer()
        context.user_data["creating_skill"] = {"step": "name"}
        await context.bot.send_message(
            msg.chat_id,
            "✨ <b>Create a Custom Skill</b>\n\n"
            "Step 1/3: What should this skill be called?\n"
            "<i>Example: Lint Check, API Test, Build</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Skill shop done → show reply keyboard
    if data == "skdone":
        await query.answer("Quick actions updated!")
        kb = build_quick_keyboard(uid)
        await context.bot.send_message(
            msg.chat_id,
            "✅ Skills saved! Your quick actions are ready below.",
            reply_markup=kb,
        )
        return

    # ── Noop (section headers)
    if data == "noop":
        await query.answer()
        return

    # ── Setup wizard callbacks
    if data == "setup:repo":
        await query.answer()
        # Reuse repo command logic
        projects_dir = BASE_DIR / "Desktop" / "Projects"
        if not projects_dir.exists():
            projects_dir = BASE_DIR
        try:
            folders = sorted(
                d.name for d in projects_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
        except PermissionError:
            await query.answer("Can't read directory.")
            return
        folder_map = {}
        rows = []
        row = []
        for f in folders[:20]:
            h = hashlib.md5(f.encode()).hexdigest()[:8]
            folder_map[h] = str(projects_dir / f)
            row.append(InlineKeyboardButton(f"📁 {f}", callback_data=f"cd:{h}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        context.user_data["repo_folders"] = folder_map
        await context.bot.send_message(
            msg.chat_id,
            "📁 <b>Pick a project:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data == "setup:model":
        await query.answer()
        current = get_model(uid)
        rows = []
        row = []
        for mid, desc in MODELS:
            label = f"{'✅ ' if mid == current else ''}{mid}"
            row.append(InlineKeyboardButton(label, callback_data=f"sm:{mid}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await context.bot.send_message(
            msg.chat_id,
            "🤖 <b>Pick a model:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("sm:"):
        new_model = data[3:]
        db_set_user(uid, model=new_model)
        await query.answer(f"Model → {new_model}")
        await safe_edit(msg, f"🤖 Model set to <code>{esc(new_model)}</code>")
        return

    if data == "setup:skills":
        await query.answer()
        await _show_skill_shop(uid, msg, context)
        return

    if data == "setup:done":
        await query.answer("Setup complete!")
        kb = build_quick_keyboard(uid)
        await context.bot.send_message(
            msg.chat_id,
            "✅ <b>Setup complete!</b>\n\n"
            f"📁 {esc(str(get_cwd(uid)))}\n"
            f"🤖 {esc(get_model(uid) or 'default')}\n"
            f"🛠️ {len(db_get_enabled_skills(uid))} skills active\n\n"
            "Your quick actions are ready. Start coding!",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # ── Git workflow buttons
    if data == "git:push":
        await query.answer("🚀 Pushing...")
        cwd = get_cwd(uid)
        result = await run_shell("git push", cwd, timeout=30)
        await context.bot.send_message(
            msg.chat_id, f"🚀 <pre>{esc(result)}</pre>", parse_mode=ParseMode.HTML
        )
        return

    if data == "git:pr":
        await query.answer("✅ Creating PR...")
        cwd = get_cwd(uid)
        # Get branch name and generate PR
        branch = (await run_shell("git branch --show-current", cwd)).split("\n")[0].strip()
        result = await run_shell(
            f'gh pr create --fill --head "{branch}" 2>&1 || echo "PR may already exist"',
            cwd, timeout=30,
        )
        await context.bot.send_message(
            msg.chat_id, f"✅ <pre>{esc(result)}</pre>", parse_mode=ParseMode.HTML
        )
        return

    if data == "git:diff":
        await query.answer("📋 Fetching diff...")
        cwd = get_cwd(uid)
        diff = await run_shell("git diff", cwd)
        if len(diff) > MAX_TG - 100:
            await context.bot.send_document(
                chat_id=msg.chat_id,
                document=BytesIO(diff.encode()),
                filename="changes.diff",
                caption="📋 Git diff",
            )
        else:
            await context.bot.send_message(
                msg.chat_id, f"📋 <pre>{esc(diff)}</pre>", parse_mode=ParseMode.HTML
            )
        return

    # ── Custom skill icon selection
    if data.startswith("skicon:"):
        icon = data[7:]
        creating = context.user_data.get("creating_skill", {})
        if creating.get("step") == "icon":
            creating["icon"] = icon
            # Save the skill
            name = creating["name"]
            prompt = creating["prompt"]
            skill_id = f"custom_{hashlib.md5(name.encode()).hexdigest()[:8]}"
            db_save_custom_skill(uid, skill_id, name, icon, prompt)
            context.user_data.pop("creating_skill", None)
            await query.answer(f"{icon} {name} created!")
            kb = build_quick_keyboard(uid)
            await context.bot.send_message(
                msg.chat_id,
                f"✅ Skill <b>{icon} {esc(name)}</b> created and enabled!\n\n"
                "It's now in your quick actions below.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
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
        BotCommand("setup", "Setup wizard"),
        BotCommand("skills", "Manage skills"),
        BotCommand("repo", "Pick a project"),
        BotCommand("model", "Switch model"),
        BotCommand("run", "Run shell command directly"),
        BotCommand("pin", "Pin project context"),
        BotCommand("pins", "View pinned context"),
        BotCommand("commit", "Git commit with AI message"),
        BotCommand("diff", "Show git diff"),
        BotCommand("search", "Search history"),
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
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("repo", cmd_repo))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("kb", cmd_keyboard))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("pins", cmd_pins))
    app.add_handler(CommandHandler("unpin", cmd_unpin))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("commit", cmd_commit))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("diff", cmd_diff))

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
