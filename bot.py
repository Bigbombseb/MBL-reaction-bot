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
