# Codex Telegram Bot

Control OpenAI Codex CLI from your phone via Telegram.

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy `.env.example` to `.env` and add your bot token
3. Install dependencies: `pip install -r requirements.txt`
4. Run: `python bot.py`

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize bot |
| `/new` | Fresh conversation (no context) |
| `/repo` | List project folders |
| `/cd <path>` | Change working directory |
| `/model <name>` | Switch model (o3, o4-mini, etc.) |
| `/status` | Current settings |

Or just send any message to start coding.

## Auto-start on macOS

```bash
# Copy the launch agent
cp com.codex.telegram-bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.codex.telegram-bot.plist
```

## Security

- Locked to your Telegram user ID only
- Full filesystem access (configurable via `BASE_DIR`)
- Codex runs with `--full-auto` and configurable sandbox mode
