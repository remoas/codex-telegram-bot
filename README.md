# Codex Telegram Bot

Control OpenAI Codex CLI from your phone via Telegram. Simple, lightweight, fully customizable.

## Quick Setup (5 minutes)

### Prerequisites
- macOS (or Linux)
- [OpenAI Codex CLI](https://github.com/openai/codex) installed (`npm install -g @openai/codex`)
- Python 3.11+
- A ChatGPT Plus/Pro account (for Codex auth)

### 1. Clone this repo
```bash
git clone https://github.com/remoas/codex-telegram-bot.git
cd codex-telegram-bot
```

### 2. Create a Telegram bot
- Open Telegram and message [@BotFather](https://t.me/BotFather)
- Send `/newbot`, follow the prompts
- Copy the bot token it gives you

### 3. Get your Telegram user ID
- Message [@userinfobot](https://t.me/userinfobot) on Telegram
- It will reply with your numeric user ID

### 4. Configure
```bash
cp .env.example .env
```
Edit `.env` and fill in:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
BASE_DIR=/Users/yourname
CODEX_SANDBOX=danger-full-access
CODEX_TIMEOUT=3600
```

### 5. Install & run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### 6. Log into Codex (first time only)
```bash
codex login
```
This opens a browser to authenticate with your ChatGPT account.

That's it. Message your bot on Telegram and start coding.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize bot |
| `/new` | Fresh conversation (no context) |
| `/repo` | List project folders |
| `/cd <path>` | Change working directory inside `BASE_DIR` |
| `/model` | List all available models |
| `/model <name>` | Switch model |
| `/status` | Current settings |

Messages in the same directory now keep short conversation history so follow-up prompts work naturally. Use `/new` or `/cd` to reset context.

## Available Models

| Model | Description |
|-------|-------------|
| `gpt-5.4` | Flagship. Best intelligence, 1M context |
| `gpt-5.4-mini` | Fast & cheap, 400K context |
| `gpt-5.3-codex` | Specialized coding model |
| `gpt-5.3-codex-spark` | Near-instant real-time coding (Pro only) |
| `o3` | Reasoning model |
| `o4-mini` | Fast reasoning model |

Switch models on the fly with `/model gpt-5.4-mini`.

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
    <key>StandardErrorPath</key>
    <string>/tmp/codex-telegram-bot.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/yourname</string>
    </dict>
</dict>
</plist>
```

Then:
```bash
launchctl load ~/Library/LaunchAgents/com.codex.telegram-bot.plist
```

The bot will auto-start on boot and restart if it crashes.

## Security

- Locked to your Telegram user ID only (`ALLOWED_USERS`)
- Working directory changes are restricted to `BASE_DIR`
- Codex runs with `--full-auto` and configurable sandbox mode
- No data sent anywhere except Telegram API and OpenAI
- Bot token stored locally in `.env` (gitignored)

## License

MIT
