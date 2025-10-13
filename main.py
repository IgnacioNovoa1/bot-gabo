import discord
from discord.ext import tasks, commands
import datetime
import json
import os
import signal
from zoneinfo import ZoneInfo
from discord.errors import NotFound, HTTPException
from keep_alive import keep_alive
import time

# === CONFIGURACIÓN ===
TOKEN = os.environ.get("TOKEN")
TARGET_USER_ID = 369975308767461378  # <-- ID de Gabo
TARGET_CHANNEL_NAME = "gabo-persona-5"
TARGET_GAMES = [
    "Persona 5", "Persona 5 Royal", "Persona 5 Royale", "Persona 5 Royal Edition",
    "Persona 3 Reload", "Persona 3 Reloaded", "Hollow Knight: Silksong", "Hollow Knight",
    "Elden Ring", "Baldur's Gate 3", "Hades", "Hades II",
    "Stardew Valley", "Minecraft", "Terraria", "The Witcher 3: Wild Hunt",
    "Ori and the Will of the Wisps", "Celeste", "Rocket League", "Fortnite",
    "Apex Legends", "Counter-Strike 2", "VALORANT",
    "Overwatch 2", "Genshin Impact", "Starfield", "Cyberpunk 2077"
]
DATA_FILE = "gabo_tiempo.json"
LOCAL_TZ = ZoneInfo("America/Santiago")

# === INTENTS ===
intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

user_game_start = {}
game_data = {}
target_channel = None


def _now():
    return datetime.datetime.now(LOCAL_TZ)

def load_data():
    global game_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                game_data = json.load(f)
        except Exception:
            game_data = {}
    else:
        game_data = {}
    if "active_sessions" not in game_data or not isinstance(game_data["active_sessions"], dict):
        game_data["active_sessions"] = {}

def save_data():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(game_data, f, indent=4)
    os.replace(tmp, DATA_FILE)

def humanize_total(total_sec: int):
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    return f"{hours}h {minutes}min", hours, minutes


def start_session_persist(user_id: int, game_name: str, start_dt: datetime.datetime):
    game_data["active_sessions"][str(user_id)] = {
        "game": game_name,
        "start": start_dt.isoformat()
    }
    if game_name not in game_data or not isinstance(game_data.get(game_name), dict):
        game_data[game_name] = {"total_seconds": 0, "total_time_human": "0h 0min"}
    save_data()


def _clear_live_fields(game_name: str):
    if game_name in game_data and isinstance(game_data[game_name], dict):
        game_data[game_name].pop("live_total_seconds", None)
        game_data[game_name].pop("live_total_time_human", None)
        game_data[game_name].pop("live_updated_at", None)


def end_session_apply(user_id: int, game_name: str, start_dt: datetime.datetime, end_dt: datetime.datetime):
    duration = end_dt - start_dt
    add_sec = int(duration.total_seconds())
    base_total = int(game_data.get(game_name, {}).get("total_seconds", 0))
    total_sec = base_total + add_sec
    total_human, hours, minutes = humanize_total(total_sec)
    game_data[game_name] = {
        **game_data.get(game_name, {}),
        "total_seconds": total_sec,
        "total_time_human": total_human,
        "last_session": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "duration": str(duration)
        }
    }
    _clear_live_fields(game_name)
    game_data["active_sessions"].pop(str(user_id), None)
    save_data()
    return duration, hours, minutes


load_data()

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
        flush_live_json.start()


@tasks.loop(seconds=15)
async def check_gabo_activity():
    if not target_channel:
        return
    guild = bot.guilds[0]
    try:
        gabo = guild.get_member(TARGET_USER_ID) or await guild.fetch_member(TARGET_USER_ID)
    except (NotFound, HTTPException):
        return
    now = _now()
    current_game = None
    for activity in getattr(gabo, "activities", []) or []:
        if isinstance(activity, (discord.Game, discord.Activity)):
            if activity.name and any(name.lower() in activity.name.lower() for name in TARGET_GAMES):
                current_game = activity.name
                break

    if current_game:
        if gabo.id not in user_game_start:
            user_game_start[gabo.id] = {"game": current_game, "start": now}
            start_session_persist(gabo.id, current_game, now)
            await target_channel.send(f"🔥 **{gabo.display_name}** empezó a ratear en **{current_game}** 🎮🔥")
        else:
            sess = user_game_start[gabo.id]
            if sess["game"] != current_game:
                duration, hours, minutes = end_session_apply(gabo.id, sess["game"], sess["start"], now)
                await target_channel.send(
                    f"⏹️ **{gabo.display_name}** dejó de ratear en **{sess['game']}**.\n"
                    f"🕒 Duración: **{duration}**\n"
                    f"⌛ Total acumulado: **{hours}h {minutes}min**"
                )
                user_game_start[gabo.id] = {"game": current_game, "start": now}
                start_session_persist(gabo.id, current_game, now)
    elif gabo.id in user_game_start:
        session = user_game_start.pop(gabo.id)
        duration, hours, minutes = end_session_apply(gabo.id, session["game"], session["start"], now)
        await target_channel.send(
            f"⏹️ **{gabo.display_name}** dejó de ratear en **{session['game']}**.\n"
            f"🕒 Duración: **{duration}**\n"
            f"⌛ Total acumulado: **{hours}h {minutes}min**"
        )


@tasks.loop(seconds=60)
async def flush_live_json():
    now = _now()
    active_map = game_data.get("active_sessions", {})
    changed = False
    for uid_str, sess in list(active_map.items()):
        game = sess.get("game")
        start_iso = sess.get("start")
        if not game or not start_iso:
            continue
        start_dt = datetime.datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
        elapsed = int((now - start_dt).total_seconds())
        base_total = int(game_data.get(game, {}).get("total_seconds", 0))
        live_total = max(0, base_total + elapsed)
        live_human, _, _ = humanize_total(live_total)
        if game not in game_data:
            game_data[game] = {}
        prev_live = game_data[game].get("live_total_seconds")
        if prev_live != live_total:
            game_data[game]["live_total_seconds"] = live_total
            game_data[game]["live_total_time_human"] = live_human
            game_data[game]["live_updated_at"] = now.isoformat()
            changed = True
    if changed:
        save_data()


@bot.command(aliases=["horas", "tiempos"])
async def vertiempo(ctx):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return
    load_data()
    data = game_data.copy()
    payload = {k: v for k, v in data.items() if k not in ("active_sessions",)}
    if not payload:
        await ctx.send("📊 Aún no hay registros de Gabo rateando.")
        return
    msg = "🎮 **Tiempo total de Gabo rateando:**\n"
    for game, info in payload.items():
        if isinstance(info, dict):
            human = info.get("live_total_time_human") or info.get("total_time_human")
            if human:
                msg += f"- **{game}**: {human}\n"
    await ctx.send(msg)


@bot.command(aliases=["tiempo"])
async def tiemporeal(ctx):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return
    if TARGET_USER_ID not in user_game_start:
        active = game_data.get("active_sessions", {}).get(str(TARGET_USER_ID))
        if active:
            start_dt = datetime.datetime.fromisoformat(active["start"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
            user_game_start[TARGET_USER_ID] = {"game": active["game"], "start": start_dt}
    if TARGET_USER_ID not in user_game_start:
        await ctx.send("⏳ Gabo no está rateando en este momento.")
        return
    session = user_game_start[TARGET_USER_ID]
    start_time = session["start"]
    game_name = session["game"]
    now = _now()
    duration = now - start_time
    total_secs = int(duration.total_seconds())
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    seconds = total_secs % 60
    await ctx.send(f"🎮 **{game_name}** en curso:\n🕒 Tiempo actual: **{hours}h {minutes}min {seconds}s**")


@bot.command(aliases=["horasjuego", "hj"])
async def juego(ctx, *, nombre: str = None):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return
    if not nombre:
        await ctx.send("Uso: `!juego <nombre>` Ej: `!juego Hollow Knight: Silksong`")
        return
    load_data()
    target = None
    for k in game_data.keys():
        if k == "active_sessions":
            continue
        if k.lower() == nombre.lower() or nombre.lower() in k.lower():
            target = k
            break
    if not target:
        await ctx.send(f"No tengo registros para **{nombre}** todavía.")
        return
    info = game_data.get(target, {})
    total = info.get("total_time_human", "0h 0min")
    live = ""
    if "live_total_time_human" in info:
        live = f" (+{info['live_total_time_human']} en curso)"
    await ctx.send(f"🎮 **{target}**: {total}{live}")


keep_alive()
bot.run(TOKEN)
