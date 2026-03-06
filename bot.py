import os
import hmac
import base64
import hashlib
import sqlite3
import requests
import random

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.helpers import mention_html
from telegram.ext import (
    Application,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Defaults,
)

# ─────────────────────────────
# ENV
# ─────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
VIP_CHAT_ID = int(os.environ["VIP_CHAT_ID"])

OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

BYPASS_CODE = os.environ.get("BYPASS_CODE", "00000000010101010")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

# ─────────────────────────────
# DATABASE
# ─────────────────────────────
def db():
    conn = sqlite3.connect("flanders_bot.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL,
        joined_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()

def save_user(telegram_id, uid):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO users(telegram_id, uid, joined_at)
        VALUES(?,?,?)
    """, (telegram_id, uid, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def get_all_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, uid FROM users")
    rows = cur.fetchall()
    conn.close()
    return rows

# ─────────────────────────────
# OKX
# ─────────────────────────────
def get_okx_server_time_iso():
    r = requests.get("https://www.okx.com/api/v5/public/time", timeout=10)
    ts_ms = r.json()["data"][0]["ts"]
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def sign_okx(method, path, body=""):
    timestamp = get_okx_server_time_iso()
    message = timestamp + method + path + body
    mac = hmac.new(
        OKX_API_SECRET.encode(),
        msg=message.encode(),
        digestmod=hashlib.sha256
    )
    signature = base64.b64encode(mac.digest()).decode()
    return timestamp, signature

def okx_affiliate_detail(uid):
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    ts, signature = sign_okx("GET", path)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = "https://www.okx.com" + path
    return requests.get(url, headers=headers, timeout=15).json()

# ─────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Solicita el acceso al grupo VIP y envíame tu UID de OKX por privado."
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text="📌 Bienvenido al grupo VIP BOTS Flanders y Fred / OKX.\n\nEnvíame tu UID de OKX (solo números) para validar acceso."
        )
    except:
        pass

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)
        await send_welcome(context, user)
        return

    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    resp = okx_affiliate_detail(text)

    if resp.get("code") != "0" or not resp.get("data"):
        await update.message.reply_text("UID no válido o no es referido.")
        return

    save_user(user.id, text)

    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await update.message.reply_text("✔️ UID verificado correctamente. Acceso aprobado.")

    await send_welcome(context, user)

async def send_welcome(context, user):
    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=(
            f"🚀👋 Bienvenido {mention_html(user.id, user.first_name)} al grupo VIP BOTS Flanders y Fred / OKX.\n\n"
            "Aquí encontrarás bots exclusivos, tips de trading y beneficios por pertenecer a nuestra comunidad, "
            "además de soporte personalizado en OKX.\n\n"
            "🔥 Prepárate para aprovechar al máximo las oportunidades del mercado.\n\n"
            "¡Saludos!"
        ),
        parse_mode=ParseMode.HTML
    )

# ─────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        return

    users = get_all_users()

    texto = "📋 LISTA DE USUARIOS VIP\n\n"

    for u in users:
        texto += f"UID: {u['uid']} | TG: {u['telegram_id']}\n"

    await update.message.reply_text(texto)

async def sorteo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.from_user.id not in ADMIN_IDS:
        return

    users = get_all_users()

    if len(users) < 2:
        await update.message.reply_text("No hay suficientes usuarios para sorteo.")
        return

    winners = random.sample(users, 2)

    mensaje = "🎉 SORTEO VIP 🎉\n\n"

    for i, w in enumerate(winners, start=1):
        mensaje += f"{i}️⃣ UID: {w['uid']} | TG: {w['telegram_id']}\n"

    await update.message.reply_text(mensaje)

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.from_user.id not in ADMIN_IDS:
        return

    ranking = []

    for user in get_all_users():
        resp = okx_affiliate_detail(user["uid"])

        if resp.get("code") == "0" and resp.get("data"):
            vol = float(resp["data"][0].get("volMonth") or 0)

            ranking.append((user["uid"], vol))

    ranking.sort(key=lambda x: x[1], reverse=True)

    mensaje = "🏆 TOP VOLUMEN DEL MES\n\n"

    for i, r in enumerate(ranking[:10], start=1):
        mensaje += f"{i}. UID {r[0]} — {r[1]:.0f} USDT\n"

    await update.message.reply_text(mensaje)

# ─────────────────────────────
# REPORTES ADMIN
# ─────────────────────────────
async def weekly_admin_report(context: ContextTypes.DEFAULT_TYPE):
    await generate_admin_report(context, "📊 REPORTE SEMANAL ADMIN")

async def monthly_admin_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ_AR)
    if now.day != 30:
        return
    await generate_admin_report(context, "📊 REPORTE MENSUAL ADMIN")

async def generate_admin_report(context, title):

    total_volumen = 0.0
    usuarios = get_all_users()

    for u in usuarios:
        resp = okx_affiliate_detail(u["uid"])

        if resp.get("code") == "0" and resp.get("data"):
            vol = float(resp["data"][0].get("volMonth") or 0)
            total_volumen += vol

    texto = (
        f"{title}\n\n"
        f"Usuarios activos: {len(usuarios)}\n"
        f"Volumen acumulado del mes: {total_volumen:.0f} USDT"
    )

    for admin in ADMIN_IDS:
        await context.bot.send_message(chat_id=admin, text=texto)

# ─────────────────────────────
# MAIN
# ─────────────────────────────
def main():

    init_db()

    defaults = Defaults(tzinfo=timezone.utc)

    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("sorteo", sorteo))
    app.add_handler(CommandHandler("top", top))

    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    app.job_queue.run_daily(
        weekly_admin_report,
        time=datetime.strptime("00:00", "%H:%M").time(),
        days=(6,),
        name="weekly_report"
    )

    app.job_queue.run_daily(
        monthly_admin_report,
        time=datetime.strptime("00:05", "%H:%M").time(),
        days=(0,1,2,3,4,5,6),
        name="monthly_admin_report"
    )

    print("🤖 BOT FLANDERS iniciado.")

    app.run_polling()

if __name__ == "__main__":
    main()
