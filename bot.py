"""NEWR Music bot entrypoint. Config comes only from environment variables.

Required: DISCORD_TOKEN, GUILD_ID (guild commands sync target).
Run:  bash ../ops/run-newr-music.sh
       (or: pip install -r requirements.txt && python3 bot.py)

Slash commands only, synced to one guild (NEWR LABS). Never global-synced.
No token, secret, or password is stored in code or logs.
"""
from __future__ import annotations

import asyncio
import os
import sys

import discord
from discord.ext import commands

try:
    from dotenv import load_dotenv  # optional, only if installed
    load_dotenv()
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Default to NEWR LABS like the other three new bots, so a
# token-only .env still syncs guild-only (never globally).
DEFAULT_GUILD_ID = 1465422166790242377  # NEWR LABS
try:
    GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else DEFAULT_GUILD_ID
except ValueError:
    GUILD_ID = DEFAULT_GUILD_ID

COGS = ["cogs.music"]


def _proxy_kwargs() -> dict:
    """VM egress goes through an HTTP proxy; direct TLS to Discord fails
    here (SSL WRONG_VERSION_NUMBER), while aiohttp with trust_env works.
    discord.py does not read proxy env vars itself, so pass them explicitly.
    Copied from the NEWR Support / NEWR AI bots on this VM.
    Credentials stay in the environment, never in code or logs."""
    from urllib.parse import unquote, urlparse

    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return {}
    parsed = urlparse(raw)
    proxy = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 3128}"
    kwargs: dict = {"proxy": proxy}
    if parsed.username:
        import aiohttp

        kwargs["proxy_auth"] = aiohttp.BasicAuth(
            unquote(parsed.username),
            unquote(parsed.password or ""))
    return kwargs


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True  # required: see who is in voice, join them
        intents.message_content = True  # not used by slash commands; kept for parity
        super().__init__(command_prefix=None, intents=intents, help_command=None,
                         **_proxy_kwargs())
        self._synced = False

    async def setup_hook(self) -> None:
        for cog in COGS:
            await self.load_extension(cog)

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (id={self.user.id})")
        if self._synced:
            return
        self._synced = True
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash commands globally")


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. See .env.example.")
    asyncio.run(MusicBot().start(token))


if __name__ == "__main__":
    main()
