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
    "Persona 5", "Persona 5 Royal", "Persona 5 Royale",
    "Persona 5 Royal Edition", "Persona 3 Reload", "Persona 3 Reloaded", "Hollow Knight: Silksong", "Hollow Knight", "League Of Legends"
]
DATA_FILE = "gabo_tiempo.json"
LOCAL_TZ = ZoneInfo("America/Santiago")

# === INTENTS ===
intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === VARIABLES EN MEMORIA ===
user_game_start = {}   # { user_id: {game, start: datetime} }
game_data = {}         # { game_name: {total_seconds, total_time_human, last_session, ...}, "active_sessions": {user_id: {...}} }
target_channel = None

# ---------- Utilidades de persistencia ----------
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
    # migración: asegurar clave de sesiones activas
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
    # guardar estado activo en disco
    game_data["active_sessions"][str(user_id)] = {
        "game": game_name,
        "start": start_dt.isoformat()
    }
    # inicializa contenedores del juego si no existen
    if game_name not in game_data or not isinstance(game_data.get(game_name), dict):
        game_data[game_name] = {"total_seconds": 0, "total_time_human": "0h 0min"}
    save_data()

def _clear_live_fields(game_name: str):
    # limpia los campos live_* si existen
    if game_name in game_data and isinstance(game_data[game_name], dict):
        game_data[game_name].pop("live_total_seconds", None)
        game_data[game_name].pop("live_total_time_human", None)
        game_data[game_name].pop("live_updated_at", None)

def end_session_apply(user_id: int, game_name: str, start_dt: datetime.datetime, end_dt: datetime.datetime):
    # sumar al acumulado y limpiar sesión activa
    duration = end_dt - start_dt
    add_sec = int(duration.total_seconds())
    base_total = 0
    if isinstance(game_data.get(game_name), dict):
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

def reconcile_on_boot(guild):
    """
    Al iniciar: si hay una sesión activa guardada y Gabo ya NO está jugando,
    cerramos la sesión usando la hora de arranque como fin.
    Si SÍ está jugando, mantenemos la sesión abierta (sin cerrar).
    """
    if not guild:
        return

    active = game_data.get("active_sessions", {}).get(str(TARGET_USER_ID))
    if not active:
        return

    async def _reconcile():
        nonlocal active
        try:
            gabo = guild.get_member(TARGET_USER_ID) or await guild.fetch_member(TARGET_USER_ID)
        except (NotFound, HTTPException):
            gabo = None

        # detectar si actualmente está jugando un título válido
        is_playing_now = False
        current_game_name = None
        if gabo and gabo.activities:
            for activity in gabo.activities:
                if isinstance(activity, (discord.Game, discord.Activity)):
                    if activity.name and any(n.lower() in activity.name.lower() for n in TARGET_GAMES):
                        is_playing_now = True
                        current_game_name = activity.name
                        break

        # parsear inicio guardado
        try:
            start_dt = datetime.datetime.fromisoformat(active["start"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
        except Exception:
            game_data["active_sessions"].pop(str(TARGET_USER_ID), None)
            save_data()
            return

        if is_playing_now:
            # mantener la sesión abierta y también en RAM
            user_game_start[TARGET_USER_ID] = {"game": active["game"], "start": start_dt}
            return

        # No está jugando ahora, cerramos la sesión pendiente hasta ahora
        end_dt = _now()
        duration, hours, minutes = end_session_apply(TARGET_USER_ID, active["game"], start_dt, end_dt)

        if target_channel:
            await target_channel.send(
                f"⏹️ **Gabo** dejó de ratear en **{active['game']}** (reconciliado tras reinicio).\n"
                f"🕒 Duración: **{duration}**\n"
                f"⌛ Total acumulado: **{hours}h {minutes}min**"
            )

    return _reconcile

# ---------- Cargar datos al importar ----------
load_data()

# === AL CONECTARSE ===
@bot.event
async def on_ready():
    global target_channel
    print(f"✅ Bot conectado como {bot.user}")

    # localizar canal objetivo
    for guild in bot.guilds:
        await guild.chunk()
        channel = discord.utils.find(lambda c: c.name == TARGET_CHANNEL_NAME, guild.text_channels)
        if channel:
            target_channel = channel
            # reconciliar aquí, por servidor
            reconcile_coro = reconcile_on_boot(guild)
            if reconcile_coro:
                await reconcile_coro()
            break

    if not target_channel:
        print(f"⚠️ No se encontró el canal '{TARGET_CHANNEL_NAME}'.")
    else:
        print(f"✅ Canal objetivo detectado: #{target_channel.name}")
        check_gabo_activity.start()
        flush_live_json.start()  # <-- arranca el refresco periódico del JSON

# === LOOP DE DETECCIÓN ===
@tasks.loop(seconds=15)
async def check_gabo_activity():
    if not target_channel:
        return  # No hay canal para enviar mensajes

    guild = bot.guilds[0]
    try:
        gabo = guild.get_member(TARGET_USER_ID) or await guild.fetch_member(TARGET_USER_ID)
    except (NotFound, HTTPException):
        print("⚠️ Gabo no fue encontrado.")
        return

    now = _now()

    # Buscar si está jugando uno de los títulos
    current_game = None
    for activity in getattr(gabo, "activities", []) or []:
        if isinstance(activity, (discord.Game, discord.Activity)):
            if activity.name and any(name.lower() in activity.name.lower() for name in TARGET_GAMES):
                current_game = activity.name
                break

    # === SI ESTÁ JUGANDO ===
    if current_game:
        if gabo.id not in user_game_start:
            # sesión nueva
            user_game_start[gabo.id] = {"game": current_game, "start": now}
            start_session_persist(gabo.id, current_game, now)
            print(f"▶️ {gabo.display_name} comenzó a jugar {current_game} a las {now.strftime('%H:%M:%S')}")
            await target_channel.send(f"🔥 **{gabo.display_name}** empezó a ratear en **{current_game}** 🎮🔥")
        else:
            # si cambió de juego en caliente, cerramos la anterior y abrimos nueva
            sess = user_game_start[gabo.id]
            if sess["game"] != current_game:
                duration, hours, minutes = end_session_apply(gabo.id, sess["game"], sess["start"], now)
                await target_channel.send(
                    f"⏹️ **{gabo.display_name}** dejó de ratear en **{sess['game']}**.\n"
                    f"🕒 Duración: **{duration}**\n"
                    f"⌛ Total acumulado: **{hours}h {minutes}min**"
                )
                # iniciar nueva
                user_game_start[gabo.id] = {"game": current_game, "start": now}
                start_session_persist(gabo.id, current_game, now)
                await target_channel.send(f"🔥 **{gabo.display_name}** empezó a ratear en **{current_game}** 🎮🔥")

    # === SI DEJÓ DE JUGAR ===
    elif gabo.id in user_game_start:
        session = user_game_start.pop(gabo.id)
        duration, hours, minutes = end_session_apply(gabo.id, session["game"], session["start"], now)
        print(f"⏹️ {gabo.display_name} dejó de jugar {session['game']}. Duración: {duration}")
        await target_channel.send(
            f"⏹️ **{gabo.display_name}** dejó de ratear en **{session['game']}**.\n"
            f"🕒 Duración: **{duration}**\n"
            f"⌛ Total acumulado: **{hours}h {minutes}min**"
        )

# === LOOP: refresco periódico del JSON con totales "en vivo" ===
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
        try:
            start_dt = datetime.datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
        except Exception:
            continue

        elapsed = int((now - start_dt).total_seconds())
        base_total = int(game_data.get(game, {}).get("total_seconds", 0))
        live_total = max(0, base_total + elapsed)
        live_human, _, _ = humanize_total(live_total)

        if game not in game_data or not isinstance(game_data.get(game), dict):
            game_data[game] = {}

        # Solo marcamos cambios si algo efectivamente cambió
        prev_live = game_data[game].get("live_total_seconds")
        if prev_live != live_total:
            game_data[game]["live_total_seconds"] = live_total
            game_data[game]["live_total_time_human"] = live_human
            game_data[game]["live_updated_at"] = now.isoformat()
            changed = True

    # Si no hay sesiones activas, opcionalmente podríamos limpiar live_* de todos los juegos
    # pero lo dejamos persistente hasta el próximo cierre.
    if changed:
        save_data()

# === COMANDO: !vertiempo ===
@bot.command()
async def vertiempo(ctx):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return

    load_data()  # recargar del disco por si otro proceso tocó el archivo
    data = game_data.copy()

    # construir mensaje
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

# === COMANDO: !tiemporeal ===
@bot.command()
async def tiemporeal(ctx):
    if ctx.channel.name != TARGET_CHANNEL_NAME:
        return

    if TARGET_USER_ID not in user_game_start:
        # si el proceso se reinició, intentar levantar desde JSON
        active = game_data.get("active_sessions", {}).get(str(TARGET_USER_ID))
        if active:
            try:
                start_dt = datetime.datetime.fromisoformat(active["start"])
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=LOCAL_TZ)
                user_game_start[TARGET_USER_ID] = {"game": active["game"], "start": start_dt}
            except Exception:
                pass

    if TARGET_USER_ID not in user_game_start:
        await ctx.send("⏳ Gabo no está rateando en este momento.")
        return

    session = user_game_start[TARGET_USER_ID]
    start_time = session["start"]
    game_name = session["game"]
    now = _now()
    duration = now - start_time

    # cuidado: duration.total_seconds() incluye días
    total_secs = int(duration.total_seconds())
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    seconds = total_secs % 60

    await ctx.send(
        f"🎮 **{game_name}** en curso:\n"
        f"🕒 Tiempo actual: **{hours}h {minutes}min {seconds}s**"
    )

# ---------- Señales para guardado seguro ----------
def handle_shutdown(signum, frame):
    # guardamos la sesión activa en JSON si aún no está reflejada
    for uid, sess in user_game_start.items():
        if str(uid) not in game_data.get("active_sessions", {}):
            game_data["active_sessions"][str(uid)] = {
                "game": sess["game"],
                "start": sess["start"].isoformat()
            }
    save_data()
    try:
        os.remove("bot.lock")
    except FileNotFoundError:
        pass
    os._exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# ---------- Guardián de instancia ----------
if os.path.exists("bot.lock"):
    exit()
else:
    open("bot.lock", "w").close()

# Servicio web keep-alive (opcional en Render)
keep_alive()

# Run bot
bot.run(TOKEN)

# limpieza al salir "normal"
try:
    os.remove("bot.lock")
except FileNotFoundError:
    pass
