import discord
from discord.ext import tasks, commands
import datetime
import json
import os
from zoneinfo import ZoneInfo
from discord.errors import NotFound, HTTPException
from keep_alive import keep_alive
import time

# === CONFIGURACIÓN ===
TOKEN = os.environ.get("TOKEN")
TARGET_USER_ID = 369975308767461378  # <-- ID de Gabo
TARGET_CHANNEL_NAME = "gabo-persona-5"
TARGET_GAMES = ["Persona 5", "Persona 5 Royal", "Persona 5 Royale", "Persona 5 Royal Edition", "Persona 3 Reload", "Persona 3 Reloaded"]
DATA_FILE = "gabo_tiempo.json"
LOCAL_TZ = ZoneInfo("America/Santiago")


# === INTENTS ===
intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === VARIABLES ===
user_game_start = {}
game_data = {}
target_channel = None

# === CARGAR DATOS SI EXISTEN ===
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        game_data = json.load(f)

# === AL CONECTARSE ===
@bot.event
async def on_ready():
    global target_channel
    print(f"✅ Bot conectado como {bot.user}")

    for guild in bot.guilds:
        await guild.chunk()
        channel = discord.utils.find(lambda c: c.name == TARGET_CHANNEL_NAME, guild.text_channels)
        if channel:
            target_channel = channel
            break

    if not target_channel:
        print(f"⚠️ No se encontró el canal '{TARGET_CHANNEL_NAME}'.")
    else:
        print(f"✅ Canal objetivo detectado: #{target_channel.name}")
        check_gabo_activity.start()

# === LOOP DE DETECCIÓN ===
@tasks.loop(seconds=15)
async def check_gabo_activity():
    if not target_channel:
        return  # No hay canal para enviar mensajes

    guild = bot.guilds[0]
    gabo = guild.get_member(TARGET_USER_ID)
    now = datetime.datetime.now(LOCAL_TZ)

    if not gabo:
        try:
            gabo = await guild.fetch_member(TARGET_USER_ID)
        except (NotFound, HTTPException):
            print("⚠️ Gabo no fue encontrado.")
            return

    # Buscar si está jugando Persona 5
    current_game = None
    for activity in gabo.activities:
        if isinstance(activity, (discord.Game, discord.Activity)):
            if activity.name and any(name.lower() in activity.name.lower() for name in TARGET_GAMES):
                current_game = activity.name
                break

    # === SI ESTÁ JUGANDO ===
    if current_game:
        if gabo.id not in user_game_start:
            user_game_start[gabo.id] = {"game": current_game, "start": now}
            print(f"▶️ {gabo.display_name} comenzó a jugar {current_game} a las {now.strftime('%H:%M:%S')}")
            await target_channel.send(f"🔥 **{gabo.display_name}** empezó a ratear en **{current_game}** 🎮🔥")

    # === SI DEJÓ DE JUGAR ===
    elif gabo.id in user_game_start:
        session = user_game_start.pop(gabo.id)
        start_time = session["start"]
        game_name = session["game"]
        duration = now - start_time

        # Actualizar acumulado
        total_sec = game_data.get(game_name, {}).get("total_seconds", 0) + int(duration.total_seconds())
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60

        game_data[game_name] = {
            "total_seconds": total_sec,
            "total_time_human": f"{hours}h {minutes}min",
            "last_session": {
                "start": start_time.isoformat(),
                "end": now.isoformat(),
                "duration": str(duration)
            }
        }

        with open(DATA_FILE, "w") as f:
            json.dump(game_data, f, indent=4)

        print(f"⏹️ {gabo.display_name} dejó de jugar {game_name}. Duración: {duration}")
        await target_channel.send(
            f"⏹️ **{gabo.display_name}** dejó de ratear en **{game_name}**.\n"
            f"🕒 Duración: **{duration}**\n"
            f"⌛ Total acumulado: **{hours}h {minutes}min**"
        )

# === COMANDO: !vertiempo ===
@bot.command()
async def vertiempo(ctx):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return

    if not os.path.exists(DATA_FILE):
        await ctx.send("📊 Aún no hay registros de Gabo rateando.")
        return

    with open(DATA_FILE, "r") as f:
        data = json.load(f)

    if not data:
        await ctx.send("📊 Aún no hay registros de Gabo rateando.")
        return

    msg = "🎮 **Tiempo total de Gabo rateando:**\n"
    for game, info in data.items():
        msg += f"- **{game}**: {info['total_time_human']}\n"

    await ctx.send(msg)
    
# === COMANDO: !tiemporeal ===
@bot.command()
async def tiemporeal(ctx):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return

    if TARGET_USER_ID not in user_game_start:
        await ctx.send("⏳ Gabo no está rateando en este momento.")
        return

    session = user_game_start[TARGET_USER_ID]
    start_time = session["start"]
    game_name = session["game"]
    now = datetime.datetime.now(LOCAL_TZ)
    duration = now - start_time

    hours = duration.seconds // 3600
    minutes = (duration.seconds % 3600) // 60
    seconds = duration.seconds % 60

    await ctx.send(
        f"🎮 **{game_name}** en curso:\n"
        f"🕒 Tiempo actual: **{hours}h {minutes}min {seconds}s**"
    )

keep_alive()
bot.run(TOKEN)
