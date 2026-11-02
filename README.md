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

## Commands (18 top-level, 21 invocations, well under Discord's 100 cap)

`/favorites` is one top-level slash group with 4 subcommands, so the
tree holds 18 top-level commands (17 flat commands + the group).

| Slash command | What it does |
|---|---|
| `/join` | Join your current voice channel. If a queue was saved from an earlier session, it resumes automatically, starting with the previously current track. |
| `/leave` | Leave voice and clear the queue. |
| `/play <query-or-url>` | Play a song: search term or audio/video URL. Auto-joins your voice channel if you are in one; if you are not in a voice channel, it tells you to join one first. Queues if something is already playing. The now-playing message carries the player card (see below). |
| `/pause` | Pause the current track. |
| `/resume` | Resume the paused track. |
| `/skip` | Skip the current track (in queue-loop mode the skipped track goes to the back of the queue). DJ or staff only (see DJ gate). |
| `/previous` | Go back to the previous track (unskip). The interrupted current track goes back to the front of the queue. |
| `/seek <seconds>` | Seek within the current track. Rejects negative values, positions past the end, and live/unknown-length tracks with a clear ephemeral error instead of crashing. |
| `/stop` | Stop playback, clear the queue, turn loop off. DJ or staff only. |
| `/queue` | Show now-playing plus the next 10 queued tracks, loop mode, and volume. |
| `/remove <position>` | Remove a queued track by 1-based position (position 1 is the first queued track, not the current one). Out-of-range positions get an ephemeral error. |
| `/move <from> <to>` | Move a queued track to a different 1-based position. Out-of-range positions get an ephemeral error. |
| `/clear` | Clear the queue, keeping the current track playing. DJ or staff only. |
| `/nowplaying` | Show the current track, position/duration, requester, loop mode, and volume. |
| `/volume <0-100>` | Set playback volume (0–100). Uses `PCMVolumeTransformer`, so it also applies to the track already playing. |
| `/shuffle` | Shuffle the queue (the current track is untouched). |
| `/loop` | Cycle loop mode: `off` → `track` → `queue` → `off`. |
| `/favorites create <name> <query>` | Save a song/search term as a per-server favorite. Names are case-insensitive and must be unique. |
| `/favorites list` | List saved favorites for this server. |
| `/favorites use <name>` | Play a saved favorite (same flow as `/play`). |
| `/favorites remove <name>` | Remove a saved favorite. |

Loop modes: `off` = play through once; `track` = repeat the current
track; `queue` = repeat the whole queue (a finished track goes to the
back). `/stop` and `/leave` reset loop to `off`.

## Player card (buttons on the /play message)

When a track starts playing, the `/play` message includes buttons:
**Pause/Resume**, **Skip**, **Queue**, and **Shuffle**. The Skip
button enforces the same DJ gate as `/skip`. The buttons expire
after 30 minutes (and are then removed from the message); expiry
never stops playback, and all slash commands keep working.

## DJ gate

`/skip`, `/stop`, and `/clear` are restricted to users who are staff
(guild owner, Administrator, or Manage Server permission) **or** who
hold a role named `DJ` (case-insensitive). If no `DJ` role exists on
the server, the DJ branch matches nobody, so staff-only applies.
Everyone else gets a clear ephemeral denial. All other commands stay
open as before.

## Auto-leave

If the bot ends up alone in its voice channel (everyone else left),
it waits ~60 seconds; if nobody has come back by then, it
disconnects and clears the queue state. If the bot is disconnected
externally (kicked or moved out), the saved queue is kept, not wiped,
so it can resume on the next join.

## Queue persistence

The current track and queue are stored in SQLite (`data.db` in the
bot directory, never committed) on every change. When the bot joins
voice again, the saved queue resumes automatically, starting with
the previously current track; `/queue` shows it. Saved stream URLs
can expire between sessions, so a resumed track whose stream fails
is re-extracted once from its page URL before giving up.

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
   Expect `Synced 18 slash commands`.

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
- Queue state is persisted in SQLite (`data.db`, never committed) and
  resumes on the next voice join; see "Queue persistence" above. The
  in-memory queue is only a working copy of that saved state.
- Do not run this bot until a real token is in `.env`; the run script
  refuses to start with an empty/placeholder token.
