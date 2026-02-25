import os
import hmac
import base64
import hashlib
import sqlite3
import requests

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
    await context.bot.send_message(
        chat_id=user.id,
        text="📌 Bienvenido al grupo VIP de Flanders y Fred - OKX. Envíame tu UID de OKX (solo números) para validar acceso."
    )

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    # BYPASS
    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

        await send_group_welcome(context, user)
        return

    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    resp = okx_affiliate_detail(text)

    # Solo validamos que exista y sea correcto
    if resp.get("code") != "0" or not resp.get("data"):
        await update.message.reply_text("UID no válido o no es referido.")
        return

    # Aprobamos acceso (sin mostrar volumen)
    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await update.message.reply_text("✔️ UID verificado correctamente. Acceso aprobado.")

    await send_group_welcome(context, user)

# ─────────────────────────────
# MENSAJE BIENVENIDA
# ─────────────────────────────
async def send_group_welcome(context, user):
    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=(
            f"🚀🔥 ¡BIENVENIDO {mention_html(user.id, user.first_name)}! 🔥🚀\n\n"
        "Has entrado oficialmente al *VIP de Flanders y Fred - OKX*.\n\n"
        "Aquí encontrarás bots exclusivos 🤖, tips de trading 📈 y beneficios especiales por formar parte de nuestra comunidad privada.\n\n"
        "Diversifica, participa y gana recomensas 💰\n\n"
        "¡Vamos con todo! 🚀🔥"
    ),
        parse_mode=ParseMode.HTML
    )

# ─────────────────────────────
# REPORTE MENSUAL SOLO ADMIN
# ─────────────────────────────
async def monthly_admin_report(context: ContextTypes.DEFAULT_TYPE):

    total_volumen = 0.0
    total_comisiones = 0.0

    # Aquí deberías iterar tus UIDs reales guardados si los tienes
    # Este ejemplo solo muestra estructura base

    report = (
        "📊 REPORTE MENSUAL KOL\n\n"
        f"Volumen total generado: {total_volumen:.0f} USDT\n"
        f"Comisiones estimadas: {total_comisiones:.2f} USDT"
    )

    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            chat_id=admin_id,
            text=report
        )

# ─────────────────────────────
# MAIN
# ─────────────────────────────
def main():
    defaults = Defaults(tzinfo=timezone.utc)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    # Reporte mensual (día 30, 00:05 UTC)
    app.job_queue.run_daily(
        monthly_admin_report,
        time=datetime.strptime("00:05", "%H:%M").time(),
        days=(0,1,2,3,4,5,6),
        name="monthly_admin_report"
    )

    print("🤖 BOT OKX PRO MAX iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()
