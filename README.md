# Codex Telegram Bot

Control OpenAI Codex CLI from your phone via Telegram. Streaming responses, conversation memory, voice input, photo analysis, 12 built-in skills, custom skill creation, and a full Mini App terminal.

## Setup (5 minutes)

### One-command install

```bash
git clone https://github.com/remoas/codex-telegram-bot.git
cd codex-telegram-bot
bash setup.sh
```

The setup script walks you through everything interactively. Or follow the manual steps below.

### Manual setup

#### Prerequisites
- macOS or Linux
- Python 3.11+
- [OpenAI Codex CLI](https://github.com/openai/codex) installed and authenticated
- A ChatGPT Plus/Pro account

#### 1. Create a Telegram bot

Open Telegram and search for **@BotFather** (blue checkmark). Send `/newbot`.

BotFather will ask you two things:
1. **A display name** for your bot — can be anything, e.g. `My Codex Bot`
2. **A username** — must end in `bot`, e.g. `myname_codex_bot`

BotFather replies with a message like:
```
Done! Congratulations on your new bot.
Use this token to access the HTTP API:
7918273645:AAH8kL2mNpQrStUvWxYz1234567890abc
```

Copy that **entire token** (the number, colon, and letters).

**Optional but recommended:** While in BotFather, also run:
- `/setuserpic` — give your bot a profile picture
- `/setdescription` — set what users see before they start the bot
- `/setinline` — enable inline mode (type `@yourbot query` in any chat)

#### 2. Get your Telegram user ID

Search for **@userinfobot** on Telegram and tap Start. It instantly replies:
```
Id: 1234567890
First: Ben
Lang: en
```

Copy the **Id** number. This locks the bot so only you can use it.

Want to share with others? Add multiple IDs comma-separated: `123,456,789`

#### 3. Clone and configure

```bash
git clone https://github.com/remoas/codex-telegram-bot.git
cd codex-telegram-bot
cp .env.example .env
```

Edit `.env`:
```bash
TELEGRAM_BOT_TOKEN=7918273645:AAH8kL2mNpQrStUvWxYz1234567890abc
ALLOWED_USERS=1234567890
BASE_DIR=/Users/yourname
```

#### 4. Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

#### 5. Authenticate Codex (first time only)

```bash
codex login
```

This opens a browser to sign in with your ChatGPT account.

That's it. Open Telegram, find your bot, send `/start`.

## Features

### Core
- **Streaming responses** — see codex thinking in real-time (updates every 2s)
- **Conversation memory** — codex remembers previous messages per project via session resume
- **HTML formatting** — syntax-highlighted code blocks, expandable tool call details
- **Copy buttons** — one-tap clipboard copy on code blocks
- **Long output as files** — responses over 4096 chars auto-send as `.md` files
- **Cancel button** — stop codex mid-execution
- **Per-user concurrency lock** — prevents double-processing

### Input types
- **Text** — regular messages
- **Voice** — send voice notes, transcribed via OpenAI Whisper (needs `OPENAI_API_KEY`)
- **Photos** — send screenshots, codex analyzes via `-i` flag
- **Documents** — send files for codex to review

### Skills
12 built-in one-tap skills (tap to see full description and toggle):

| Skill | What it does |
|-------|-------------|
| 🔍 Review | Code review for bugs, security, readability |
| 🧪 Tests | Run test suite, analyze failures |
| 📊 Git Status | Branch, commits, uncommitted changes |
| 🚀 Deploy | Deployment walkthrough |
| 📝 Docs | Generate documentation |
| 🔐 Security | Security vulnerability audit |
| 🎨 Refactor | Suggest code improvements |
| 💬 Explain | High-level codebase explanation |
| 🐛 Debug | Debug recent errors |
| 📦 Dependencies | Check outdated/vulnerable deps |
| ✅ PR Prep | Draft pull request |
| 🏗️ Scaffold | Create new components |

**Custom skills:** Create your own 3-step from Telegram (name → prompt → icon).

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize bot, show quick actions keyboard |
| `/new` | Clear conversation memory for current project |
| `/setup` | Guided setup wizard (project, model, skills) |
| `/skills` | Skill shop — browse, toggle, create skills |
| `/repo` | Pick a project from button grid |
| `/cd <path>` | Change working directory |
| `/model` | Switch AI model with button picker |
| `/status` | Current settings |
| `/kb` | Toggle quick actions keyboard |

### Inline mode

Type `@yourbotname query` in **any** Telegram chat to get a codex response inlined.

### Mini App (optional)

A full terminal-like web UI inside Telegram with real-time SSE streaming and Prism.js syntax highlighting.

```bash
pip install aiohttp
python server.py
# Expose via ngrok: ngrok http 8443
# Set WEBAPP_URL in .env to your ngrok URL
# Configure in BotFather: Bot Settings → Menu Button
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | From @BotFather |
| `ALLOWED_USERS` | Yes | — | Comma-separated Telegram user IDs |
| `BASE_DIR` | No | `$HOME` | Root directory for projects |
| `CODEX_MODEL` | No | (codex default) | AI model to use |
| `CODEX_TIMEOUT` | No | `3600` | Max seconds per request |
| `OPENAI_API_KEY` | No | — | Enables voice transcription |
| `WEBAPP_URL` | No | — | HTTPS URL for Mini App |
| `WEBAPP_PORT` | No | `8443` | Mini App server port |

## Auto-start on macOS

Create `~/Library/LaunchAgents/com.codex.telegram-bot.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.codex.telegram-bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/codex-telegram-bot/.venv/bin/python</string>
        <string>/path/to/codex-telegram-bot/bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/codex-telegram-bot</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>~/.codex-telegram/bot.log</string>
    <key>StandardErrorPath</key>
    <string>~/.codex-telegram/bot-error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/yourname</string>
    </dict>
</dict>
</plist>
```

Replace `/path/to/` and `/Users/yourname` with your actual paths, then:

```bash
mkdir -p ~/.codex-telegram
launchctl load ~/Library/LaunchAgents/com.codex.telegram-bot.plist
```

The bot auto-starts on boot and restarts if it crashes.

## Security

- Locked to your Telegram user ID(s) — nobody else can use it
- Codex runs with full system access (`--dangerously-bypass-approvals-and-sandbox`)
- No data sent anywhere except Telegram API and OpenAI
- Bot token and API keys stored locally in `.env` (gitignored)
- SQLite database stored in `data/` (gitignored)

## License

MIT
