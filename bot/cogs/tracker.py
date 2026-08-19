from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.db import SETTING_DAILY_CHANNEL_ID, SETTING_WEEKLY_CHANNEL_ID, Store
from bot.faceit import FaceitClient, FaceitError, FaceitNotFound
from bot.maps import collect_day_maps, last_map_for
from bot.report import build_daily_embed, build_last_map_embed, build_week_embed
from bot.week import collect_week_stats, completed_week_start, current_week_start, snapshot_all_players

logger = logging.getLogger(__name__)

WEEKLY_CHANNEL_NAME = "weekly-report"
DAILY_CHANNEL_NAME = "daily-breakdown"


class TrackerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        tz = ZoneInfo(bot.settings.timezone)
        self.midnight_snapshot.change_interval(time=time(hour=0, minute=0, tzinfo=tz))
        self.scheduled_posts.change_interval(
            time=[
                time(hour=22, minute=45, tzinfo=tz),
                time(hour=23, minute=59, tzinfo=tz),
            ]
        )
        self.midnight_snapshot.start()
        self.scheduled_posts.start()

    def cog_unload(self) -> None:
        self.midnight_snapshot.cancel()
        self.scheduled_posts.cancel()

    @property
    def store(self) -> Store:
        return self.bot.store

    @property
    def faceit(self) -> FaceitClient:
        return self.bot.faceit

    @app_commands.command(name="addplayer", description="Track a FACEIT player by nickname")
    @app_commands.describe(nickname="FACEIT nickname")
    async def addplayer(self, interaction: discord.Interaction, nickname: str) -> None:
        await interaction.response.defer()
        nickname = nickname.strip()
        existing = await self.store.find_player_by_nickname(nickname)
        if existing:
            await interaction.followup.send(f"**{existing.nickname}** is already on the roster.")
            return

        try:
            profile = await self.faceit.get_player_by_nickname(nickname)
        except FaceitNotFound:
            await interaction.followup.send(f"No FACEIT player named **{nickname}**.")
            return
        except FaceitError as exc:
            logger.exception("addplayer failed for %s", nickname)
            await interaction.followup.send(f"Could not look up **{nickname}**: {exc}")
            return

        already = await self.store.get_player(profile.player_id)
        if already:
            await interaction.followup.send(f"**{already.nickname}** is already on the roster.")
            return

        await self.store.add_player(profile.player_id, profile.nickname)
        await self.store.add_snapshot(profile.player_id, profile.elo, profile.level)
        if profile.elo == 0 and profile.level == 0:
            await interaction.followup.send(
                f"Tracking **{profile.nickname}** — still calibrating (no ELO yet)."
            )
        else:
            await interaction.followup.send(
                f"Tracking **{profile.nickname}** — {profile.elo} ELO, level {profile.level}."
            )

    @app_commands.command(name="removeplayer", description="Stop tracking a FACEIT player")
    @app_commands.describe(nickname="FACEIT nickname")
    async def removeplayer(self, interaction: discord.Interaction, nickname: str) -> None:
        await interaction.response.defer()
        player = await self.store.find_player_by_nickname(nickname.strip())
        if player is None:
            await interaction.followup.send(f"**{nickname}** is not on the roster.")
            return
        await self.store.remove_player(player.player_id)
        await interaction.followup.send(f"Removed **{player.nickname}**.")

    @app_commands.command(name="listplayers", description="List tracked FACEIT players")
    async def listplayers(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        players = await self.store.list_players()
        if not players:
            await interaction.followup.send("No players tracked yet. Use `/addplayer`.")
            return
        lines = [f"• **{player.nickname}**" for player in players]
        await interaction.followup.send("\n".join(lines)[:2000])

    @app_commands.command(name="setweekly", description="Set this channel for Sunday weekly recaps")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setweekly(self, interaction: discord.Interaction) -> None:
        await self._set_named_channel(interaction, SETTING_WEEKLY_CHANNEL_ID, "Sunday weekly recaps")

    @app_commands.command(name="setdaily", description="Set this channel for daily map breakdowns")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setdaily(self, interaction: discord.Interaction) -> None:
        await self._set_named_channel(interaction, SETTING_DAILY_CHANNEL_ID, "daily map breakdowns")

    async def _set_named_channel(
        self, interaction: discord.Interaction, key: str, label: str
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run this command in a text channel.", ephemeral=True
            )
            return
        await self.store.set_channel_id(interaction.channel.id, key)
        await interaction.response.send_message(
            f"{label.capitalize()} will go to {interaction.channel.mention}."
        )

    @setweekly.error
    async def setweekly_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await self._channel_perm_error(interaction, error)

    @setdaily.error
    async def setdaily_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await self._channel_perm_error(interaction, error)

    async def _channel_perm_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need **Manage Server** to set recap channels."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        raise error

    @app_commands.command(name="report", description="Post this Sunday–Saturday FACEIT week so far")
    async def report(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        tz = ZoneInfo(self.bot.settings.timezone)
        week_start = current_week_start(datetime.now(tz))
        weeks = await collect_week_stats(
            self.store, self.faceit, self.bot.settings.timezone, week_start
        )
        if not weeks:
            await interaction.followup.send("No players tracked yet. Use `/addplayer`.")
            return
        embed = build_week_embed(weeks, week_start=week_start, completed=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="last-map", description="Break down a player's last FACEIT matchmaking map")
    @app_commands.describe(nickname="FACEIT nickname")
    async def last_map(self, interaction: discord.Interaction, nickname: str) -> None:
        await interaction.response.defer()
        nickname = nickname.strip()
        player = await self.store.find_player_by_nickname(nickname)
        if player:
            player_id = player.player_id
            display = player.nickname
        else:
            try:
                profile = await self.faceit.get_player_by_nickname(nickname)
            except FaceitNotFound:
                await interaction.followup.send(f"No FACEIT player named **{nickname}**.")
                return
            except FaceitError as exc:
                await interaction.followup.send(f"Could not look up **{nickname}**: {exc}")
                return
            player_id = profile.player_id
            display = profile.nickname

        result = await last_map_for(self.faceit, player_id, display)
        await interaction.followup.send(embed=build_last_map_embed(result))

    @tasks.loop(time=time(hour=0, minute=0))
    async def midnight_snapshot(self) -> None:
        logger.info("Taking midnight FACEIT snapshots")
        await snapshot_all_players(self.store, self.faceit)

    @midnight_snapshot.before_loop
    async def before_midnight_snapshot(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=time(hour=23, minute=59))
    async def scheduled_posts(self) -> None:
        tz = ZoneInfo(self.bot.settings.timezone)
        now = datetime.now(tz)
        weekday = now.weekday()
        is_weekend = weekday >= 5
        at_weekday_daily = now.hour == 22 and now.minute == 45 and not is_weekend
        at_weekend_daily = now.hour == 23 and now.minute == 59 and is_weekend
        at_weekly = now.hour == 23 and now.minute == 59 and weekday == 6

        if at_weekday_daily or at_weekend_daily:
            await self._post_daily(now)
        if at_weekly:
            await self._post_weekly(now)

    @scheduled_posts.before_loop
    async def before_scheduled_posts(self) -> None:
        await self.bot.wait_until_ready()
        await self._resolve_named_channels()

    async def _post_weekly(self, now: datetime) -> None:
        channel = await self._get_text_channel(SETTING_WEEKLY_CHANNEL_ID)
        if channel is None:
            logger.warning("Skipping Sunday recap: #weekly-report not configured")
            return
        week_start = completed_week_start(now)
        logger.info("Posting weekly recap to #%s", channel.name)
        weeks = await collect_week_stats(
            self.store, self.faceit, self.bot.settings.timezone, week_start
        )
        if not weeks:
            await channel.send("No players tracked. Use `/addplayer`.")
            return
        await channel.send(embed=build_week_embed(weeks, week_start=week_start, completed=True))

    async def _post_daily(self, now: datetime) -> None:
        channel = await self._get_text_channel(SETTING_DAILY_CHANNEL_ID)
        if channel is None:
            logger.warning("Skipping daily breakdown: #daily-breakdown not configured")
            return
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        logger.info("Posting daily breakdown to #%s", channel.name)
        players = await collect_day_maps(
            self.store, self.faceit, self.bot.settings.timezone, day_start
        )
        await channel.send(embed=build_daily_embed(players, day=now))

    async def _get_text_channel(self, setting_key: str) -> discord.TextChannel | None:
        channel_id = await self.store.get_channel_id(setting_key)
        if channel_id is None:
            await self._resolve_named_channels()
            channel_id = await self.store.get_channel_id(setting_key)
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def _resolve_named_channels(self) -> None:
        guild_id = self.bot.settings.discord_guild_id
        if not guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id)
            except discord.HTTPException:
                return
        try:
            channels = await guild.fetch_channels()
        except discord.HTTPException:
            channels = guild.channels
        by_name = {
            channel.name: channel
            for channel in channels
            if isinstance(channel, discord.TextChannel)
        }
        weekly = await self.store.get_channel_id(SETTING_WEEKLY_CHANNEL_ID)
        daily = await self.store.get_channel_id(SETTING_DAILY_CHANNEL_ID)
        if weekly is None and WEEKLY_CHANNEL_NAME in by_name:
            await self.store.set_channel_id(by_name[WEEKLY_CHANNEL_NAME].id, SETTING_WEEKLY_CHANNEL_ID)
            logger.info("Bound weekly recap to #%s", WEEKLY_CHANNEL_NAME)
        if daily is None and DAILY_CHANNEL_NAME in by_name:
            await self.store.set_channel_id(by_name[DAILY_CHANNEL_NAME].id, SETTING_DAILY_CHANNEL_ID)
            logger.info("Bound daily breakdown to #%s", DAILY_CHANNEL_NAME)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TrackerCog(bot))
