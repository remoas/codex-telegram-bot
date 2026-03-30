#!/usr/bin/env python3
# Optional Mini App server for Codex Telegram Bot
#
# Setup:
#   pip install aiohttp
#   python server.py
#
# For HTTPS (required by Telegram Mini Apps):
#   ngrok http 8443
#   Set WEBAPP_URL in .env to your ngrok URL
#   Configure via @BotFather → Bot Settings → Menu Button
#
# Requires: aiohttp>=3.9

"""
aiohttp server providing the backend for the Telegram Mini App.

Serves the static frontend from ./app/, validates Telegram initData,
streams codex exec output via SSE, and lists available project directories.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import urllib.parse
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

# ── Configuration ────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USERS", "").split(",")
    if uid.strip()
}
BASE_DIR = Path(os.environ.get("BASE_DIR", str(Path.home())))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "danger-full-access")
PORT = int(os.environ.get("WEBAPP_PORT", "8443"))
APP_DIR = Path(__file__).parent / "app"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("codex-server")


# ── Telegram initData validation ─────────────────────────────

def validate_init_data(init_data: str, bot_token: str) -> bool:
    """Validate Telegram Mini App initData using HMAC-SHA256."""
    if not init_data:
        return False
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return False
        data_check = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret = hmac.new(
            b"WebAppData", bot_token.encode(), hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret, data_check.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, received_hash)
    except Exception:
        return False


def extract_user_id(init_data: str) -> int | None:
    """Extract user.id from Telegram initData."""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        user_json = parsed.get("user", "")
        if user_json:
            user = json.loads(user_json)
            return int(user.get("id", 0)) or None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


# ── CORS middleware ──────────────────────────────────────────

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers to all responses for Telegram webview compatibility."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ── SSE helpers ──────────────────────────────────────────────

def sse_event(event: str, data: dict | str) -> bytes:
    """Format a server-sent event."""
    if isinstance(data, dict):
        data = json.dumps(data)
    return f"event: {event}\ndata: {data}\n\n".encode()


def strip_shell_wrapper(cmd: str) -> str:
    """Strip shell wrapper like '/bin/zsh -lc \"actual command\"'."""
    if " -lc " in cmd:
        cmd = cmd.split(" -lc ", 1)[1].strip("'\"")
    return cmd


# ── Route handlers ───────────────────────────────────────────

async def handle_root(request: web.Request) -> web.Response:
    """Redirect / to the Mini App."""
    raise web.HTTPFound("/app/index.html")


async def handle_status(request: web.Request) -> web.Response:
    """Return current server settings."""
    return web.json_response({
        "model": CODEX_MODEL or "default",
        "sandbox": CODEX_SANDBOX,
        "base_dir": str(BASE_DIR),
        "port": PORT,
    })


async def handle_projects(request: web.Request) -> web.Response:
    """List available project directories."""
    projects_dir = BASE_DIR / "Desktop" / "Projects"
    if not projects_dir.exists():
        projects_dir = BASE_DIR

    try:
        folders = sorted(
            [
                {"name": d.name, "path": str(d)}
                for d in projects_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            ],
            key=lambda x: x["name"].lower(),
        )
    except PermissionError:
        folders = []

    return web.json_response(folders)


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """SSE streaming endpoint for codex exec."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response(
            {"error": "Invalid JSON body"}, status=400
        )

    prompt = body.get("prompt", "").strip()
    cwd = body.get("cwd", str(BASE_DIR))
    model = body.get("model", CODEX_MODEL)
    init_data = body.get("initData", "")

    # Validate initData
    if not validate_init_data(init_data, BOT_TOKEN):
        return web.json_response(
            {"error": "Invalid or missing initData"}, status=403
        )

    # Check allowed users
    user_id = extract_user_id(init_data)
    if ALLOWED_USERS and (user_id is None or user_id not in ALLOWED_USERS):
        return web.json_response(
            {"error": "User not authorized"}, status=403
        )

    if not prompt:
        return web.json_response(
            {"error": "Prompt is required"}, status=400
        )

    # Validate cwd
    cwd_path = Path(cwd)
    if not cwd_path.exists() or not cwd_path.is_dir():
        cwd_path = BASE_DIR

    # Set up SSE response
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    # Build codex command
    cmd = [
        "codex", "exec",
        "--json",
        "--sandbox", CODEX_SANDBOX,
        "-C", str(cwd_path),
        "--skip-git-repo-check",
        "--full-auto",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    log.info(
        "Stream request from user %s: %s",
        user_id,
        prompt[:120],
    )

    # Emit thinking event
    await response.write(sse_event("thinking", {}))

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )

        usage = {}

        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")
            item = event.get("item", {})
            item_type = item.get("type", "") if isinstance(item, dict) else ""

            if event_type == "item.started":
                if item_type == "command_execution":
                    cmd_str = strip_shell_wrapper(item.get("command", ""))
                    if cmd_str:
                        await response.write(
                            sse_event("action", {"command": cmd_str})
                        )

            elif event_type == "item.completed":
                if item_type == "agent_message":
                    agent_text = item.get("text", "")
                    if agent_text:
                        await response.write(
                            sse_event("text", {"text": agent_text})
                        )

                elif item_type == "command_execution":
                    cmd_str = strip_shell_wrapper(item.get("command", ""))
                    exit_code = item.get("exit_code", None)
                    data = {"command": cmd_str}
                    if exit_code is not None:
                        data["exit_code"] = exit_code
                    await response.write(sse_event("action", data))

            elif event_type == "error":
                msg = event.get("message", "Unknown error")
                try:
                    parsed = json.loads(msg)
                    msg = parsed.get("error", {}).get("message", msg)
                except (json.JSONDecodeError, AttributeError):
                    pass
                await response.write(
                    sse_event("error", {"message": msg})
                )

            elif event_type == "turn.failed":
                err = event.get("error", {})
                msg = (
                    err.get("message", "Turn failed")
                    if isinstance(err, dict)
                    else str(err)
                )
                try:
                    parsed = json.loads(msg)
                    msg = parsed.get("error", {}).get("message", msg)
                except (json.JSONDecodeError, AttributeError):
                    pass
                await response.write(
                    sse_event("error", {"message": msg})
                )

            elif event_type == "turn.completed":
                turn_usage = event.get("usage", {})
                if turn_usage:
                    usage = turn_usage

        await proc.wait()

        # Check stderr on failure
        if proc.returncode and proc.returncode != 0:
            stderr_bytes = await proc.stderr.read()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if stderr_text:
                await response.write(
                    sse_event("error", {"message": stderr_text[:2000]})
                )

        await response.write(sse_event("done", {"usage": usage}))

    except asyncio.CancelledError:
        log.info("Stream cancelled by client (user %s)", user_id)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        raise

    except FileNotFoundError:
        await response.write(
            sse_event("error", {
                "message": "codex CLI not found. Is it installed and on PATH?"
            })
        )
        await response.write(sse_event("done", {"usage": {}}))

    except Exception as e:
        log.exception("Error in stream handler")
        try:
            await response.write(
                sse_event("error", {"message": str(e)})
            )
            await response.write(sse_event("done", {"usage": {}}))
        except Exception:
            pass

    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    return response


# ── Application setup ────────────────────────────────────────

def create_app() -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get("/", handle_root)
    app.router.add_post("/api/stream", handle_stream)
    app.router.add_get("/api/projects", handle_projects)
    app.router.add_get("/api/status", handle_status)

    # Serve static files from ./app/
    if APP_DIR.exists():
        app.router.add_static("/app/", path=str(APP_DIR), name="app_static")
    else:
        log.warning("Static app directory not found: %s", APP_DIR)

    return app


async def on_shutdown(app: web.Application):
    """Clean up on shutdown."""
    log.info("Server shutting down...")


def main():
    log.info("Starting Codex Mini App server")
    log.info("Port: %d", PORT)
    log.info("Base dir: %s", BASE_DIR)
    log.info("Model: %s", CODEX_MODEL or "default")
    log.info("Static dir: %s (exists: %s)", APP_DIR, APP_DIR.exists())
    if ALLOWED_USERS:
        log.info("Allowed users: %s", ALLOWED_USERS)
    else:
        log.info("Allowed users: all (no restriction)")

    app = create_app()
    app.on_shutdown.append(on_shutdown)

    # Graceful shutdown on SIGTERM
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal():
        log.info("Received shutdown signal")
        raise web.GracefulExit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT,
        loop=loop,
        print=lambda msg: log.info(msg),
    )


if __name__ == "__main__":
    main()
