#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Codex Telegram Bot — Interactive Setup
# Run: bash setup.sh
# ─────────────────────────────────────────────────────────────

set -e

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo -e "${BOLD}🚀 Codex Telegram Bot — Setup${NC}"
echo -e "${DIM}────────────────────────────────────────${NC}"
echo ""

# ── Step 1: Check prerequisites ──────────────────────────────

echo -e "${BOLD}Step 1/6: Checking prerequisites${NC}"
echo ""

# Python
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    echo -e "  ${GREEN}✓${NC} Python ${PY_VERSION}"
else
    echo -e "  ${RED}✗${NC} Python 3 not found. Install it: https://python.org"
    exit 1
fi

# Codex CLI
if command -v codex &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} Codex CLI found at $(which codex)"
else
    echo -e "  ${RED}✗${NC} Codex CLI not found."
    echo ""
    echo -e "  Install it with: ${CYAN}npm install -g @openai/codex${NC}"
    echo -e "  Then log in with: ${CYAN}codex login${NC}"
    echo ""
    read -p "  Press Enter after installing, or Ctrl+C to quit..."
    if ! command -v codex &>/dev/null; then
        echo -e "  ${RED}Still not found. Please install codex and try again.${NC}"
        exit 1
    fi
fi

# Node (for codex)
if command -v node &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} Node.js $(node --version)"
else
    echo -e "  ${YELLOW}!${NC} Node.js not found (needed for codex CLI)"
fi

echo ""

# ── Step 2: Create Telegram Bot ──────────────────────────────

echo -e "${BOLD}Step 2/6: Create your Telegram bot${NC}"
echo ""
echo -e "  You need a bot token from Telegram's BotFather."
echo ""

if [ -f .env ] && grep -q "TELEGRAM_BOT_TOKEN=" .env 2>/dev/null; then
    EXISTING_TOKEN=$(grep "TELEGRAM_BOT_TOKEN=" .env | cut -d= -f2)
    if [ "$EXISTING_TOKEN" != "your_bot_token_here" ] && [ -n "$EXISTING_TOKEN" ]; then
        echo -e "  ${GREEN}✓${NC} Bot token already configured"
        echo ""
        read -p "  Keep existing token? (Y/n): " KEEP_TOKEN
        if [ "$KEEP_TOKEN" != "n" ] && [ "$KEEP_TOKEN" != "N" ]; then
            BOT_TOKEN="$EXISTING_TOKEN"
        fi
    fi
fi

if [ -z "$BOT_TOKEN" ]; then
    echo -e "  ${CYAN}Here's exactly what to do:${NC}"
    echo ""
    echo -e "  1. Open Telegram on your phone"
    echo -e "  2. Search for ${BOLD}@BotFather${NC} (has a blue checkmark)"
    echo -e "  3. Tap ${BOLD}Start${NC}, then send: ${CYAN}/newbot${NC}"
    echo -e "  4. BotFather asks: ${DIM}\"Alright, a new bot. How are we going"
    echo -e "     to call it? Please choose a name for your bot.\"${NC}"
    echo -e "     → Type any name, e.g.: ${BOLD}My Codex Bot${NC}"
    echo -e "  5. BotFather asks: ${DIM}\"Good. Now let's choose a username"
    echo -e "     for your bot. It must end in 'bot'.\"${NC}"
    echo -e "     → Type a unique username, e.g.: ${BOLD}myname_codex_bot${NC}"
    echo -e "  6. BotFather replies with a message containing:"
    echo -e "     ${DIM}\"Use this token to access the HTTP API:\"${NC}"
    echo -e "     ${BOLD}1234567890:ABCdefGHIjklMNOpqrsTUVwxyz${NC}"
    echo -e "     → Copy that entire token"
    echo ""
    echo -e "  ${YELLOW}Tip: You can also configure your bot's profile picture"
    echo -e "  and description in BotFather with /setuserpic and /setdescription${NC}"
    echo ""
    read -p "  Paste your bot token here: " BOT_TOKEN
    echo ""

    if [ -z "$BOT_TOKEN" ]; then
        echo -e "  ${RED}No token provided. Exiting.${NC}"
        exit 1
    fi

    # Basic validation
    if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:.+$ ]]; then
        echo -e "  ${YELLOW}⚠ That doesn't look like a bot token (should be like 123456:ABC...).${NC}"
        read -p "  Continue anyway? (y/N): " CONTINUE
        if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
            exit 1
        fi
    else
        echo -e "  ${GREEN}✓${NC} Token looks valid"
    fi
fi

echo ""

# ── Step 3: Get Telegram User ID ─────────────────────────────

echo -e "${BOLD}Step 3/6: Get your Telegram user ID${NC}"
echo ""
echo -e "  Your user ID locks the bot so only you can use it."
echo ""

if [ -f .env ] && grep -q "ALLOWED_USERS=" .env 2>/dev/null; then
    EXISTING_UID=$(grep "ALLOWED_USERS=" .env | cut -d= -f2)
    if [ "$EXISTING_UID" != "your_telegram_user_id" ] && [ -n "$EXISTING_UID" ]; then
        echo -e "  ${GREEN}✓${NC} User ID already configured: ${EXISTING_UID}"
        echo ""
        read -p "  Keep existing ID? (Y/n): " KEEP_UID
        if [ "$KEEP_UID" != "n" ] && [ "$KEEP_UID" != "N" ]; then
            USER_ID="$EXISTING_UID"
        fi
    fi
fi

if [ -z "$USER_ID" ]; then
    echo -e "  ${CYAN}Here's how to find your user ID:${NC}"
    echo ""
    echo -e "  1. Open Telegram on your phone"
    echo -e "  2. Search for ${BOLD}@userinfobot${NC}"
    echo -e "  3. Tap ${BOLD}Start${NC}"
    echo -e "  4. It instantly replies with your info:"
    echo -e "     ${DIM}Id: ${BOLD}1234567890${NC}"
    echo -e "     ${DIM}First: Ben${NC}"
    echo -e "     ${DIM}Lang: en${NC}"
    echo -e "     → Copy the ${BOLD}Id${NC} number"
    echo ""
    echo -e "  ${YELLOW}Tip: You can add multiple user IDs separated by commas"
    echo -e "  if you want to share the bot with others.${NC}"
    echo ""
    read -p "  Your Telegram user ID: " USER_ID
    echo ""

    if [ -z "$USER_ID" ]; then
        echo -e "  ${RED}No user ID provided. Exiting.${NC}"
        exit 1
    fi

    if [[ ! "$USER_ID" =~ ^[0-9,\ ]+$ ]]; then
        echo -e "  ${YELLOW}⚠ User ID should be a number (or comma-separated numbers).${NC}"
        read -p "  Continue anyway? (y/N): " CONTINUE
        if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
            exit 1
        fi
    else
        echo -e "  ${GREEN}✓${NC} User ID: ${USER_ID}"
    fi
fi

echo ""

# ── Step 4: Configure ────────────────────────────────────────

echo -e "${BOLD}Step 4/6: Configuration${NC}"
echo ""

# Base directory
DEFAULT_BASE="$HOME"
read -p "  Base directory [$DEFAULT_BASE]: " BASE_DIR
BASE_DIR="${BASE_DIR:-$DEFAULT_BASE}"
echo -e "  ${GREEN}✓${NC} Base dir: ${BASE_DIR}"
echo ""

# Write .env
cat > .env << ENVFILE
# Telegram Bot
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
ALLOWED_USERS=${USER_ID}

# Filesystem
BASE_DIR=${BASE_DIR}

# Codex (leave CODEX_MODEL blank for Codex default)
# CODEX_MODEL=gpt-5.4
CODEX_TIMEOUT=3600

# Optional: voice transcription (pip install openai)
# OPENAI_API_KEY=sk-...

# Optional: Mini App (pip install aiohttp)
# WEBAPP_URL=https://your-domain.ngrok.io
# WEBAPP_PORT=8443
ENVFILE

echo -e "  ${GREEN}✓${NC} .env written"
echo ""

# ── Step 5: Install dependencies ─────────────────────────────

echo -e "${BOLD}Step 5/6: Installing dependencies${NC}"
echo ""

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo -e "  ${GREEN}✓${NC} Virtual environment created"
fi

source .venv/bin/activate
pip install -q -r requirements.txt
echo -e "  ${GREEN}✓${NC} Dependencies installed"
echo ""

# ── Step 6: Test & Launch ────────────────────────────────────

echo -e "${BOLD}Step 6/6: Ready to launch!${NC}"
echo ""

# Check codex auth
echo -e "  Checking Codex authentication..."
if codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "say ok" 2>/dev/null | grep -q "agent_message"; then
    echo -e "  ${GREEN}✓${NC} Codex authenticated and working"
else
    echo -e "  ${YELLOW}!${NC} Codex may not be authenticated."
    echo -e "  Run ${CYAN}codex login${NC} to authenticate with your ChatGPT account."
    echo ""
    read -p "  Press Enter to continue anyway, or Ctrl+C to fix first..."
fi

echo ""
echo -e "${DIM}────────────────────────────────────────${NC}"
echo ""
echo -e "${GREEN}${BOLD}✅ Setup complete!${NC}"
echo ""
echo -e "  Start the bot:"
echo -e "  ${CYAN}source .venv/bin/activate && python bot.py${NC}"
echo ""
echo -e "  Then open Telegram, find your bot, and send ${BOLD}/start${NC}"
echo ""
echo -e "  ${DIM}Auto-start on boot:${NC}"
echo -e "  ${DIM}See README.md for macOS LaunchAgent setup.${NC}"
echo ""
