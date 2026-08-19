from __future__ import annotations

from datetime import datetime, timedelta

import discord

from bot.maps import PlayerMaps, format_map_block
from bot.week import PlayerWeek

WEEK_FOOTER = "CS2 matchmaking · ELO from midnight snapshots"


def format_week_of(week_start: datetime) -> str:
    return f"Week: {week_start.strftime('%B')} {_ordinal(week_start.day)}"


def build_week_embed(
    weeks: list[PlayerWeek],
    *,
    week_start: datetime,
    completed: bool,
) -> discord.Embed:
    end = week_start + timedelta(days=6)
    title = "FACEIT week" if completed else "FACEIT week so far"
    embed = discord.Embed(
        title=title,
        description=f"{week_start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=WEEK_FOOTER)

    if not weeks:
        embed.add_field(name="Roster", value="No players tracked yet. Use `/addplayer`.", inline=False)
        return embed

    week_line = format_week_of(week_start)
    for week in weeks:
        embed.add_field(
            name=week.nickname,
            value=_player_body(week, week_line)[:1024],
            inline=False,
        )
    return embed


def _player_body(week: PlayerWeek, week_line: str) -> str:
    if week.error:
        return f"Peak Elo: {week.peak_elo if week.peak_elo is not None else 0}\n{week_line}\n{week.error}"

    if week.calibrating:
        header = "Calibrating — no ELO yet"
    elif week.started_on:
        header = _started_line(week, prefix=f"Started tracking {week.started_on}")
    else:
        header = _started_line(week, prefix="Started Sunday")

    lines = [f"Peak Elo: {week.peak_elo if week.peak_elo is not None else 0}", week_line, header, "", "```"]
    for day in week.days:
        lines.append(_format_day(day))
    lines.append("```")
    lines.append(_format_total(week))
    return "\n".join(lines)


def _started_line(week: PlayerWeek, *, prefix: str) -> str:
    if week.start_elo is None:
        return f"{prefix} at unknown ELO"
    level = f" (Lvl {week.start_level})" if week.start_level is not None else ""
    return f"{prefix} at {week.start_elo} ELO{level}"


def _format_day(day) -> str:
    if day.na:
        return f"{day.name:<3}   N/A"
    maps_label = "map" if day.maps == 1 else "maps"
    elo = _format_delta(day.elo_delta)
    return f"{day.name:<3}   {day.maps:>2} {maps_label:<5}  {day.wins}W-{day.losses}L   {elo}"


def _format_total(week: PlayerWeek) -> str:
    record = f"{week.total_wins}W-{week.total_losses}L"
    if week.calibrating:
        return f"Total  {record}"
    delta = _format_delta(week.total_elo_delta)
    if week.start_elo is not None and week.end_elo is not None:
        return f"Total  {delta} ELO  ({week.start_elo} → {week.end_elo})   {record}"
    return f"Total  {delta} ELO   {record}"


def _format_delta(delta: int | None) -> str:
    if delta is None:
        return "—"
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def build_daily_embed(players: list[PlayerMaps], *, day: datetime) -> discord.Embed:
    embed = discord.Embed(
        title="FACEIT daily breakdown",
        description=day.strftime("%A, %B ") + _ordinal(day.day),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="CS2 matchmaking")
    if not players:
        embed.add_field(name="Roster", value="No players tracked yet.", inline=False)
        return embed
    for player in players:
        blocks = _daily_player_blocks(player)
        for index, block in enumerate(blocks):
            name = player.nickname if index == 0 else f"{player.nickname} (cont.)"
            embed.add_field(name=name, value=block[:1024], inline=False)
    return embed


def build_last_map_embed(player: PlayerMaps) -> discord.Embed:
    embed = discord.Embed(
        title=f"Last map · {player.nickname}",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="CS2 matchmaking")
    if player.error:
        embed.description = player.error
        return embed
    if not player.maps:
        embed.description = "No recent matchmaking maps found."
        return embed
    embed.description = format_map_block(player.maps[-1])
    return embed


def _daily_player_blocks(player: PlayerMaps) -> list[str]:
    if player.error:
        return [player.error]
    if not player.maps:
        return ["No matchmaking maps today."]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for stats in player.maps:
        block = format_map_block(stats)
        extra = len(block) + (2 if current else 0)
        if current and size + extra > 1024:
            chunks.append("\n\n".join(current))
            current = [block]
            size = len(block)
        else:
            current.append(block)
            size += extra
    if current:
        chunks.append("\n\n".join(current))
    return chunks
