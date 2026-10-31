"""NEWR Music player cog — ALL user-facing actions are slash commands.

There are NO prefix commands in this bot (no ``!play`` or similar) and
no command that runs from a plain chat message. Every user-triggered
feature below is a ``discord.app_commands`` slash command, registered
via the bot's command tree and guild-synced by bot.py on ready.
No ``on_message`` handler exists in this cog or in bot.py, so nothing
here can be triggered by typing in chat.

Playback: discord.py VoiceClient + yt-dlp, streaming only
(``download=False``). Nothing is downloaded to disk and no temp audio
file is created, so there is nothing to clean up afterwards. If
yt-dlp extraction or voice playback fails, the command replies with a
clear error message instead of crashing the bot.
"""
from __future__ import annotations

import asyncio
import os
import random
import shutil
from collections import deque
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


@dataclass
class Track:
    title: str
    url: str  # public page URL shown to the user
    stream_url: str  # direct audio stream URL, never downloaded
    duration: int  # seconds, 0 if unknown
    requester: str


def _format_duration(seconds: int) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _yt_dlp_opts() -> dict:
    """Stream-only yt-dlp options. Same VM proxy rule as the Discord
    connection: direct egress fails here, so yt-dlp gets the proxy
    from the environment too (value never logged)."""
    opts: dict = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch",
        "extract_flat": False,
        "socket_timeout": 30,
    }
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        opts["proxy"] = proxy
    return opts


def _extract_track(query: str, requester: str) -> Track:
    """Blocking yt-dlp call — always run via run_in_executor by callers.
    Raises ValueError with a user-readable message on failure."""
    import yt_dlp

    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL(_yt_dlp_opts()) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:  # yt-dlp raises many error types; never crash
        raise ValueError(f"Could not find or load that audio: {exc}") from exc
    if not info:
        raise ValueError("Could not find or load that audio.")
    entries = info.get("entries")
    if entries:
        info = entries[0]
    stream_url = info.get("url") or ""
    if not stream_url:
        # Pick the best audio-only format that has a direct URL.
        for fmt in info.get("formats") or []:
            if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
                stream_url = fmt["url"]
                break
    if not stream_url:
        raise ValueError("Found the video, but no playable audio stream was available.")
    return Track(
        title=info.get("title") or "Unknown title",
        url=info.get("webpage_url") or query,
        stream_url=stream_url,
        duration=int(info.get("duration") or 0),
        requester=requester,
    )


class GuildPlayer:
    """Per-guild playback state: voice client, queue, loop mode, volume."""

    LOOP_MODES = ("off", "track", "queue")

    def __init__(self, bot: commands.Bot, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop = "off"
        self.volume = 50  # percent
        self._lock = asyncio.Lock()

    @property
    def voice(self) -> discord.VoiceClient | None:
        guild = self.bot.get_guild(self.guild_id)
        return guild.voice_client if guild else None  # type: ignore[return-value]

    def _source_for(self, track: Track) -> discord.PCMVolumeTransformer:
        audio = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=FFMPEG,
            before_options=FFMPEG_BEFORE,
            options=FFMPEG_OPTIONS,
        )
        source = discord.PCMVolumeTransformer(audio, volume=self.volume / 100)
        return source

    async def _play_current(self) -> None:
        """Start playing self.current on the voice client, if connected."""
        voice = self.voice
        if voice is None or self.current is None:
            return
        if voice.is_playing():
            voice.stop()
        source = self._source_for(self.current)
        loop = self.bot.loop

        def after(_error: Exception | None) -> None:
            # Voice callbacks run outside the event loop; schedule safely.
            if _error is not None:
                print(f"[music] playback error guild {self.guild_id}: {_error}")
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._on_track_end()))

        try:
            voice.play(source, after=after)
        except Exception as exc:
            print(f"[music] voice.play failed guild {self.guild_id}: {exc}")
            self.current = None

    async def _on_track_end(self) -> None:
        async with self._lock:
            finished = self.current
            # finished is None only when /skip cleared current before
            # stopping: in that case just advance to the next queued
            # track instead of returning early (or the queue stalls).
            if finished is not None and self.loop == "track":
                pass  # replay the same track
            elif finished is not None and self.loop == "queue":
                self.queue.append(finished)
                self.current = self.queue.popleft() if self.queue else None
            else:
                self.current = self.queue.popleft() if self.queue else None
            if self.current is not None:
                await self._play_current()

    async def enqueue(self, track: Track) -> bool:
        """Add a track; return True if it started playing immediately."""
        async with self._lock:
            if self.current is None and (self.voice is None or not self.voice.is_playing()):
                self.current = track
                await self._play_current()
                return True
            self.queue.append(track)
            return False

    async def stop_all(self) -> None:
        async with self._lock:
            self.queue.clear()
            self.current = None
            self.loop = "off"
            voice = self.voice
            if voice is not None and (voice.is_playing() or voice.is_paused()):
                voice.stop()


class Music(commands.Cog):
    """Slash commands for music playback in NEWR LABS."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    def _player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot, guild_id)
        return self.players[guild_id]

    async def _guild_only(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Music commands only work inside a server, not in DMs.",
                ephemeral=True)
            return False
        return True

    async def _ensure_voice(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """Return the player with a voice connection, auto-joining the
        caller's voice channel if needed. Returns None (after replying)
        if the user is not in a voice channel and the bot is not either."""
        guild = interaction.guild
        assert guild is not None
        player = self._player(guild.id)
        voice = player.voice
        member = guild.get_member(interaction.user.id)
        user_channel = member.voice.channel if member and member.voice else None
        try:
            if voice is None:
                if user_channel is None:
                    await interaction.followup.send(
                        "I am not in a voice channel. Join a voice channel "
                        "first, then use /play again (or use /join while "
                        "you are in a voice channel).")
                    return None
                await user_channel.connect()
            elif user_channel is not None and voice.channel != user_channel:
                await voice.move_to(user_channel)
        except discord.ClientException as exc:
            await interaction.followup.send(f"Could not join voice: {exc}")
            return None
        except Exception as exc:
            await interaction.followup.send(f"Could not join voice: {exc}")
            return None
        return player

    # -- voice connection -------------------------------------------------

    @app_commands.command(name="join", description="Join your current voice channel.")
    async def join(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        player = await self._ensure_voice(interaction)
        if player is None:
            return
        channel = player.voice.channel if player.voice else None  # type: ignore[union-attr]
        await interaction.followup.send(f"Joined {channel.mention if channel else 'voice'}.")

    @app_commands.command(name="leave", description="Leave the voice channel and clear the queue.")
    async def leave(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        await player.stop_all()
        voice = player.voice
        try:
            if voice is not None:
                await voice.disconnect()
        except Exception as exc:
            await interaction.followup.send(f"Left, but disconnect reported an error: {exc}")
            return
        await interaction.followup.send("Left the voice channel and cleared the queue.")

    # -- playback ---------------------------------------------------------

    @app_commands.command(name="play", description="Play a song from a search term or URL.")
    @app_commands.describe(query="Song name, search term, or audio/video URL")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        try:
            player = await self._ensure_voice(interaction)
            if player is None:
                return
            loop = self.bot.loop
            track = await loop.run_in_executor(
                None, _extract_track, query, interaction.user.display_name)
            started = await player.enqueue(track)
            if started:
                await interaction.followup.send(
                    f"Now playing: **{track.title}** "
                    f"({_format_duration(track.duration)}) — requested by {track.requester}")
            else:
                await interaction.followup.send(
                    f"Queued #{len(player.queue)}: **{track.title}** "
                    f"({_format_duration(track.duration)}) — requested by {track.requester}")
        except ValueError as exc:
            await interaction.followup.send(f"Sorry, I could not play that: {exc}")
        except Exception as exc:
            print(f"[music] /play failed: {exc}")
            await interaction.followup.send(
                "Sorry, something went wrong while trying to play that. Please try again.")

    @app_commands.command(name="pause", description="Pause the current track.")
    async def pause(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        voice = player.voice
        if voice is None or not voice.is_playing():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        voice.pause()
        await interaction.response.send_message("Paused.")

    @app_commands.command(name="resume", description="Resume the paused track.")
    async def resume(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        voice = player.voice
        if voice is None or not voice.is_paused():
            await interaction.response.send_message("Nothing is paused right now.", ephemeral=True)
            return
        voice.resume()
        await interaction.response.send_message("Resumed.")

    @app_commands.command(name="skip", description="Skip the current track.")
    async def skip(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        # Skip by stopping: the voice after-callback runs _on_track_end,
        # which advances to the next queued track (loop=queue re-queues
        # the skipped track first, handled below).
        voice = player.voice
        if voice is None or player.current is None:
            await interaction.followup.send("Nothing is playing to skip.")
            return
        if player.loop == "queue" and player.current is not None:
            player.queue.append(player.current)
        player.current = None
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        else:
            await player._on_track_end()
        # _on_track_end (via the stop callback) sets the next track; if
        # it already ran, report it, otherwise report the queued next.
        await asyncio.sleep(0.2)
        if player.current is not None:
            await interaction.followup.send(f"Skipped. Now playing: **{player.current.title}**.")
        elif player.queue:
            await interaction.followup.send("Skipped.")
        else:
            await interaction.followup.send("Skipped. The queue is now empty.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue.")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        await player.stop_all()
        await interaction.response.send_message("Stopped playback and cleared the queue.")

    # -- queue / info -----------------------------------------------------

    @app_commands.command(name="queue", description="Show the current queue.")
    async def queue(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        lines: list[str] = []
        if player.current is not None:
            lines.append(f"Now playing: **{player.current.title}** "
                         f"({_format_duration(player.current.duration)}) — {player.current.requester}")
        if player.queue:
            lines.append(f"Up next ({len(player.queue)}):")
            for i, track in enumerate(list(player.queue)[:10], start=1):
                lines.append(f"{i}. **{track.title}** "
                             f"({_format_duration(track.duration)}) — {track.requester}")
            if len(player.queue) > 10:
                lines.append(f"... and {len(player.queue) - 10} more.")
        else:
            lines.append("The queue is empty.")
        lines.append(f"Loop: {player.loop} | Volume: {player.volume}%")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Show the currently playing track.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        if player.current is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        track = player.current
        await interaction.response.send_message(
            f"Now playing: **{track.title}**\n"
            f"Duration: {_format_duration(track.duration)} | "
            f"Requested by: {track.requester} | "
            f"Loop: {player.loop} | Volume: {player.volume}%\n{track.url}")

    @app_commands.command(name="volume", description="Set playback volume (0-100).")
    @app_commands.describe(level="Volume percent, 0 to 100")
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        if not await self._guild_only(interaction):
            return
        if level < 0 or level > 100:
            await interaction.response.send_message(
                "Volume must be between 0 and 100.", ephemeral=True)
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        player.volume = level
        voice = player.voice
        if voice is not None and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = level / 100
        await interaction.response.send_message(f"Volume set to {level}%.")

    @app_commands.command(name="shuffle", description="Shuffle the queue (not the current track).")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        if len(player.queue) < 2:
            await interaction.response.send_message(
                "Not enough tracks in the queue to shuffle.", ephemeral=True)
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await interaction.response.send_message("Queue shuffled.")

    @app_commands.command(name="loop", description="Cycle loop mode: off -> track -> queue -> off.")
    async def loop(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._player(interaction.guild.id)  # type: ignore[union-attr]
        order = list(GuildPlayer.LOOP_MODES)
        nxt = order[(order.index(player.loop) + 1) % len(order)]
        player.loop = nxt
        meaning = {"off": "Loop off.", "track": "Looping the current track.",
                   "queue": "Looping the whole queue."}[nxt]
        await interaction.response.send_message(f"Loop mode: **{nxt}** — {meaning}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
