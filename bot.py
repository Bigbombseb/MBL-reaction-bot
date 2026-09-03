import os
import uuid
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.reactions = True
intents.message_content = True
intents.members = True  # needed to iterate role members for /reactping

bot = commands.Bot(command_prefix="!", intents=intents)

DEFAULT_TIME_SLOTS = ["8:00", "8:30", "9:00", "9:30", "10:00", "10:30", "11:00"]

STATUS_EMOJIS = {
    "✅": "Yes",
    "❌": "No",
    "❓": "Maybe",
}

# event_id -> {
#   "title": str,
#   "guild_id": int,
#   "channel_id": int,
#   "slots": {
#       time_label: {
#           "message_id": int,
#           "votes": {emoji: set(user_id)}
#       }
#   }
# }
active_events = {}

# message_id -> (event_id, time_label)   -- fast lookup for reaction events
message_index = {}

# guild_id -> role_id (the "roster")
rosters = {}

# guild_id -> most recent event_id
last_event = {}

# set of (message_id, emoji) where the bot currently holds a seed reaction
bot_seeded = set()

# guild_id -> list of "HH:MM" (24hr) time strings, custom per server
guild_times = {}

# guild_id -> UTC offset in hours (float, supports half-hour zones)
guild_timezones = {}


def get_time_slots(guild_id: int) -> list:
    return guild_times.get(guild_id, DEFAULT_TIME_SLOTS)


def get_timezone_offset(guild_id: int) -> float:
    return guild_timezones.get(guild_id, 0.0)


def parse_hhmm(value: str):
    """Parse an 'H:MM' or 'HH:MM' 24-hour string into (hour, minute), or None if invalid."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def build_timestamp(time_label: str, date_str: str, tz_offset: float) -> int:
    """Convert a time label + date string + UTC offset into a Unix timestamp."""
    hour, minute = parse_hhmm(time_label)
    year, month, day = (int(p) for p in date_str.split("-"))
    tz = timezone(timedelta(hours=tz_offset))
    dt = datetime(year, month, day, hour, minute, tzinfo=tz)
    return int(dt.timestamp())


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="setroster", description="Set the role used as the roster for /reactping")
@app_commands.describe(role="The role whose members count as the roster")
@app_commands.checks.has_permissions(manage_guild=True)
async def setroster(interaction: discord.Interaction, role: discord.Role):
    rosters[interaction.guild_id] = role.id
    await interaction.response.send_message(
        f"Roster set to {role.mention}. `/reactping` will check its members.",
        ephemeral=True,
    )


@bot.tree.command(name="settimes", description="Customize the time slots /rsvp uses")
@app_commands.describe(times="Comma-separated 24hr times, e.g. 18:00,18:30,19:00")
@app_commands.checks.has_permissions(manage_guild=True)
async def settimes(interaction: discord.Interaction, times: str):
    raw_slots = [t.strip() for t in times.split(",") if t.strip()]
    if not raw_slots:
        await interaction.response.send_message("Give at least one time.", ephemeral=True)
        return

    for slot in raw_slots:
        if parse_hhmm(slot) is None:
            await interaction.response.send_message(
                f"Couldn't parse `{slot}`. Use 24-hour HH:MM, e.g. `18:00,18:30,19:00`.",
                ephemeral=True,
            )
            return

    guild_times[interaction.guild_id] = raw_slots
    await interaction.response.send_message(
        f"Time slots updated: {', '.join(raw_slots)}", ephemeral=True
    )


@bot.tree.command(name="settimezone", description="Set the UTC offset used to interpret /rsvp times")
@app_commands.describe(offset="UTC offset in hours, e.g. -4 for EDT, -5 for EST, 5.5 for IST")
@app_commands.checks.has_permissions(manage_guild=True)
async def settimezone(interaction: discord.Interaction, offset: float):
    guild_timezones[interaction.guild_id] = offset
    await interaction.response.send_message(
        f"Timezone offset set to UTC{'+' if offset >= 0 else ''}{offset}. "
        f"`/rsvp` times will be interpreted in this offset.",
        ephemeral=True,
    )


@bot.tree.command(name="rsvp", description="Create an RSVP — one message per time slot")
@app_commands.describe(
    title="What are people RSVPing to?",
    date="Optional date as YYYY-MM-DD (defaults to today, in the server's set timezone)",
)
async def rsvp(interaction: discord.Interaction, title: str, date: str = None):
    tz_offset = get_timezone_offset(interaction.guild_id)

    if date is None:
        tz = timezone(timedelta(hours=tz_offset))
        date_str = datetime.now(tz).strftime("%Y-%m-%d")
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            await interaction.response.send_message(
                "Date must be in YYYY-MM-DD format.", ephemeral=True
            )
            return
        date_str = date

    time_slots = get_time_slots(interaction.guild_id)

    await interaction.response.send_message(f"Creating RSVP for **{title}**...", ephemeral=True)

    event_id = str(uuid.uuid4())
    slots = {}

    for time_label in time_slots:
        ts = build_timestamp(time_label, date_str, tz_offset)
        message = await interaction.channel.send(f"**{title} — <t:{ts}:t>**")
        for emoji in STATUS_EMOJIS:
            await message.add_reaction(emoji)
            bot_seeded.add((message.id, emoji))

        votes = {emoji: set() for emoji in STATUS_EMOJIS}
        slots[time_label] = {"message_id": message.id, "votes": votes}
        message_index[message.id] = (event_id, time_label)

    active_events[event_id] = {
        "title": title,
        "guild_id": interaction.guild_id,
        "channel_id": interaction.channel_id,
        "slots": slots,
    }
    last_event[interaction.guild_id] = event_id


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    lookup = message_index.get(payload.message_id)
    if not lookup:
        return
    event_id, time_label = lookup
    emoji = str(payload.emoji)
    if emoji not in STATUS_EMOJIS:
        return

    event = active_events[event_id]
    slot = event["slots"][time_label]
    slot["votes"][emoji].add(payload.user_id)

    # A real person just reacted with this emoji — remove the bot's own
    # seed reaction on this emoji so it stops inflating the count.
    key = (payload.message_id, emoji)
    if key in bot_seeded:
        channel = bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(emoji, bot.user)
        except discord.HTTPException:
            pass
        bot_seeded.discard(key)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    lookup = message_index.get(payload.message_id)
    if not lookup:
        return
    event_id, time_label = lookup
    emoji = str(payload.emoji)
    if emoji not in STATUS_EMOJIS:
        return

    event = active_events[event_id]
    slot = event["slots"][time_label]
    slot["votes"][emoji].discard(payload.user_id)

    # If that was the last real reaction on this emoji, the option would
    # vanish from the message entirely — re-add the bot's seed reaction
    # so people can still click it.
    key = (payload.message_id, emoji)
    if not slot["votes"][emoji] and key not in bot_seeded:
        channel = bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass
        bot_seeded.add(key)


@bot.tree.command(name="reactping", description="Ping roster members missing a reaction on any time slot")
async def reactping(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    event_id = last_event.get(guild_id)
    if event_id is None or event_id not in active_events:
        await interaction.response.send_message(
            "No RSVP found to check. Run `/rsvp` first.", ephemeral=True
        )
        return

    role_id = rosters.get(guild_id)
    if role_id is None:
        await interaction.response.send_message(
            "No roster set. Use `/setroster @role` first.", ephemeral=True
        )
        return

    role = interaction.guild.get_role(role_id)
    if role is None:
        await interaction.response.send_message(
            "Roster role not found (was it deleted?).", ephemeral=True
        )
        return

    event = active_events[event_id]

    # A member "reacted" to a time slot if they used ANY of the 3 status
    # emojis on that slot's message. They need to have reacted to EVERY slot.
    missing_people = []
    for member in role.members:
        if member.bot:
            continue
        reacted_all = True
        for time_label, slot in event["slots"].items():
            reacted_this_slot = any(
                member.id in slot["votes"][emoji] for emoji in STATUS_EMOJIS
            )
            if not reacted_this_slot:
                reacted_all = False
                break
        if not reacted_all:
            missing_people.append(member)

    if not missing_people:
        await interaction.response.send_message(
            f"Everyone in {role.mention} has responded to every time slot. ✅"
        )
        return

    mentions = " ".join(m.mention for m in missing_people)
    await interaction.response.send_message(
        f"⏰ Reminder for **{event['title']}** — you're missing a response on at least one time slot: {mentions}"
    )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)
import os
import uuid
import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.reactions = True
intents.message_content = True
intents.members = True  # needed to iterate role members for /reactping

bot = commands.Bot(command_prefix="!", intents=intents)

TIME_SLOTS = ["8:00", "8:30", "9:00", "9:30", "10:00", "10:30", "11:00"]

STATUS_EMOJIS = {
    "✅": "Yes",
    "❌": "No",
    "❓": "Maybe",
}

# event_id -> {
#   "title": str,
#   "guild_id": int,
#   "channel_id": int,
#   "slots": {
#       time_label: {
#           "message_id": int,
#           "votes": {emoji: set(user_id)}
#       }
#   }
# }
active_events = {}

# message_id -> (event_id, time_label)   -- fast lookup for reaction events
message_index = {}

# guild_id -> role_id (the "roster")
rosters = {}

# guild_id -> most recent event_id
last_event = {}

# set of (message_id, emoji) where the bot currently holds a seed reaction
bot_seeded = set()


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="setroster", description="Set the role used as the roster for /reactping")
@app_commands.describe(role="The role whose members count as the roster")
@app_commands.checks.has_permissions(manage_guild=True)
async def setroster(interaction: discord.Interaction, role: discord.Role):
    rosters[interaction.guild_id] = role.id
    await interaction.response.send_message(
        f"Roster set to {role.mention}. `/reactping` will check its members.",
        ephemeral=True,
    )


@bot.tree.command(name="rsvp", description="Create an RSVP — one message per time slot")
@app_commands.describe(title="What are people RSVPing to?")
async def rsvp(interaction: discord.Interaction, title: str):
    await interaction.response.send_message(f"Creating RSVP for **{title}**...", ephemeral=True)

    event_id = str(uuid.uuid4())
    slots = {}

    for time_label in TIME_SLOTS:
        message = await interaction.channel.send(f"**{title} — {time_label}**")
        for emoji in STATUS_EMOJIS:
            await message.add_reaction(emoji)
            bot_seeded.add((message.id, emoji))

        votes = {emoji: set() for emoji in STATUS_EMOJIS}
        slots[time_label] = {"message_id": message.id, "votes": votes}
        message_index[message.id] = (event_id, time_label)

    active_events[event_id] = {
        "title": title,
        "guild_id": interaction.guild_id,
        "channel_id": interaction.channel_id,
        "slots": slots,
    }
    last_event[interaction.guild_id] = event_id


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    lookup = message_index.get(payload.message_id)
    if not lookup:
        return
    event_id, time_label = lookup
    emoji = str(payload.emoji)
    if emoji not in STATUS_EMOJIS:
        return

    event = active_events[event_id]
    slot = event["slots"][time_label]
    slot["votes"][emoji].add(payload.user_id)

    # A real person just reacted with this emoji — remove the bot's own
    # seed reaction on this emoji so it stops inflating the count.
    key = (payload.message_id, emoji)
    if key in bot_seeded:
        channel = bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(emoji, bot.user)
        except discord.HTTPException:
            pass
        bot_seeded.discard(key)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    lookup = message_index.get(payload.message_id)
    if not lookup:
        return
    event_id, time_label = lookup
    emoji = str(payload.emoji)
    if emoji not in STATUS_EMOJIS:
        return

    event = active_events[event_id]
    slot = event["slots"][time_label]
    slot["votes"][emoji].discard(payload.user_id)

    # If that was the last real reaction on this emoji, the option would
    # vanish from the message entirely — re-add the bot's seed reaction
    # so people can still click it.
    key = (payload.message_id, emoji)
    if not slot["votes"][emoji] and key not in bot_seeded:
        channel = bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass
        bot_seeded.add(key)


@bot.tree.command(name="reactping", description="Ping roster members missing a reaction on any time slot")
async def reactping(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    event_id = last_event.get(guild_id)
    if event_id is None or event_id not in active_events:
        await interaction.response.send_message(
            "No RSVP found to check. Run `/rsvp` first.", ephemeral=True
        )
        return

    role_id = rosters.get(guild_id)
    if role_id is None:
        await interaction.response.send_message(
            "No roster set. Use `/setroster @role` first.", ephemeral=True
        )
        return

    role = interaction.guild.get_role(role_id)
    if role is None:
        await interaction.response.send_message(
            "Roster role not found (was it deleted?).", ephemeral=True
        )
        return

    event = active_events[event_id]

    # A member "reacted" to a time slot if they used ANY of the 3 status
    # emojis on that slot's message. They need to have reacted to EVERY slot.
    missing_people = []
    for member in role.members:
        if member.bot:
            continue
        reacted_all = True
        for time_label, slot in event["slots"].items():
            reacted_this_slot = any(
                member.id in slot["votes"][emoji] for emoji in STATUS_EMOJIS
            )
            if not reacted_this_slot:
                reacted_all = False
                break
        if not reacted_all:
            missing_people.append(member)

    if not missing_people:
        await interaction.response.send_message(
            f"Everyone in {role.mention} has responded to every time slot. ✅"
        )
        return

    mentions = " ".join(m.mention for m in missing_people)
    await interaction.response.send_message(
        f"⏰ Reminder for **{event['title']}** — you're missing a response on at least one time slot: {mentions}"
    )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)
