"""IRC bridge — connects to ergo as lazer-bridge, relays bot commands from lazer players."""
from __future__ import annotations

import asyncio
import logging
import os

import irctokens

log = logging.getLogger("g0v0.irc_bridge")

_HOST = os.environ.get("IRC_HOST", "ergo")
_PORT = int(os.environ.get("IRC_PORT", "6667"))
_NICK = "lazer-bridge"
_BOT = "BanchoBot"

_pending: dict[int, asyncio.Future[str]] = {}
_writer: asyncio.StreamWriter | None = None
_ready = asyncio.Event()


async def _connect() -> None:
    global _writer
    while True:
        try:
            reader, writer = await asyncio.open_connection(_HOST, _PORT)
            _writer = writer
            writer.write(f"NICK {_NICK}\r\nUSER {_NICK} 0 * :lazer bridge\r\n".encode())
            await writer.drain()
            buf = b""
            async for chunk in reader:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.rstrip(b"\r").decode(errors="replace")
                    if not line:
                        continue
                    tok = irctokens.tokenise(line)
                    if tok.command == "PING":
                        writer.write(f"PONG :{tok.params[0]}\r\n".encode())
                        await writer.drain()
                    elif tok.command == "001":
                        log.info("IRC bridge connected as %s", _NICK)
                        _ready.set()
                    elif tok.command == "PRIVMSG" and tok.params[1].startswith("REPLY "):
                        parts = tok.params[1].split(" ", 2)
                        if len(parts) == 3:
                            uid = int(parts[1])
                            fut = _pending.pop(uid, None)
                            if fut and not fut.done():
                                fut.set_result(parts[2])
        except Exception:
            log.warning("IRC bridge disconnected, reconnecting in 5s…")
            _ready.clear()
            _writer = None
        await asyncio.sleep(5)


async def start() -> None:
    asyncio.create_task(_connect())


async def ask_bot(user_id: int, username: str, message: str, timeout: float = 2.0) -> str | None:
    if not _ready.is_set() or _writer is None:
        return None
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[str] = loop.create_future()
    _pending[user_id] = fut
    try:
        _writer.write(f"PRIVMSG {_BOT} :MSG {user_id} {username} {message}\r\n".encode())
        await _writer.drain()
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        _pending.pop(user_id, None)
        return None
