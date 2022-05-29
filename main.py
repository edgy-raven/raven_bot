import argparse
import dataclasses
from datetime import datetime, timedelta
import json
import sqlalchemy
from sqlalchemy import Column, Integer
from sqlalchemy.orm import (
    declarative_base, make_transient, make_transient_to_detached, sessionmaker
)
from typing import List, Optional, Union
import re

import nextcord
from nextcord.ext import commands

parser = argparse.ArgumentParser()
parser.add_argument("--prod", dest="prod", action="store_true")
parser.set_defaults(prod=False)

raven_bot = commands.Bot(command_prefix=commands.when_mentioned_or())

Base = declarative_base()
raven_sessionmaker = None


# Code is a bit hacky for now as we need to divide each section into its
# individual modules.
# ========== Debug ==========
@raven_bot.command()
async def echo(ctx, msg):
    await ctx.send(f"```{msg}```")

# ========== Guild metadata ==========
class GuildConfig(Base):
    __tablename__ = "guild_config"
    registry = {}

    guild_id = Column(Integer, primary_key=True)
    host_role_id = Column(Integer, nullable=True)
    queue_channel = Column(Integer, nullable=True)
    default_queue_size = Column(Integer, nullable=True)

    @classmethod
    def channel_check(cls, ctx):
        cfg = cls.registry[ctx.guild.id]
        return cfg.queue_channel == ctx.message.channel.id

    @classmethod
    def host_check(cls, ctx):
        cfg = cls.registry[ctx.guild.id]
        return cfg.host_role_id is None or any(
            r.id == cfg.host_role_id for r in ctx.author.roles)

    def configure(self, ctx, key, value):
        if key not in ("host_role_id", "queue_channel", "default_queue_size"):
            return f"Invalid configuration setting {key}."
        
        # TODO: This should be done with SQLAlchemy types.
        if key == "queue_channel":
            if not isinstance(value, nextcord.TextChannel):
                return "Invalid argument. Required a channel, e.g., #general."
            permissions = value.permissions_for(ctx.me)
            if not permissions.send_messages and permissions.read_messages:
                return f"Bot does not have permissions for: {channel.mention}"
            self.queue_channel = value.id
            return 
        elif key == "host_role_id":
            if value is not None or not isinstance(value, nextcord.Role):
                return "Invalid argument. Required a role, e.g., @Members."
            if value is not None:
                value = value.id
            self.host_role_id = value
        elif key == "default_queue_size":
            if not isinstance(value, int):
                return "Invalid argument. Required a number, e.g., 8."
            self.default_queue_size = value


@raven_bot.command()
@commands.has_permissions(manage_guild=True)
async def configure(
    ctx, 
    key: str, 
    value: Union[nextcord.TextChannel, nextcord.Role, int, None] = None
):
    msg = None
    with raven_sessionmaker() as session:
        if ctx.guild.id not in GuildConfig.registry:
            obj = GuildConfig(guild_id=ctx.guild.id)
        else:
            obj = session.merge(GuildConfig.registry[ctx.guild.id])
        msg = obj.configure(ctx, key, value)
        session.commit()
    if msg:
        await ctx.send(msg)


# ========== Lobby management ==========
# For now, let's assume each guild only has one lobby.
@dataclasses.dataclass
class QueueJoin:
    member_id : int
    end_time : datetime


@dataclasses.dataclass
class LobbyState:
    registry = {}
    
    guild_id : int
    queue_size : int    
    start_time : datetime
    end_time : datetime
    queue : List[int]

    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue_size = GuildConfig.registry[guild_id].default_queue_size or 8
        self.start_time = datetime.now()
        # TODO: allow custom end time setting.
        self.end_time = self.start_time + timedelta(days=1)
        self.queue = []

    def clean_up(self):
        now = datetime.now()
        if now > self.end_time:
            return True
        self.queue = [qj for qj in self.queue if now < qj.end_time]
        return False

    @classmethod
    def lobby_exists(cls, ctx):
        state = cls.registry.get(ctx.guild.id)
        if state is not None and state.clean_up():
            cls.registry.pop(ctx.guild.id)
        return ctx.guild.id in cls.registry

    @classmethod
    def lobby_not_exists(cls, ctx):
        return not cls.lobby_exists(ctx)


@raven_bot.command()
@commands.check(GuildConfig.channel_check)
@commands.check(GuildConfig.host_check)
@commands.check(LobbyState.lobby_not_exists)
async def host(ctx):
    LobbyState.registry[ctx.guild.id] = LobbyState(guild_id=ctx.guild.id)
    await ctx.send("Lobby created!") 


@raven_bot.command()
@commands.check(GuildConfig.channel_check)
@commands.check(LobbyState.lobby_exists)
async def join(ctx):
    lobby_state = LobbyState.registry[ctx.guild.id]
    # TODO: give user ability to specify how long they want to join queue for.
    end_time = datetime.now() + timedelta(hours=2)
    # TODO: give configurations ability to allow multiple joins.
    try:
        qj = next(
            qj for qj in lobby_state.queue if qj.member_id == ctx.author.id)
        qj.end_time = end_time
        return await ctx.send("You are already part of the queue!")
    except StopIteration:
        pass

    lobby_state.queue.append(
        QueueJoin(member_id=ctx.author.id, end_time=end_time))
    await ctx.send("You have successfully joined the queue!")
    if len(lobby_state.queue) == lobby_state.queue_size:
        await ctx.send(
            ", ".join(f"<@{qj.member_id}>" for qj in lobby_state.queue) +
            " Full lobby. Start game!"
        )
        LobbyState.registry.pop(ctx.guild.id)

@raven_bot.command()
@commands.check(GuildConfig.channel_check)
@commands.check(LobbyState.lobby_exists)
async def leave(ctx):
    lobby_state = LobbyState.registry[ctx.guild.id]
    lobby_state.queue = [
        qj for qj in lobby_state.queue if qj.member_id != ctx.author.id]
    await ctx.send("You are no longer part of the queue!")

@raven_bot.command()
@commands.check(GuildConfig.channel_check)
@commands.check(LobbyState.lobby_exists)
async def query(ctx):
    lobby_state = LobbyState.registry[ctx.guild.id]
    await ctx.send(
        f"Currently there are {len(lobby_state.queue)} players in the lobby.")


# ========== Resource setup ==========
if __name__ == "__main__":
    with open("keyring.json") as keyring_file:
        keyring = json.load(keyring_file)
    options = parser.parse_args()

    # Development configurations
    @raven_bot.check
    def prod_server_check(ctx):
        return ctx.guild and (
            not options.prod and ctx.guild.id == keyring["dev_guild_id"]
        )
    if options.prod:
        raise NotImplementedError("TODO -- setup db on server computer")
    else:
        engine = sqlalchemy.create_engine(
            "sqlite:///raven_dev.db", echo=True, future=True)
        raven_sessionmaker = sessionmaker(engine)
        Base.metadata.create_all(bind=engine)
    with raven_sessionmaker() as session:
        guild_configs = session.query(GuildConfig).all()
        GuildConfig.registry = {gc.guild_id : gc for gc in guild_configs}

    raven_bot.run(keyring["discord_api_token"])