from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
import yaml
from discord.ext import commands

from bot.config import Settings, load_settings
from bot.db import SETTING_DAILY_CHANNEL_ID, SETTING_WEEKLY_CHANNEL_ID, Store
from bot.faceit import FaceitClient, FaceitError, FaceitNotFound

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class TrackerBot(commands.Bot):
    def __init__(self, settings: Settings, store: Store, faceit: FaceitClient) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.store = store
        self.faceit = faceit

    async def setup_hook(self) -> None:
        await self.load_extension("bot.cogs.tracker")
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %s guild commands", len(synced))
            except discord.Forbidden:
                logger.warning(
                    "Bot is not in the server yet, or was invited without the "
                    "applications.commands scope. Invite it, then commands will sync on join."
                )
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global commands", len(synced))

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self.settings.discord_guild_id and guild.id != self.settings.discord_guild_id:
            return
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %s commands after joining %s", len(synced), guild.name)
        except discord.HTTPException:
            logger.exception("Failed to sync commands after joining %s", guild.name)

    async def close(self) -> None:
        await self.faceit.close()
        await self.store.close()
        await super().close()


def _seed_nicknames(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if isinstance(data, list):
        nicknames = data
    elif isinstance(data, dict):
        nicknames = data.get("players") or []
    else:
        return []
    return [str(nick).strip() for nick in nicknames if str(nick).strip()]


async def seed_players(store: Store, faceit: FaceitClient, players_file: Path) -> None:
    nicknames = _seed_nicknames(players_file)
    if not nicknames:
        return

    for nickname in nicknames:
        existing = await store.find_player_by_nickname(nickname)
        if existing:
            continue
        try:
            profile = await faceit.get_player_by_nickname(nickname)
        except FaceitNotFound:
            logger.warning("Seed skipped, FACEIT nick not found: %s", nickname)
            continue
        except FaceitError:
            logger.exception("Seed failed for %s", nickname)
            continue

        if await store.get_player(profile.player_id):
            continue
        await store.add_player(profile.player_id, profile.nickname)
        await store.add_snapshot(profile.player_id, profile.elo, profile.level)
        logger.info("Seeded %s (%s ELO, lvl %s)", profile.nickname, profile.elo, profile.level)
        await asyncio.sleep(0.2)


async def run() -> None:
    settings = load_settings()
    store = Store(settings.db_path)
    await store.connect()

    if settings.weekly_channel_id and await store.get_channel_id(SETTING_WEEKLY_CHANNEL_ID) is None:
        await store.set_channel_id(settings.weekly_channel_id, SETTING_WEEKLY_CHANNEL_ID)
    if settings.daily_channel_id and await store.get_channel_id(SETTING_DAILY_CHANNEL_ID) is None:
        await store.set_channel_id(settings.daily_channel_id, SETTING_DAILY_CHANNEL_ID)

    faceit = FaceitClient(settings.faceit_api_key, game_id=settings.game_id)
    try:
        await seed_players(store, faceit, settings.players_file)
    except Exception:
        await faceit.close()
        await store.close()
        raise

    bot = TrackerBot(settings, store, faceit)
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
