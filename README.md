# NEWR Music

Discord music bot for **NEWR LABS** (guild ID `1465422166790242377`).
Slash commands only, guild-synced to NEWR LABS — never global.

## Slash-only — no prefix commands

**Every command in this bot is a slash command** (`discord.app_commands`,
guild-synced). There are no prefix commands: no `!play`, no `!join`,
and no command that runs from a plain chat message. There is no
`on_message` handler in this bot at all, so typing in chat cannot
trigger anything. All user-triggered features are reached through
Discord's `/` command picker in NEWR LABS.

## Commands (12 total, well under Discord's 100 cap)

| Slash command | What it does |
|---|---|
| `/join` | Join your current voice channel. |
| `/leave` | Leave voice and clear the queue. |
| `/play <query-or-url>` | Play a song: search term or audio/video URL. Auto-joins your voice channel if you are in one; if you are not in a voice channel, it tells you to join one first. Queues if something is already playing. |
| `/pause` | Pause the current track. |
| `/resume` | Resume the paused track. |
| `/skip` | Skip the current track (in queue-loop mode the skipped track goes to the back of the queue). |
| `/stop` | Stop playback, clear the queue, turn loop off. |
| `/queue` | Show now-playing plus the next 10 queued tracks, loop mode, and volume. |
| `/nowplaying` | Show the current track, duration, requester, loop mode, and volume. |
| `/volume <0-100>` | Set playback volume (0–100). Uses `PCMVolumeTransformer`, so it also applies to the track already playing. |
| `/shuffle` | Shuffle the queue (the current track is untouched). |
| `/loop` | Cycle loop mode: `off` → `track` → `queue` → `off`. |

Loop modes: `off` = play through once; `track` = repeat the current
track; `queue` = repeat the whole queue (a finished track goes to the
back). `/stop` and `/leave` reset loop to `off`.

## Setup

1. Create a Discord application + bot in the Developer Portal (not done
   by this build — no application was created, no bot was invited, and
   nothing was pushed to GitHub).
2. Enable intents in the portal: **Server Members Intent is not
   required** for this bot; `voice_states` is required, and
   `message_content` is enabled in code for parity but is not used by
   any slash command.
3. Copy `.env.example` to `.env` and put the bot token in it:
   `DISCORD_TOKEN=<your token>` and `GUILD_ID=1465422166790242377`.
   Never commit `.env` (it is chmod 600 and gitignored).
4. Invite the bot to NEWR LABS with at least these permissions:
   View Channel, Connect, Speak, Use Application Commands
   (slash commands). No administrator permission is needed.
5. Run it:
   ```bash
   bash ~/workspace/bots/ops/run-newr-music.sh
   ```
   The script creates `~/workspace/bots/newr-music/.venv` if needed,
   installs `requirements.txt`, and logs to
   `~/workspace/bots/logs/newr-music.log`. On ready it prints
   `Logged in as ...` and `Synced N slash commands to guild 1465422166790242377`.
   Expect `Synced 12 slash commands`.

## How it works

- `bot.py` is thin: intents, the proxy helper, guild sync on ready,
  and loading `cogs.music`. All player logic lives in `cogs/music.py`.
- Playback is `discord.py` `VoiceClient` + `yt-dlp`, per-guild player
  state (`GuildPlayer`: queue, current track, loop mode, volume).
- **Stream only — nothing is downloaded to disk.** yt-dlp runs with
  `download=False` and the bot plays the stream URL through ffmpeg
  (`/usr/bin/ffmpeg` on this VM, with reconnect options). No temp
  audio file is created, so there is nothing to clean up.
- This VM's egress goes through an HTTP proxy: direct TLS to Discord
  fails here. Like the NEWR Support / NEWR AI bots, this bot passes
  the same `_proxy_kwargs()` to discord.py, and yt-dlp gets the proxy
  from the `HTTPS_PROXY`/`https_proxy` environment as well. Proxy
  credentials are never logged.

## Notes / limits

- If yt-dlp extraction or voice playback fails (bad URL, no results,
  source blocking extraction, voice disconnect), the command replies
  with a clear error message — the bot does not crash.
- Audio sources that yt-dlp cannot extract a stream URL from cannot be
  played; this is a source/yt-dlp limitation, not a queue bug.
- Playback is one stream per guild (one voice channel at a time per
  server). `/play` from a different voice channel moves the bot to
  the caller's channel.
- Queue state is in memory only: restarting the bot clears the queue.
- Do not run this bot until a real token is in `.env`; the run script
  refuses to start with an empty/placeholder token.
