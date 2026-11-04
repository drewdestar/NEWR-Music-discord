# NEWR Music

Music bot for NEWR LABS. Slash commands only, synced to the server.

`/play`, `/pause`, `/resume`, `/skip`, `/previous`, `/seek`, `/stop`,
`/queue`, `/nowplaying`, `/volume`, `/shuffle`, `/loop`, plus
`/remove`, `/move`, `/clear`, and a `/favorites` group to save songs
per server. The now-playing message carries Pause / Skip / Queue /
Shuffle buttons.

`/skip`, `/stop`, and `/clear` are DJ-or-staff only (a role named DJ,
or Administrator / Manage Server). Everyone else can play, queue, and
look around freely.

The queue is saved in SQLite and resumes when the bot rejoins voice.
Streams only, nothing is downloaded to disk. If the bot ends up alone
in a voice channel, it leaves after about a minute.

Run it:

```bash
bash ~/workspace/bots/ops/run-newr-music.sh
```

Token goes in `.env` (`DISCORD_TOKEN`, `GUILD_ID` is NEWR LABS). The
script sets up the venv, installs requirements, and logs to
`~/workspace/bots/logs/newr-music.log`. The bot needs Connect, Speak,
and Use Application Commands in the server.
