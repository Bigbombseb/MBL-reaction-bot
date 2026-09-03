import os
import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.reactions = True
intents.message_content = True
intents.members = True  # needed to iterate role members for /reactping

bot = commands.Bot(command_prefix="!", intents=intents)

TIME_SLOTS = {
    "🕗": "8:00",
    "🕣": "8:30",
    "🕘": "9:00",
    "🕤": "9:30",
    "🕙": "10:00",
    "🕥": "10:30",
    "🕚": "11:00",
}

# message_id -> {"title": str, "votes": {emoji: set(user_ids)}, "channel_id": int}
active_rsvps = {}

# guild_id -> role_id (the "roster")
rosters = {}

# guild_id -> most recent rsvp message_id (for default targeting)
last_rsvp = {}


def build_embed(title: str, votes: dict) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    for emoji, label in TIME_SLOTS.items():
        names = votes.get(emoji, set())
        value = "\n".join(f"<@{uid}>" for uid in names) if names else "—"
        embed.add_field(name=f"{emoji} {label} ({len(names)})", value=value, inline=True)
    return embed


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


@bot.tree.command(name="rsvp", description="Create an RSVP for a time slot")
@app_commands.describe(title="What are people RSVPing to?")
async def rsvp(interaction: discord.Interaction, title: str):
    votes = {emoji: set() for emoji in TIME_SLOTS}
    embed = build_embed(title, votes)

    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()

    for emoji in TIME_SLOTS:
        await message.add_reaction(emoji)

    active_rsvps[message.id] = {
        "title": title,
        "votes": votes,
        "channel_id": interaction.channel_id,
    }
    last_rsvp[interaction.guild_id] = message.id


async def update_rsvp_message(message_id, channel):
    data = active_rsvps.get(message_id)
    if not data:
        return
    message = await channel.fetch_message(message_id)
    embed = build_embed(data["title"], data["votes"])
    await message.edit(embed=embed)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    data = active_rsvps.get(payload.message_id)
    if not data:
        return
    emoji = str(payload.emoji)
    if emoji not in TIME_SLOTS:
        return

    data["votes"][emoji].add(payload.user_id)

    channel = bot.get_channel(payload.channel_id)
    await update_rsvp_message(payload.message_id, channel)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    data = active_rsvps.get(payload.message_id)
    if not data:
        return
    emoji = str(payload.emoji)
    if emoji not in TIME_SLOTS:
        return

    data["votes"][emoji].discard(payload.user_id)

    channel = bot.get_channel(payload.channel_id)
    await update_rsvp_message(payload.message_id, channel)


@bot.tree.command(name="reactping", description="Ping roster members missing reactions on an RSVP")
@app_commands.describe(message_id="Optional: specific RSVP message ID (defaults to the most recent one)")
async def reactping(interaction: discord.Interaction, message_id: str = None):
    guild_id = interaction.guild_id

    # Resolve which RSVP message to check
    target_id = int(message_id) if message_id else last_rsvp.get(guild_id)
    if target_id is None or target_id not in active_rsvps:
        await interaction.response.send_message(
            "No RSVP found to check. Run `/rsvp` first or pass a valid message ID.",
            ephemeral=True,
        )
        return

    # Resolve the roster role
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

    data = active_rsvps[target_id]
    votes = data["votes"]

    # Anyone who hasn't reacted to ALL 7 time slots gets pinged
    missing_people = []
    for member in role.members:
        if member.bot:
            continue
        reacted_all = all(member.id in votes[emoji] for emoji in TIME_SLOTS)
        if not reacted_all:
            missing_people.append(member)

    if not missing_people:
        await interaction.response.send_message(
            f"Everyone in {role.mention} has reacted to all time slots. ✅"
        )
        return

    mentions = " ".join(m.mention for m in missing_people)
    await interaction.response.send_message(
        f"⏰ Reminder for **{data['title']}** — you're missing at least one time slot: {mentions}"
    )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)
