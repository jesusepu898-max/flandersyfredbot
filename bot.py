import os
import hmac
import base64
import hashlib
import requests

from datetime import datetime, timezone
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
# VARIABLES DE ENTORNO
# ─────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
VIP_CHAT_ID = int(os.environ["VIP_CHAT_ID"])

OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

BYPASS_CODE = os.environ.get("BYPASS_CODE", "00000000010101010")

# ─────────────────────────────
# OKX VALIDACIÓN UID
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
        "👋 Solicita acceso al grupo VIP y envíame tu UID de OKX por privado."
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text="📌 Bienvenido al grupo VIP de Flanders y Fred - OKX.\n\nEnvíame tu UID de OKX (solo números) para validar acceso."
        )
    except:
        # Si el usuario nunca inició conversación, Telegram bloquea
        pass

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    # BYPASS DIRECTO
    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)
        await send_group_welcome(context, user)
        await update.message.reply_text("✅ Acceso aprobado.")
        return

    # Validación numérica
    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    # Validación con OKX
    resp = okx_affiliate_detail(text)

    if resp.get("code") != "0" or not resp.get("data"):
        await update.message.reply_text("❌ UID no válido o no es referido.")
        return

    # Aprobación
    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await update.message.reply_text("✅ UID verificado correctamente. Acceso aprobado.")

    await send_group_welcome(context, user)

# ─────────────────────────────
# MENSAJE BIENVENIDA FOMO
# ─────────────────────────────
async def send_group_welcome(context, user):
    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=(
            f"🚀🔥 <b>¡Bienvenido {mention_html(user.id, user.first_name)}!</b> 🔥🚀\n\n"
            "🎉 Has entrado oficialmente al <b>Grupo VIP de Flanders y Fred - OKX</b>.\n\n"
            "Aquí encontrarás:\n"
            "🤖 Bots exclusivos\n"
            "📊 Tips avanzados de trading\n"
            "💎 Estrategias que no publicamos en ningún otro lugar\n"
            "🎁 Beneficios especiales por pertenecer a nuestra comunidad\n"
            "🧠 Soporte personalizado en OKX\n\n"
            "¡Saludos y a romper el mercado! 💥"
        ),
        parse_mode=ParseMode.HTML
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

    print("🤖 BOT FLANDERS Y FRED OKX VIP iniciado.")
    app.run_polling()

if __name__ == "__main__":
    main()
