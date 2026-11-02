"""Premium embed design system for NEWR Music.

Palette (shared across NEWR bots):
- blurple 0x5865F2: primary / info
- success 0x57F287: confirmations
- error 0xED4245: errors and denials
- warning 0xFEE75C: warnings
- gold 0xF5C518: reserved for starboard / leaderboard / rep only;
  Music never uses gold.

Every embed carries the footer "NEWR Music • NEWR LABS" and a
timestamp via discord.utils.utcnow(). Titles use a single leading
emoji, descriptions stay clean, and no message shouts in ALL-CAPS.
"""
from __future__ import annotations

import discord

BLURPLE = 0x5865F2
SUCCESS = 0x57F287
ERROR = 0xED4245
WARNING = 0xFEE75C
GOLD = 0xF5C518  # not used by Music; kept for palette parity

FOOTER = "NEWR Music • NEWR LABS"


def panel(title: str, description: str, colour: int) -> discord.Embed:
    """Base embed: title, description, colour, footer, timestamp."""
    embed = discord.Embed(title=title, description=description, colour=colour)
    embed.set_footer(text=FOOTER)
    embed.timestamp = discord.utils.utcnow()
    return embed


def make(title: str, description: str, colour: int,
         fields: list[tuple[str, str, bool]] | None = None,
         thumbnail: str | None = None) -> discord.Embed:
    """Embed with optional fields (name, value, inline) and thumbnail."""
    embed = panel(title, description, colour)
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


def ok(title: str, description: str) -> discord.Embed:
    """Small success confirmation embed."""
    return panel(title, description, SUCCESS)


def err(title: str, description: str) -> discord.Embed:
    """Small error / denial embed."""
    return panel(title, description, ERROR)


def warn(title: str, description: str) -> discord.Embed:
    """Small warning embed."""
    return panel(title, description, WARNING)


def info(title: str, description: str) -> discord.Embed:
    """Small informational embed."""
    return panel(title, description, BLURPLE)
