"""NEWR Music via Lavalink (wavelink) — the famous-bot architecture.

Why Lavalink, not direct discord.py voice: this VM blocks UDP entirely
(even to localhost), and Discord voice is UDP-only, so a direct voice
client can never finish its handshake here (discover_ip timeout). With
Lavalink, this bot only talks TCP/WebSocket+REST to an external Lavalink
node (through the VM HTTP proxy); the node itself does the UDP voice
connection to Discord and streams the audio. That is how Muse, JMusicBot
successors and most production music bots work in 2026.

Nodes are free public Lavalink v4 nodes, no sign-up, no token in code:
primary Millohost, fallback Jirayu. If a node dies, wavelink fails over
to the other. No Lavalink password is a secret of ours; these are public
community nodes, listed on lavalink.darrennathanael.com.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from urllib.parse import unquote, urlparse

import aiohttp
import discord
import wavelink
from discord import app_commands
from discord.ext import commands

import ui

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.db")
AUTO_LEAVE_GRACE_SECONDS = 60
DJ_ROLE_NAME = "DJ"

# Public Lavalink v4 nodes (verified reachable from this VM on 2026-11-01).
NODES = [
    ("https://lava-v4.millohost.my.id:443", "https://discord.gg/mjS5J2K3ep"),
    ("https://lavalink.jirayu.net:443", "youshallnotpass"),
]


def _proxy_parts():
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return "", {}
    p = urlparse(raw)
    url = f"{p.scheme}://{p.hostname}:{p.port or 3128}"
    headers = {}
    if p.username:
        headers = {"Proxy-Authorization": aiohttp.encode_basic_auth(unquote(p.username), unquote(p.password or ""))}
    return url, headers


PROXY_URL, PROXY_HEADERS = _proxy_parts()


class _ProxySession(aiohttp.ClientSession):
    """aiohttp session that routes Lavalink WS+REST through the VM proxy.

    wavelink's websocket connect does not pass a proxy itself, and a
    default session does not use the proxy env for wss, so without this
    the node connection fails with SSL WRONG_VERSION_NUMBER. REST and
    WS both go through the same proxy here; credentials stay in env.
    """

    async def ws_connect(self, *args, **kwargs):
        kwargs.setdefault("proxy", PROXY_URL)
        kwargs.setdefault("proxy_headers", PROXY_HEADERS)
        return await super().ws_connect(*args, **kwargs)

    async def _request(self, *args, **kwargs):
        kwargs.setdefault("proxy", PROXY_URL)
        kwargs.setdefault("proxy_headers", PROXY_HEADERS)
        return await super()._request(*args, **kwargs)


def _db_init() -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS favorites ("
                " guild_id INTEGER NOT NULL, name_lower TEXT NOT NULL,"
                " name TEXT NOT NULL, query TEXT NOT NULL,"
                " PRIMARY KEY (guild_id, name_lower))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS playlists ("
                " guild_id INTEGER NOT NULL, name_lower TEXT NOT NULL,"
                " name TEXT NOT NULL, PRIMARY KEY (guild_id, name_lower))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS playlist_tracks ("
                " guild_id INTEGER NOT NULL, name_lower TEXT NOT NULL,"
                " position INTEGER NOT NULL, query TEXT NOT NULL, title TEXT NOT NULL,"
                " PRIMARY KEY (guild_id, name_lower, position))"
            )
    except Exception:
        pass


def _fmt(ms: int) -> str:
    if not ms:
        return "?:??"
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _track_link(track) -> str:
    title = getattr(track, "title", "Unknown") or "Unknown"
    uri = getattr(track, "uri", None)
    return f"[{title}]({uri})" if uri else f"**{title}**"


def _is_staff_or_dj(member: discord.Member) -> bool:
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(r.name.lower() == DJ_ROLE_NAME.lower() for r in member.roles)


class PlayerView(discord.ui.View):
    def __init__(self, cog: "Music"):
        super().__init__(timeout=1800)
        self.cog = cog
        self.message: discord.Message | None = None

    async def _player(self, interaction: discord.Interaction):
        player = self.cog._get_player(interaction.guild) if interaction.guild else None
        if player is None:
            await interaction.response.send_message(embed=ui.err("⏹️ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return None
        return player

    @discord.ui.button(label="Pause/Resume", emoji="⏸️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self._player(interaction)
        if player is None:
            return
        await player.pause(not player.paused)
        await interaction.response.send_message(embed=ui.ok("⏸️ Paused" if player.paused else "▶️ Resumed", "Playback paused." if player.paused else "Playback resumed."), ephemeral=True)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self._player(interaction)
        if player is None:
            return
        if not await self.cog._require_dj_or_staff(interaction, "skip tracks"):
            return
        skipped = await player.skip(force=True)
        await interaction.response.send_message(embed=ui.ok("⏭️ Skipped", f"Skipped **{skipped.title}**." if skipped else "Skipped."), ephemeral=True)

    @discord.ui.button(label="Queue", emoji="📋", style=discord.ButtonStyle.secondary)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self._player(interaction)
        if player is None:
            return
        await interaction.response.send_message(embed=self.cog._queue_embed(player), ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = await self._player(interaction)
        if player is None:
            return
        player.queue.shuffle()
        await interaction.response.send_message(embed=ui.ok("🔀 Shuffled", "Queue shuffled."), ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._auto_leave_tasks: dict[int, asyncio.Task] = {}
        self._stay: set[int] = set()  # guild ids with /247 on
        _db_init()

    async def cog_load(self) -> None:
        nodes = [wavelink.Node(uri=uri, password=pw, session=_ProxySession()) for uri, pw in NODES]
        await wavelink.Pool.connect(nodes=nodes, client=self.bot)
        print("[music] Lavalink pool connected (Millohost + Jirayu)")

    # -- helpers ---------------------------------------------------------
    def _get_player(self, guild: discord.Guild | None):
        if guild is None:
            return None
        vc = guild.voice_client
        return vc if isinstance(vc, wavelink.Player) else None

    async def _guild_only(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(embed=ui.err("🏠 Guild Only", "Use this command in a server, not in DMs."), ephemeral=True)
            return False
        return True

    async def _require_dj_or_staff(self, interaction: discord.Interaction, action: str) -> bool:
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None  # type: ignore[union-attr]
        if member is None or _is_staff_or_dj(member):
            return True
        await interaction.response.send_message(embed=ui.err("🎧 DJ Only", f"Only staff or members with the DJ role can {action}."), ephemeral=True)
        return False

    def _cancel_auto_leave(self, guild_id: int) -> None:
        task = self._auto_leave_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _ensure_player(self, interaction: discord.Interaction):
        guild = interaction.guild
        assert guild is not None
        player = self._get_player(guild)
        member = guild.get_member(interaction.user.id)
        user_channel = member.voice.channel if member and member.voice else None
        if player is None:
            if user_channel is None:
                await interaction.followup.send(embed=ui.err("🔊 Not in Voice", "I am not in a voice channel. Join a voice channel first, then use /play again (or use /join while you are in a voice channel)."))
                return None
            try:
                player = await user_channel.connect(cls=wavelink.Player)
            except Exception as exc:
                print(f"[music] lavalink join failed guild {guild.id}: {type(exc).__name__}: {exc}")
                await interaction.followup.send(embed=ui.err("🔊 Voice Error", f"Could not join voice ({type(exc).__name__}: {exc or 'unknown error'}). Please try again."))
                return None
        elif user_channel is not None and player.channel != user_channel:
            try:
                await player.move_to(user_channel)
            except Exception as exc:
                await interaction.followup.send(embed=ui.err("🔊 Voice Error", f"Could not move to your voice channel: {exc}"))
                return None
        return player

    def _progress_bar(self, player, track) -> str:
        total = track.length or 1
        pos = min(player.position, total)
        filled = int((pos / total) * 20) if total else 0
        bar = "▬" * filled + "🔘" + "▬" * max(0, 19 - filled)
        return f"{bar}  {_fmt(pos)} / {_fmt(total)}"

    def _player_embed(self, player, track) -> discord.Embed:
        return ui.make(
            "🎵 Now Playing", f"{_track_link(track)}\n\n{self._progress_bar(player, track)}", ui.BLURPLE,
            fields=[
                ("Position", f"{_fmt(player.position)} / {_fmt(track.length)}", True),
                ("Requested by", getattr(track, "requester", "—") if hasattr(track, "requester") else "—", True),
                ("Loop", {wavelink.QueueMode.normal: "off", wavelink.QueueMode.loop: "track", wavelink.QueueMode.loop_all: "queue"}.get(player.queue.mode, "off"), True),
                ("Volume", f"{player.volume}%", True),
                ("Queue", f"{len(player.queue)} track(s)", True),
            ],
            thumbnail=getattr(track, "artwork", None),
        )

    def _queue_embed(self, player) -> discord.Embed:
        if player.current is not None:
            description = f"▶️ Now playing: {_track_link(player.current)} ({_fmt(player.current.length)})"
        else:
            description = "Nothing is playing right now."
        fields = []
        if not player.queue.is_empty:
            lines = [f"{i}. **{t.title}** ({_fmt(t.length)})" for i, t in enumerate(list(player.queue)[:10], start=1)]
            if len(player.queue) > 10:
                lines.append(f"... and {len(player.queue) - 10} more.")
            fields.append((f"Up next ({len(player.queue)})", "\n".join(lines), False))
        else:
            fields.append(("Up next", "The queue is empty.", False))
        fields.append(("Loop", {wavelink.QueueMode.normal: "off", wavelink.QueueMode.loop: "track", wavelink.QueueMode.loop_all: "queue"}.get(player.queue.mode, "off"), True))
        fields.append(("Volume", f"{player.volume}%", True))
        return ui.make("📋 Queue", description, ui.BLURPLE, fields=fields)

    async def _play_query(self, interaction: discord.Interaction, player, query: str, requester: str):
        try:
            tracks = await wavelink.Playable.search(query)
        except Exception as exc:
            print(f"[music] lavalink search failed: {type(exc).__name__}: {exc}")
            await interaction.followup.send(embed=ui.err("⚠️ Playback Error", f"Sorry, I could not find that: {exc}"))
            return
        if not tracks:
            await interaction.followup.send(embed=ui.err("⚠️ Not Found", "No tracks found for that search."))
            return
        track = tracks[0]
        # Remember who requested it for the card (wavelink tracks don't carry it).
        track.requester = requester  # type: ignore[attr-defined]
        if player.playing or player.paused:
            await player.queue.put_wait(track)
            await interaction.followup.send(embed=ui.info("➕ Queued", f"Queued #{len(player.queue)}: **{track.title}** ({_fmt(track.length)}) — requested by {requester}"))
        else:
            await player.play(track)
            view = PlayerView(self)
            message = await interaction.followup.send(embed=self._player_embed(player, track), view=view)
            view.message = message

    # -- listeners -------------------------------------------------------
    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if player is None:
            return
        print(f"[music] track ended guild {player.guild.id}: {payload.reason}")
        # BUG FIX (2026-11-01): wavelink 3 does NOT auto-advance a normal
        # queue when autoplay is disabled — its _auto_play_event returns
        # early in that case, so after /skip (or a finished track) the
        # queued songs just sat there with nothing playing, and a later
        # /play only queued again instead of playing. This handler is the
        # queue advancer. When autoplay is enabled/partial, wavelink
        # handles advancement itself, so we stay out of its way there.
        if payload.reason == "replaced":
            return
        if player.autoplay != wavelink.AutoPlayMode.disabled:
            return
        if player.queue.mode == wavelink.QueueMode.loop:
            if payload.track is not None:
                await player.play(payload.track)
            return
        if player.queue.mode == wavelink.QueueMode.loop_all and payload.track is not None:
            await player.queue.put_wait(payload.track)
        if not player.queue.is_empty:
            try:
                nxt = player.queue.get()
            except Exception:
                return
            await player.play(nxt)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        if payload.player and payload.track:
            print(f"[music] track started guild {payload.player.guild.id}: {payload.track.title}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if self.bot.user is None or member.id == self.bot.user.id:
            return
        guild = member.guild
        player = self._get_player(guild)
        if player is None or player.channel is None:
            return
        channel = player.channel
        if before.channel != channel and after.channel != channel:
            return
        others = [uid for uid in channel.voice_states if uid != self.bot.user.id]
        if not others:
            if guild.id not in self._auto_leave_tasks:
                self._auto_leave_tasks[guild.id] = asyncio.create_task(self._auto_leave_after(guild.id, channel.id))
        else:
            self._cancel_auto_leave(guild.id)

    async def _auto_leave_after(self, guild_id: int, channel_id: int) -> None:
        try:
            await asyncio.sleep(AUTO_LEAVE_GRACE_SECONDS)
            guild = self.bot.get_guild(guild_id)
            player = self._get_player(guild) if guild else None
            if player is None or player.channel is None or player.channel.id != channel_id:
                return
            if self.bot.user is None:
                return
            others = [uid for uid in player.channel.voice_states if uid != self.bot.user.id]
            if others or guild_id in self._stay:
                return
            await player.disconnect()
        except asyncio.CancelledError:
            pass
        finally:
            self._auto_leave_tasks.pop(guild_id, None)

    # -- commands --------------------------------------------------------
    @app_commands.command(name="join", description="Join your current voice channel.")
    async def join(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        player = await self._ensure_player(interaction)
        if player is None:
            return
        self._cancel_auto_leave(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.followup.send(embed=ui.make("🔊 Joined", "Connected to voice via Lavalink.", ui.SUCCESS, fields=[("Channel", player.channel.mention if player.channel else "voice", False)]))

    @app_commands.command(name="leave", description="Leave the voice channel and clear the queue.")
    async def leave(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("🔊 Not Connected", "I am not in a voice channel."), ephemeral=True)
            return
        player.queue.clear()
        await player.disconnect()
        await interaction.response.send_message(embed=ui.ok("👋 Left", "Left the voice channel and cleared the queue."))

    @app_commands.command(name="play", description="Play a song by name or URL.")
    @app_commands.describe(query="Song name or URL")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        player = await self._ensure_player(interaction)
        if player is None:
            return
        self._cancel_auto_leave(interaction.guild.id)  # type: ignore[union-attr]
        await self._play_query(interaction, player, query, interaction.user.display_name)

    @app_commands.command(name="pause", description="Pause the current track.")
    async def pause(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or not player.playing:
            await interaction.response.send_message(embed=ui.err("⏹️ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await player.pause(True)
        await interaction.response.send_message(embed=ui.ok("⏸️ Paused", "Playback paused."))

    @app_commands.command(name="resume", description="Resume the paused track.")
    async def resume(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or not player.paused:
            await interaction.response.send_message(embed=ui.err("⏸️ Nothing Paused", "Nothing is paused right now."), ephemeral=True)
            return
        await player.pause(False)
        await interaction.response.send_message(embed=ui.ok("▶️ Resumed", "Playback resumed."))

    @app_commands.command(name="skip", description="Skip the current track. (DJ or staff only)")
    async def skip(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        if not await self._require_dj_or_staff(interaction, "skip tracks"):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("⏭️ Nothing to Skip", "Nothing is playing to skip."), ephemeral=True)
            return
        skipped = await player.skip(force=True)
        await interaction.response.send_message(embed=ui.ok("⏭️ Skipped", f"Skipped **{skipped.title}**." if skipped else "Skipped."))

    @app_commands.command(name="stop", description="Stop playback and clear the queue. (DJ or staff only)")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        if not await self._require_dj_or_staff(interaction, "stop playback"):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("⏹️ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        player.queue.clear()
        await player.stop(force=True)
        await interaction.response.send_message(embed=ui.ok("⏹️ Stopped", "Playback stopped and the queue was cleared."))

    @app_commands.command(name="queue", description="Show the current queue.")
    async def queue(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("📋 No Player", "I am not in a voice channel."), ephemeral=True)
            return
        await interaction.response.send_message(embed=self._queue_embed(player))

    @app_commands.command(name="nowplaying", description="Show the currently playing track.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("🎵 Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await interaction.response.send_message(embed=self._player_embed(player, player.current))

    @app_commands.command(name="volume", description="Set the playback volume (0-100).")
    @app_commands.describe(volume="Volume percent")
    async def volume(self, interaction: discord.Interaction, volume: int) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("🔊 No Player", "I am not in a voice channel."), ephemeral=True)
            return
        volume = max(0, min(100, volume))
        await player.set_volume(volume)
        await interaction.response.send_message(embed=ui.ok("🔊 Volume", f"Volume set to {volume}%."))

    @app_commands.command(name="shuffle", description="Shuffle the queue.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("🔀 No Player", "I am not in a voice channel."), ephemeral=True)
            return
        player.queue.shuffle()
        await interaction.response.send_message(embed=ui.ok("🔀 Shuffled", "Queue shuffled."))

    @app_commands.command(name="loop", description="Cycle loop mode: off, track, queue.")
    async def loop(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("🔁 No Player", "I am not in a voice channel."), ephemeral=True)
            return
        order = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
        nxt = order[(order.index(player.queue.mode) + 1) % len(order)]
        player.queue.mode = nxt
        label = {wavelink.QueueMode.normal: "off", wavelink.QueueMode.loop: "track", wavelink.QueueMode.loop_all: "queue"}[nxt]
        await interaction.response.send_message(embed=ui.ok("🔁 Loop", f"Loop mode set to {label}."))

    @app_commands.command(name="seek", description="Seek to a position in seconds.")
    @app_commands.describe(seconds="Position in seconds")
    async def seek(self, interaction: discord.Interaction, seconds: int) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("⏩ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        if not player.current.is_seekable:
            await interaction.response.send_message(embed=ui.err("⏩ Not Seekable", "This track cannot be seeked."), ephemeral=True)
            return
        length = player.current.length or 0
        if seconds < 0 or seconds * 1000 > length:
            await interaction.response.send_message(embed=ui.err("⏩ Invalid Position", f"Seek position must be between 0 and {length // 1000} seconds."), ephemeral=True)
            return
        await player.seek(seconds * 1000)
        await interaction.response.send_message(embed=ui.ok("⏩ Seeked", f"Seeked to {_fmt(seconds * 1000)}."))

    @app_commands.command(name="previous", description="Go back to the previous track.")
    async def previous(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or not player.queue.history:
            await interaction.response.send_message(embed=ui.err("⏮️ No Previous Track", "There is no previous track."), ephemeral=True)
            return
        prev = player.queue.history[-1]
        await player.play(prev)
        await interaction.response.send_message(embed=ui.ok("⏮️ Previous", f"Now playing: **{prev.title}**."))

    @app_commands.command(name="remove", description="Remove a track from the queue by position.")
    @app_commands.describe(position="Queue position (1-based)")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or position < 1 or position > len(player.queue):
            await interaction.response.send_message(embed=ui.err("🗑️ Invalid Position", "That queue position does not exist."), ephemeral=True)
            return
        removed = player.queue.remove(position - 1)
        await interaction.response.send_message(embed=ui.ok("🗑️ Removed", f"Removed **{removed.title}** from the queue."))

    @app_commands.command(name="move", description="Move a queued track to another position.")
    @app_commands.describe(from_position="Current position (1-based)", to_position="New position (1-based)")
    async def move(self, interaction: discord.Interaction, from_position: int, to_position: int) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or from_position < 1 or from_position > len(player.queue) or to_position < 1 or to_position > len(player.queue):
            await interaction.response.send_message(embed=ui.err("↕️ Invalid Position", "One of those queue positions does not exist."), ephemeral=True)
            return
        player.queue.swap(from_position - 1, to_position - 1)
        await interaction.response.send_message(embed=ui.ok("↕️ Moved", f"Moved queue position {from_position} to {to_position}."))

    @app_commands.command(name="clear", description="Clear the queue. (DJ or staff only)")
    async def clear(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        if not await self._require_dj_or_staff(interaction, "clear the queue"):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("🧹 No Player", "I am not in a voice channel."), ephemeral=True)
            return
        player.queue.clear()
        await interaction.response.send_message(embed=ui.ok("🧹 Cleared", "Queue cleared."))

    favorites = app_commands.Group(name="favorites", description="Manage your favorite songs.")

    @favorites.command(name="create", description="Save a song as a favorite.")
    @app_commands.describe(name="Favorite name", query="Song name or URL")
    async def favorites_create(self, interaction: discord.Interaction, name: str, query: str) -> None:
        if not await self._guild_only(interaction):
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO favorites VALUES (?, ?, ?, ?)", (interaction.guild.id, name.lower(), name, query))  # type: ignore[union-attr]
        await interaction.response.send_message(embed=ui.ok("⭐ Favorite Saved", f"Saved **{name}**."))

    @favorites.command(name="list", description="List your favorites.")
    async def favorites_list(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT name, query FROM favorites WHERE guild_id = ? ORDER BY name", (interaction.guild.id,)).fetchall()  # type: ignore[union-attr]
        if not rows:
            await interaction.response.send_message(embed=ui.info("⭐ Favorites", "No favorites saved yet. Use /favorites create."), ephemeral=True)
            return
        await interaction.response.send_message(embed=ui.make("⭐ Favorites", "\n".join(f"**{n}** — {q}" for n, q in rows), ui.BLURPLE))

    @favorites.command(name="use", description="Play a saved favorite.")
    @app_commands.describe(name="Favorite name")
    async def favorites_use(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT query FROM favorites WHERE guild_id = ? AND name_lower = ?", (interaction.guild.id, name.lower())).fetchone()  # type: ignore[union-attr]
        if row is None:
            await interaction.followup.send(embed=ui.err("⭐ Not Found", f"No favorite named **{name}**."), ephemeral=True)
            return
        player = await self._ensure_player(interaction)
        if player is None:
            return
        await self._play_query(interaction, player, row[0], interaction.user.display_name)

    @favorites.command(name="remove", description="Remove a saved favorite.")
    @app_commands.describe(name="Favorite name")
    async def favorites_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guild_only(interaction):
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("DELETE FROM favorites WHERE guild_id = ? AND name_lower = ?", (interaction.guild.id, name.lower()))  # type: ignore[union-attr]
            gone = cur.rowcount
        if gone:
            await interaction.response.send_message(embed=ui.ok("⭐ Removed", f"Removed favorite **{name}**."))
        else:
            await interaction.response.send_message(embed=ui.err("⭐ Not Found", f"No favorite named **{name}**."), ephemeral=True)

    # -- upgrade batch (2026-11-01): premium extras, famous-bot features ----
    @app_commands.command(name="search", description="Search songs and pick one to play.")
    @app_commands.describe(query="Song name or artist")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        try:
            tracks = await wavelink.Playable.search(query)
        except Exception as exc:
            await interaction.followup.send(embed=ui.err("🔍 Search Failed", f"Could not search: {exc}"))
            return
        if not tracks:
            await interaction.followup.send(embed=ui.err("🔍 Not Found", "No tracks found."))
            return
        top = list(tracks[:5])

        class SearchView(discord.ui.View):
            def __init__(self, cog, tracks):
                super().__init__(timeout=120)
                self.cog = cog
                self.tracks = tracks
                for i, tr in enumerate(tracks, start=1):
                    btn = discord.ui.Button(label=f"{i}. {tr.title[:40]}", style=discord.ButtonStyle.secondary)
                    async def _cb(inter, idx=i - 1, cog=cog, tracks=tracks):
                        await inter.response.defer(ephemeral=False)
                        player = await cog._ensure_player(inter)
                        if player is None:
                            return
                        track = tracks[idx]
                        track.requester = inter.user.display_name  # type: ignore[attr-defined]
                        if player.playing or player.paused or not player.queue.is_empty:
                            await player.queue.put_wait(track)
                            await inter.followup.send(embed=ui.info("➕ Queued", f"Queued: **{track.title}**"))
                        else:
                            await player.play(track)
                            await inter.followup.send(embed=cog._player_embed(player, track))
                    btn.callback = _cb
                    self.add_item(btn)

        desc = "\n".join(f"**{i}.** {tr.title} ({_fmt(tr.length)}) — {tr.author}" for i, tr in enumerate(top, start=1))
        await interaction.followup.send(embed=ui.make("🔍 Search Results", desc + f"\n\nTap a button to play. Search: `{query}`", ui.BLURPLE), view=SearchView(self, top))

    @app_commands.command(name="lyrics", description="Show lyrics for the currently playing track (free lrclib).")
    async def lyrics(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("🎤 Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        track = player.current
        artist = getattr(track, "author", "") or ""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://lrclib.net/api/get", params={"artist_name": artist, "track_name": track.title, "duration": int(track.length // 1000) if track.length else 0}, proxy=PROXY_URL, headers=PROXY_HEADERS, timeout=15) as r:
                    data = await r.json() if r.status == 200 else {}
        except Exception:
            data = {}
        text = (data.get("syncedLyrics") or data.get("plainLyrics") or "").strip()
        if not text:
            await interaction.followup.send(embed=ui.info("🎤 Lyrics", f"No lyrics found for **{track.title}**."))
            return
        # Strip timestamps from synced lyrics, cap to Discord limits.
        lines = []
        for line in text.splitlines():
            if "]" in line and line.startswith("["):
                line = line.split("]", 1)[1]
            lines.append(line)
        clean = "\n".join(lines)[:3900]
        await interaction.followup.send(embed=ui.make("🎤 Lyrics", f"**{track.title}** — {artist}\n\n{clean}", ui.BLURPLE))

    @app_commands.command(name="autoplay", description="Toggle autoplay: keep playing similar tracks when the queue ends.")
    async def autoplay(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.err("▶️ No Player", "I am not in a voice channel."), ephemeral=True)
            return
        player.autoplay = wavelink.AutoPlayMode.disabled if player.autoplay == wavelink.AutoPlayMode.enabled else wavelink.AutoPlayMode.enabled
        state = "on" if player.autoplay == wavelink.AutoPlayMode.enabled else "off"
        await interaction.response.send_message(embed=ui.ok("▶️ Autoplay", f"Autoplay is now {state}."))

    @app_commands.command(name="247", description="Toggle 24/7 mode: stay in voice even when alone.")
    async def stay247(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        gid = interaction.guild.id  # type: ignore[union-attr]
        if gid in self._stay:
            self._stay.discard(gid)
            await interaction.response.send_message(embed=ui.ok("🕐 24/7 Off", "I will leave when the channel is empty again."))
        else:
            self._stay.add(gid)
            self._cancel_auto_leave(gid)
            await interaction.response.send_message(embed=ui.ok("🕐 24/7 On", "I will stay in voice even when alone. Use /247 again to turn it off, or /leave to disconnect."))

    @app_commands.command(name="skipto", description="Skip straight to a queue position.")
    @app_commands.describe(position="Queue position (1-based)")
    async def skipto(self, interaction: discord.Interaction, position: int) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or position < 1 or position > len(player.queue):
            await interaction.response.send_message(embed=ui.err("⏭️ Invalid Position", "That queue position does not exist."), ephemeral=True)
            return
        for _ in range(position - 1):
            player.queue.get()
        skipped = await player.skip(force=True)
        await interaction.response.send_message(embed=ui.ok("⏭️ Skipped To", f"Now playing position {position}." if skipped else "Skipped."))

    @app_commands.command(name="playnext", description="Play a song right after the current one.")
    @app_commands.describe(query="Song name or URL")
    async def playnext(self, interaction: discord.Interaction, query: str) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        player = await self._ensure_player(interaction)
        if player is None:
            return
        try:
            tracks = await wavelink.Playable.search(query)
        except Exception as exc:
            await interaction.followup.send(embed=ui.err("⚠️ Playback Error", f"Could not find that: {exc}"))
            return
        if not tracks:
            await interaction.followup.send(embed=ui.err("⚠️ Not Found", "No tracks found."))
            return
        track = tracks[0]
        track.requester = interaction.user.display_name  # type: ignore[attr-defined]
        if player.playing or player.paused:
            await player.queue.put_at(0, track)
            await interaction.followup.send(embed=ui.info("⏭️ Play Next", f"Will play next: **{track.title}**."))
        else:
            await player.play(track)
            await interaction.followup.send(embed=self._player_embed(player, track))

    @app_commands.command(name="replay", description="Replay the current track from the start.")
    async def replay(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("🔁 Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await player.seek(0)
        await interaction.response.send_message(embed=ui.ok("🔁 Replayed", f"Replaying **{player.current.title}**."))

    @app_commands.command(name="forward", description="Fast-forward 30 seconds.")
    async def forward(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("⏩ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await player.seek(min(player.position + 30000, player.current.length))
        await interaction.response.send_message(embed=ui.ok("⏩ Forward", "Skipped forward 30 seconds."))

    @app_commands.command(name="rewind", description="Rewind 30 seconds.")
    async def rewind(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("⏪ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await player.seek(max(player.position - 30000, 0))
        await interaction.response.send_message(embed=ui.ok("⏪ Rewind", "Rewound 30 seconds."))

    @app_commands.command(name="grab", description="Save the current track to your favorites.")
    @app_commands.describe(name="Favorite name")
    async def grab(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("⭐ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO favorites VALUES (?, ?, ?, ?)", (interaction.guild.id, name.lower(), name, player.current.uri or player.current.title))  # type: ignore[union-attr]
        await interaction.response.send_message(embed=ui.ok("⭐ Grabbed", f"Saved **{player.current.title}** as favorite **{name}**."))

    @app_commands.command(name="history", description="Show recently played tracks.")
    async def history(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None or not player.queue.history:
            await interaction.response.send_message(embed=ui.info("🕘 History", "No history yet in this session."), ephemeral=True)
            return
        hist = list(player.queue.history)[-10:][::-1]
        desc = "\n".join(f"**{i}.** {t.title} ({_fmt(t.length)})" for i, t in enumerate(hist, start=1))
        await interaction.response.send_message(embed=ui.make("🕘 Recently Played", desc, ui.BLURPLE))

    @app_commands.command(name="stats", description="Show the music player's status.")
    async def stats(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        player = self._get_player(interaction.guild)
        if player is None:
            await interaction.response.send_message(embed=ui.info("📊 Player Stats", "Not connected to voice."), ephemeral=True)
            return
        node = player.node.uri if player.node else "—"
        await interaction.response.send_message(embed=ui.make("📊 Player Stats", "Live Lavalink player status.", ui.BLURPLE, fields=[
            ("Channel", player.channel.mention if player.channel else "—", True),
            ("Playing", player.current.title if player.current else "—", True),
            ("Queue", f"{len(player.queue)} track(s)", True),
            ("Volume", f"{player.volume}%", True),
            ("Loop", {wavelink.QueueMode.normal: "off", wavelink.QueueMode.loop: "track", wavelink.QueueMode.loop_all: "queue"}.get(player.queue.mode, "off"), True),
            ("24/7", "on" if interaction.guild.id in self._stay else "off", True),  # type: ignore[union-attr]
            ("Node", node, False),
        ]))

    filter_group = app_commands.Group(name="filter", description="Audio filters (bassboost, nightcore, and more).")

    async def _apply_filter(self, interaction: discord.Interaction, filters: wavelink.Filters, label: str) -> None:
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            await interaction.response.send_message(embed=ui.err("🎛️ Nothing Playing", "Nothing is playing right now."), ephemeral=True)
            return
        await player.set_filters(filters)
        await interaction.response.send_message(embed=ui.ok("🎛️ Filter", f"Filter applied: {label}."))

    @filter_group.command(name="bassboost", description="Boost the bass.")
    async def filter_bassboost(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        f = wavelink.Filters()
        f.set_filters(equalizer=wavelink.Equalizer([{"band": 0, "gain": 0.6}, {"band": 1, "gain": 0.4}, {"band": 2, "gain": 0.2}]))
        await self._apply_filter(interaction, f, "Bassboost")

    @filter_group.command(name="nightcore", description="Faster + higher pitch (nightcore).")
    async def filter_nightcore(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        f = wavelink.Filters()
        f.set_filters(timescale=wavelink.Timescale({"speed": 1.25, "pitch": 1.25, "rate": 1.0}))
        await self._apply_filter(interaction, f, "Nightcore")

    @filter_group.command(name="vaporwave", description="Slower + lower pitch (vaporwave).")
    async def filter_vaporwave(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        f = wavelink.Filters()
        f.set_filters(timescale=wavelink.Timescale({"speed": 0.8, "pitch": 0.8, "rate": 1.0}))
        await self._apply_filter(interaction, f, "Vaporwave")

    @filter_group.command(name="8d", description="Rotating 8D audio effect.")
    async def filter_8d(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        f = wavelink.Filters()
        f.set_filters(rotation=wavelink.Rotation({"rotationHz": 0.2}))
        await self._apply_filter(interaction, f, "8D")

    @filter_group.command(name="karaoke", description="Karaoke filter (reduce vocals).")
    async def filter_karaoke(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        f = wavelink.Filters()
        f.set_filters(karaoke=wavelink.Karaoke({"level": 1.0, "monoLevel": 1.0, "filterBand": 220.0, "filterWidth": 100.0}))
        await self._apply_filter(interaction, f, "Karaoke")

    @filter_group.command(name="reset", description="Remove all audio filters.")
    async def filter_reset(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        await self._apply_filter(interaction, wavelink.Filters(), "Reset (no filter)")

    playlist_group = app_commands.Group(name="playlist", description="Manage server playlists.")

    @playlist_group.command(name="create", description="Create a playlist.")
    @app_commands.describe(name="Playlist name")
    async def playlist_create(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guild_only(interaction):
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO playlists VALUES (?, ?, ?)", (interaction.guild.id, name.lower(), name))  # type: ignore[union-attr]
        await interaction.response.send_message(embed=ui.ok("📂 Playlist", f"Playlist **{name}** created."))

    @playlist_group.command(name="add", description="Add a song to a playlist.")
    @app_commands.describe(name="Playlist name", query="Song name or URL")
    async def playlist_add(self, interaction: discord.Interaction, name: str, query: str) -> None:
        if not await self._guild_only(interaction):
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT name FROM playlists WHERE guild_id = ? AND name_lower = ?", (interaction.guild.id, name.lower())).fetchone()  # type: ignore[union-attr]
            if row is None:
                await interaction.response.send_message(embed=ui.err("📂 Not Found", f"No playlist named **{name}**."), ephemeral=True)
                return
            pos = conn.execute("SELECT COUNT(*) FROM playlist_tracks WHERE guild_id = ? AND name_lower = ?", (interaction.guild.id, name.lower())).fetchone()[0] + 1  # type: ignore[union-attr]
            conn.execute("INSERT INTO playlist_tracks VALUES (?, ?, ?, ?, ?)", (interaction.guild.id, name.lower(), pos, query, query))  # type: ignore[union-attr]
        await interaction.response.send_message(embed=ui.ok("📂 Added", f"Added to playlist **{name}** (#{pos})."))

    @playlist_group.command(name="list", description="List playlists and their songs.")
    @app_commands.describe(name="Playlist name (empty = all playlists)")
    async def playlist_list(self, interaction: discord.Interaction, name: str | None = None) -> None:
        if not await self._guild_only(interaction):
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            if name:
                rows = conn.execute("SELECT position, query FROM playlist_tracks WHERE guild_id = ? AND name_lower = ? ORDER BY position", (interaction.guild.id, name.lower())).fetchall()  # type: ignore[union-attr]
                if not rows:
                    await interaction.response.send_message(embed=ui.info("📂 Playlist", f"Playlist **{name}** is empty or does not exist."), ephemeral=True)
                    return
                desc = "\n".join(f"**{pos}.** {q}" for pos, q in rows)
                await interaction.response.send_message(embed=ui.make(f"📂 Playlist: {name}", desc, ui.BLURPLE))
            else:
                rows = conn.execute("SELECT name FROM playlists WHERE guild_id = ? ORDER BY name", (interaction.guild.id,)).fetchall()  # type: ignore[union-attr]
                if not rows:
                    await interaction.response.send_message(embed=ui.info("📂 Playlists", "No playlists yet. Use /playlist create."), ephemeral=True)
                    return
                await interaction.response.send_message(embed=ui.make("📂 Playlists", "\n".join(f"• **{r[0]}**" for r in rows), ui.BLURPLE))

    @playlist_group.command(name="play", description="Play a whole playlist.")
    @app_commands.describe(name="Playlist name")
    async def playlist_play(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guild_only(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT query FROM playlist_tracks WHERE guild_id = ? AND name_lower = ? ORDER BY position", (interaction.guild.id, name.lower())).fetchall()  # type: ignore[union-attr]
        if not rows:
            await interaction.followup.send(embed=ui.err("📂 Not Found", f"Playlist **{name}** is empty or does not exist."), ephemeral=True)
            return
        player = await self._ensure_player(interaction)
        if player is None:
            return
        queued = 0
        for (q,) in rows:
            try:
                tracks = await wavelink.Playable.search(q)
            except Exception:
                continue
            if not tracks:
                continue
            track = tracks[0]
            track.requester = interaction.user.display_name  # type: ignore[attr-defined]
            if player.playing or player.paused or not player.queue.is_empty or queued:
                await player.queue.put_wait(track)
            else:
                await player.play(track)
            queued += 1
        await interaction.followup.send(embed=ui.ok("📂 Playlist Playing", f"Playlist **{name}**: {queued} track(s) queued/playing."))

    @playlist_group.command(name="delete", description="Delete a playlist. (staff only)")
    @app_commands.describe(name="Playlist name")
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guild_only(interaction):
            return
        member = interaction.guild.get_member(interaction.user.id)  # type: ignore[union-attr]
        if member is None or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
            await interaction.response.send_message(embed=ui.err("⛔ Staff Only", "Only staff can delete playlists."), ephemeral=True)
            return
        _db_init()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM playlist_tracks WHERE guild_id = ? AND name_lower = ?", (interaction.guild.id, name.lower()))  # type: ignore[union-attr]
            cur = conn.execute("DELETE FROM playlists WHERE guild_id = ? AND name_lower = ?", (interaction.guild.id, name.lower()))  # type: ignore[union-attr]
        if cur.rowcount:
            await interaction.response.send_message(embed=ui.ok("📂 Deleted", f"Playlist **{name}** deleted."))
        else:
            await interaction.response.send_message(embed=ui.err("📂 Not Found", f"No playlist named **{name}**."), ephemeral=True)

    dj_group = app_commands.Group(name="dj", description="Manage the DJ role. (staff only)")

    @dj_group.command(name="add", description="Give a member the DJ role.")
    @app_commands.describe(member="Member to make DJ")
    async def dj_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._guild_only(interaction):
            return
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(embed=ui.err("⛔ Staff Only", "Only staff can manage DJs."), ephemeral=True)
            return
        role = discord.utils.get(interaction.guild.roles, name=DJ_ROLE_NAME)  # type: ignore[union-attr]
        if role is None:
            await interaction.response.send_message(embed=ui.err("🎧 No DJ Role", "The DJ role does not exist on this server."), ephemeral=True)
            return
        await member.add_roles(role)
        await interaction.response.send_message(embed=ui.ok("🎧 DJ Added", f"{member.mention} is now a DJ."))

    @dj_group.command(name="remove", description="Remove the DJ role from a member.")
    @app_commands.describe(member="Member to remove DJ from")
    async def dj_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._guild_only(interaction):
            return
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(embed=ui.err("⛔ Staff Only", "Only staff can manage DJs."), ephemeral=True)
            return
        role = discord.utils.get(interaction.guild.roles, name=DJ_ROLE_NAME)  # type: ignore[union-attr]
        if role is None:
            await interaction.response.send_message(embed=ui.err("🎧 No DJ Role", "The DJ role does not exist on this server."), ephemeral=True)
            return
        await member.remove_roles(role)
        await interaction.response.send_message(embed=ui.ok("🎧 DJ Removed", f"{member.mention} is no longer a DJ."))

    @dj_group.command(name="list", description="List current DJs.")
    async def dj_list(self, interaction: discord.Interaction) -> None:
        if not await self._guild_only(interaction):
            return
        role = discord.utils.get(interaction.guild.roles, name=DJ_ROLE_NAME)  # type: ignore[union-attr]
        if role is None or not role.members:
            await interaction.response.send_message(embed=ui.info("🎧 DJs", "No DJs yet."), ephemeral=True)
            return
        await interaction.response.send_message(embed=ui.make("🎧 DJs", "\n".join(m.mention for m in role.members), ui.BLURPLE))



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
