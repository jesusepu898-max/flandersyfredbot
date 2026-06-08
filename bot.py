import os
import csv
import hmac
import json
import time
import base64
import hashlib
import sqlite3
import requests
import tempfile
import random

from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
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
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

# En Render usar:
# DB_PATH=/var/data/flanders_fred_bot.db
DB_PATH = os.environ.get("DB_PATH", "/var/data/flanders_fred_bot.db")

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")
GROUP_NAME = "Comunidad Flanders y Fred VIP by OKX"
OKX_BASE_URL = "https://www.okx.com"
INSTRUMENTS_CACHE = {}


# ─────────────────────────────
# UTILS
# ─────────────────────────────
def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def money(value):
    try:
        return f"{float(value or 0):,.2f}"
    except Exception:
        return "0.00"


def number(value):
    try:
        return f"{float(value or 0):,.0f}"
    except Exception:
        return "0"


def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def split_uids(raw_text: str):
    raw_text = raw_text.replace(",", " ").replace("\n", " ").replace(";", " ")
    return [x.strip() for x in raw_text.split() if x.strip().isnumeric()]


def ts_to_human(value):
    if value in [None, ""]:
        return ""

    value = str(value).strip()

    if not value:
        return ""

    try:
        if value.isdigit() and len(value) >= 12:
            dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
            return dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S")

        if value.isdigit() and len(value) == 10:
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
            return dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    return value


def format_optional_usdt(value):
    if value is None or value == "":
        return "No disponible"
    return f"{money(value)} USDT"


# ─────────────────────────────
# DATABASE
# ─────────────────────────────
def db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL,
        first_name TEXT,
        username TEXT,
        joined_at TEXT NOT NULL,
        last_vol_month REAL DEFAULT 0,
        last_checked_at TEXT
    )
    """)

    cur.execute("PRAGMA table_info(users)")
    existing_columns = [row["name"] for row in cur.fetchall()]

    if "first_name" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN first_name TEXT")

    if "username" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN username TEXT")

    if "last_vol_month" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN last_vol_month REAL DEFAULT 0")

    if "last_checked_at" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN last_checked_at TEXT")

    conn.commit()
    conn.close()

    print(f"✅ DB inicializada en: {DB_PATH}")


def save_user(telegram_id, uid, first_name=None, username=None, last_vol_month=0):
    conn = db()
    cur = conn.cursor()

    now = now_utc_iso()

    cur.execute("""
        INSERT OR REPLACE INTO users (
            telegram_id,
            uid,
            first_name,
            username,
            joined_at,
            last_vol_month,
            last_checked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_id,
        uid,
        first_name,
        username,
        now,
        float(last_vol_month or 0),
        now
    ))

    conn.commit()
    conn.close()

    print(f"✅ Usuario guardado: TG={telegram_id} UID={uid} VOL={last_vol_month}")


def update_user_volume_by_uid(uid, volume):
    conn = db()
    cur = conn.cursor()

    now = now_utc_iso()

    cur.execute("""
        UPDATE users
        SET last_vol_month = ?, last_checked_at = ?
        WHERE uid = ?
    """, (float(volume or 0), now, uid))

    conn.commit()
    conn.close()


def get_user_by_telegram_id(telegram_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    conn.close()
    return row


def get_user_by_uid(uid):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE uid = ?", (uid,))
    row = cur.fetchone()

    conn.close()
    return row


def get_all_users():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT telegram_id, uid, first_name, username, joined_at, last_vol_month, last_checked_at
        FROM users
        ORDER BY joined_at ASC
    """)
    rows = cur.fetchall()

    conn.close()
    return rows


# ─────────────────────────────
# OKX BASE
# ─────────────────────────────
def get_okx_server_time_iso():
    r = requests.get(f"{OKX_BASE_URL}/api/v5/public/time", timeout=10)
    r.raise_for_status()

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


def okx_get(path):
    ts, signature = sign_okx("GET", path)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = OKX_BASE_URL + path
    return requests.get(url, headers=headers, timeout=20).json()


def okx_public_get(path):
    url = OKX_BASE_URL + path
    return requests.get(url, timeout=20).json()


# ─────────────────────────────
# OKX AFFILIATE
# ─────────────────────────────
def okx_affiliate_detail(uid):
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    return okx_get(path)


def parse_okx_invitee_detail(resp):
    if resp.get("code") != "0" or not resp.get("data"):
        return None

    data = resp["data"][0]

    def pick(*keys, default=""):
        for key in keys:
            value = data.get(key)
            if value not in [None, ""]:
                return value
        return default

    vol_month = float(pick("volMonth", default=0) or 0)
    total_vol = float(pick("totalVol", "totalVolume", default=0) or 0)
    dep_amt = float(pick("depAmt", "depositAmt", "totalDepAmt", default=0) or 0)
    wd_amt = float(pick("wdAmt", "withdrawAmt", default=0) or 0)

    dep_15d = pick(
        "depAmt15d",
        "depAmt15D",
        "deposit15d",
        "deposit15D",
        "depAmtLast15Days",
        "depositLast15Days",
        default=""
    )

    vol_7d = pick(
        "vol7d",
        "vol7D",
        "volume7d",
        "volume7D",
        "tradeVol7d",
        "tradeVol7D",
        "volLast7Days",
        "volumeLast7Days",
        default=""
    )

    try:
        dep_15d = float(dep_15d) if dep_15d not in ["", None] else None
    except Exception:
        dep_15d = None

    try:
        vol_7d = float(vol_7d) if vol_7d not in ["", None] else None
    except Exception:
        vol_7d = None

    first_trade_time = pick(
        "firstTradeTime",
        "firstTradeTs",
        "firstTradeDate",
        "firstTradeAt"
    )

    register_time = pick(
        "joinTime",
        "registerTime",
        "regTime",
        "registerDate",
        "createTime",
        "kycTime"
    )

    kyc_time = pick("kycTime", "kycDate")
    affiliate_code = pick("affiliateCode", "affCode", "referralCode")
    region = pick("region", "country", "areaCode")
    invitee_level = pick("inviteeLevel", "level")

    did_first_trade = bool(first_trade_time) or vol_month > 0 or total_vol > 0

    return {
        "vol_month": vol_month,
        "total_vol": total_vol,
        "dep_amt": dep_amt,
        "wd_amt": wd_amt,
        "dep_15d": dep_15d,
        "vol_7d": vol_7d,
        "first_trade_time": ts_to_human(first_trade_time),
        "register_time": ts_to_human(register_time),
        "kyc_time": ts_to_human(kyc_time),
        "affiliate_code": affiliate_code,
        "region": region,
        "invitee_level": invitee_level,
        "did_first_trade": did_first_trade,
        "raw": data
    }


def get_uid_volume(uid):
    resp = okx_affiliate_detail(uid)
    parsed = parse_okx_invitee_detail(resp)

    if parsed is None:
        return None

    return parsed["vol_month"]


def get_uid_report(uid):
    resp = okx_affiliate_detail(uid)
    parsed = parse_okx_invitee_detail(resp)

    if parsed is None:
        return None

    local_user = get_user_by_uid(uid)

    return {
        "uid": uid,
        "is_affiliate": True,
        "is_local_community": local_user is not None,
        "telegram_id": local_user["telegram_id"] if local_user else "",
        "first_name": local_user["first_name"] if local_user else "",
        "username": local_user["username"] if local_user else "",
        "joined_at": local_user["joined_at"] if local_user else "",
        **parsed
    }


# ─────────────────────────────
# OKX CUENTA PROPIA: VOLUMEN MAESTRO
# ─────────────────────────────
def okx_trade_fills_history(inst_type, begin_ms, end_ms, after=None, limit=100):
    params = {
        "instType": inst_type,
        "begin": str(begin_ms),
        "end": str(end_ms),
        "limit": str(limit)
    }

    if after:
        params["after"] = str(after)

    path = "/api/v5/trade/fills-history?" + urlencode(params)
    return okx_get(path)


def get_instrument_info(inst_type, inst_id):
    cache_key = f"{inst_type}:{inst_id}"

    if cache_key in INSTRUMENTS_CACHE:
        return INSTRUMENTS_CACHE[cache_key]

    params = {
        "instType": inst_type,
        "instId": inst_id
    }

    path = "/api/v5/public/instruments?" + urlencode(params)
    resp = okx_public_get(path)

    if resp.get("code") == "0" and resp.get("data"):
        info = resp["data"][0]
        INSTRUMENTS_CACHE[cache_key] = info
        return info

    INSTRUMENTS_CACHE[cache_key] = {}
    return {}


def estimate_fill_volume_usdt(fill):
    inst_type = fill.get("instType", "")
    inst_id = fill.get("instId", "")

    fill_sz = safe_float(fill.get("fillSz"))
    fill_px = safe_float(fill.get("fillPx"))

    if fill_sz <= 0 or fill_px <= 0:
        return 0.0

    if inst_type in ["SPOT", "MARGIN"]:
        return fill_sz * fill_px

    inst_info = get_instrument_info(inst_type, inst_id)

    ct_val = safe_float(inst_info.get("ctVal"))
    ct_val_ccy = str(inst_info.get("ctValCcy") or "").upper()

    if ct_val <= 0:
        return fill_sz * fill_px

    if ct_val_ccy in ["USD", "USDT", "USDC"]:
        return fill_sz * ct_val

    return fill_sz * ct_val * fill_px


def parse_period_to_days(args):
    if not args:
        return 7

    raw = args[0].lower().strip()
    raw = raw.replace("d", "")
    raw = raw.replace("dias", "")
    raw = raw.replace("días", "")
    raw = raw.replace("dia", "")
    raw = raw.replace("día", "")

    try:
        days = int(raw)
    except Exception:
        return None

    if days <= 0:
        return None

    if days > 90:
        days = 90

    return days


def get_master_trading_volume(days=7):
    end_dt = datetime.now(timezone.utc)
    begin_dt = end_dt - timedelta(days=days)

    begin_ms = int(begin_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    inst_types = ["SPOT", "MARGIN", "SWAP", "FUTURES", "OPTION"]

    total_volume = 0.0
    total_fills = 0

    by_type = {
        "SPOT": 0.0,
        "MARGIN": 0.0,
        "SWAP": 0.0,
        "FUTURES": 0.0,
        "OPTION": 0.0
    }

    errors = []

    for inst_type in inst_types:
        after = None
        pages = 0

        while True:
            pages += 1

            if pages > 20:
                break

            try:
                resp = okx_trade_fills_history(
                    inst_type=inst_type,
                    begin_ms=begin_ms,
                    end_ms=end_ms,
                    after=after,
                    limit=100
                )
            except Exception as e:
                errors.append(f"{inst_type}: {e}")
                break

            if resp.get("code") != "0":
                errors.append(f"{inst_type}: code={resp.get('code')} msg={resp.get('msg')}")
                break

            data = resp.get("data") or []

            if not data:
                break

            for fill in data:
                vol = estimate_fill_volume_usdt(fill)
                total_volume += vol
                by_type[inst_type] += vol
                total_fills += 1

            last_bill_id = data[-1].get("billId")

            if not last_bill_id:
                break

            after = last_bill_id

            time.sleep(0.15)

            if len(data) < 100:
                break

    return {
        "days": days,
        "begin": begin_dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S"),
        "end": end_dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S"),
        "total_volume": total_volume,
        "total_fills": total_fills,
        "by_type": by_type,
        "errors": errors
    }


def format_master_volume_report(report):
    errors_text = ""

    if report["errors"]:
        errors_text = "\n\n⚠️ Alertas:\n" + "\n".join(report["errors"][:5])

    return (
        f"📊 Volumen propio cuenta maestra OKX\n\n"
        f"Periodo: últimos {report['days']} días\n"
        f"Desde: {report['begin']} AR\n"
        f"Hasta: {report['end']} AR\n\n"
        f"Total estimado: {money(report['total_volume'])} USDT\n"
        f"Fills encontrados: {report['total_fills']}\n\n"
        f"Detalle por mercado:\n"
        f"SPOT: {money(report['by_type'].get('SPOT'))} USDT\n"
        f"MARGIN: {money(report['by_type'].get('MARGIN'))} USDT\n"
        f"SWAP: {money(report['by_type'].get('SWAP'))} USDT\n"
        f"FUTURES: {money(report['by_type'].get('FUTURES'))} USDT\n"
        f"OPTION: {money(report['by_type'].get('OPTION'))} USDT\n\n"
        f"Este cálculo usa únicamente trades/fills propios de la cuenta API.\n"
        f"No incluye volumen de referidos."
        f"{errors_text}"
    )


# ─────────────────────────────
# FORMATOS DE REPORTE
# ─────────────────────────────
def format_uid_report(report):
    if report is None:
        return "❌ UID no encontrado en tu comunidad de afiliados OKX."

    first_trade = "Sí" if report["did_first_trade"] else "No"
    community = "Sí" if report["is_local_community"] else "No registrado en DB local"
    affiliate = "Sí" if report["is_affiliate"] else "No"

    username = report.get("username") or ""
    if username:
        username = f"@{username}"

    return (
        f"📊 Reporte UID: {report['uid']}\n\n"
        f"✅ Parte de tu afiliado OKX: {affiliate}\n"
        f"👥 Registrado en DB comunidad: {community}\n"
        f"📅 Fecha registro / join: {report.get('register_time') or 'No disponible'}\n"
        f"📅 Fecha KYC: {report.get('kyc_time') or 'No disponible'}\n"
        f"🌎 Región: {report.get('region') or 'No disponible'}\n"
        f"🏷️ Código afiliado: {report.get('affiliate_code') or 'No disponible'}\n"
        f"⭐ Nivel invitee: {report.get('invitee_level') or 'No disponible'}\n\n"
        f"💰 Depósito acumulado: {money(report.get('dep_amt'))} USDT\n"
        f"💰 Depósito últimos 15 días: {format_optional_usdt(report.get('dep_15d'))}\n"
        f"📈 Volumen mensual: {money(report.get('vol_month'))} USDT\n"
        f"📈 Volumen últimos 7 días: {format_optional_usdt(report.get('vol_7d'))}\n"
        f"📈 Volumen total histórico: {money(report.get('total_vol'))} USDT\n"
        f"🏦 Retiros acumulados: {money(report.get('wd_amt'))} USDT\n\n"
        f"🎯 Primer trade: {first_trade}\n"
        f"🕒 Fecha primer trade: {report.get('first_trade_time') or 'No disponible'}\n\n"
        f"Telegram: {report.get('first_name') or '-'} {username}"
    )


def format_uid_report_line(report, uid):
    if report is None:
        return f"❌ {uid} | No afiliado / no encontrado"

    first_trade = "Sí" if report["did_first_trade"] else "No"
    community = "Sí" if report["is_local_community"] else "No DB"

    dep_15d = report.get("dep_15d")
    vol_7d = report.get("vol_7d")

    dep_15d_txt = number(dep_15d) if dep_15d is not None else "N/D"
    vol_7d_txt = number(vol_7d) if vol_7d is not None else "N/D"

    return (
        f"✅ {uid} | "
        f"Comunidad: {community} | "
        f"Dep total: {number(report.get('dep_amt'))} | "
        f"Dep 15d: {dep_15d_txt} | "
        f"Vol mes: {number(report.get('vol_month'))} | "
        f"Vol 7d: {vol_7d_txt} | "
        f"Vol total: {number(report.get('total_vol'))} | "
        f"1er trade: {first_trade}"
    )


# ─────────────────────────────
# MENSAJES FLANDERS Y FRED
# ─────────────────────────────
def group_welcome_text(user):
    return (
        f"🚀👋 Bienvenido {mention_html(user.id, user.first_name)} al grupo {GROUP_NAME}.\n\n"
        "Aquí encontrarás bots exclusivos, tips de trading y beneficios por pertenecer a nuestra comunidad, "
        "además de soporte personalizado en OKX.\n\n"
        "🔥 Prepárate para aprovechar al máximo las oportunidades del mercado.\n\n"
        "¡Saludos!"
    )


def private_rules_text(user):
    return (
        f"🚀 Bienvenido {mention_html(user.id, user.first_name)} al grupo {GROUP_NAME}.\n\n"
        "Aquí encontrarás bots exclusivos, tips de trading y beneficios por pertenecer a nuestra comunidad, "
        "además de soporte personalizado en OKX.\n\n"
        "Puedes consultar tu volumen mensual escribiendo /volumen en este bot.\n\n"
        "¡Saludos y buenos trades! 📈"
    )


async def send_welcome(context, user):
    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=group_welcome_text(user),
        parse_mode=ParseMode.HTML
    )


# ─────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Bienvenido a {GROUP_NAME}.\n\n"
        "Solicita el acceso al grupo VIP y envíame tu UID de OKX por privado.\n\n"
        "Si solo quieres verificar si tu UID está registrado bajo el afiliado, usa:\n"
        "/verificaruid TU_UID"
    )


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"📌 Bienvenido al grupo {GROUP_NAME}.\n\n"
                "Envíame tu UID de OKX usando solo números para validar acceso."
            )
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar DM al usuario {user.id}: {e}")


async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)
        await send_welcome(context, user)

        await context.bot.send_message(
            chat_id=user.id,
            text=private_rules_text(user),
            parse_mode=ParseMode.HTML
        )
        return

    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    uid = text

    try:
        report = get_uid_report(uid)
    except Exception as e:
        print(f"Error validando UID={uid}: {e}")
        await update.message.reply_text("❌ Error consultando OKX. Intenta nuevamente más tarde.")
        return

    if report is None:
        await update.message.reply_text("UID no válido o no es referido.")
        return

    vol_month = float(report.get("vol_month") or 0)

    save_user(
        telegram_id=user.id,
        uid=uid,
        first_name=user.first_name,
        username=user.username,
        last_vol_month=vol_month
    )

    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await update.message.reply_text("✔️ UID verificado correctamente. Acceso aprobado.")

    await context.bot.send_message(
        chat_id=user.id,
        text=private_rules_text(user),
        parse_mode=ParseMode.HTML
    )

    await send_welcome(context, user)


# ─────────────────────────────
# PÚBLICO: VERIFICAR UID CONTRA AFILIADO
# ─────────────────────────────
async def verificaruid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Para verificar tu UID, escríbeme por privado y usa:\n\n"
            "/verificaruid TU_UID"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n"
            "/verificaruid 123456789\n\n"
            "Envía tu UID de OKX usando solo números."
        )
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        resp = okx_affiliate_detail(uid)

        if resp.get("code") == "0" and resp.get("data"):
            await update.message.reply_text(
                "✅ Tu UID aparece registrado correctamente bajo el afiliado correspondiente.\n\n"
                "Puedes continuar con el proceso de acceso a la comunidad."
            )
        else:
            await update.message.reply_text(
                "❌ Este UID no aparece registrado bajo el afiliado correspondiente.\n\n"
                "Verifica que copiaste bien tu UID de OKX y que creaste tu cuenta con el link o código correcto."
            )

    except Exception as e:
        print(f"Error verificando UID público={uid}: {e}")
        await update.message.reply_text(
            "❌ Hubo un error consultando OKX.\n"
            "Intenta nuevamente más tarde."
        )


# ─────────────────────────────
# USUARIO: CONSULTAR VOLUMEN
# ─────────────────────────────
async def volumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Para consultar tu volumen, escríbeme por privado y usa /volumen."
        )
        return

    user = update.message.from_user
    row = get_user_by_telegram_id(user.id)

    if not row or not row["uid"]:
        await update.message.reply_text(
            "❌ No encontré un UID registrado para tu usuario.\n\n"
            "Primero debes validar tu acceso enviando tu UID de OKX."
        )
        return

    uid = row["uid"]

    try:
        vol_month = get_uid_volume(uid)

        if vol_month is None:
            await update.message.reply_text(
                "❌ No pude consultar tu volumen en OKX en este momento.\n"
                "Intenta nuevamente más tarde."
            )
            return

        update_user_volume_by_uid(uid, vol_month)

        await update.message.reply_text(
            "📊 Volumen mensual OKX\n\n"
            f"UID: {uid}\n"
            f"Volumen acumulado del mes: {vol_month:.0f} USDT\n\n"
            "Este volumen corresponde al mes en curso."
        )

    except Exception as e:
        print(f"Error consultando volumen para TG={user.id}: {e}")

        await update.message.reply_text(
            "❌ Hubo un error consultando tu volumen.\n"
            "Intenta nuevamente más tarde."
        )


# ─────────────────────────────
# ADMIN: VOLUMEN PROPIO CUENTA MAESTRA
# ─────────────────────────────
async def mivolumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa este comando en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    days = parse_period_to_days(context.args)

    if days is None:
        await update.message.reply_text(
            "Uso:\n"
            "/mivolumen\n"
            "/mivolumen 7d\n"
            "/mivolumen 15d\n"
            "/mivolumen 30d\n\n"
            "Máximo permitido: 90d."
        )
        return

    await update.message.reply_text(
        f"⏳ Consultando volumen propio de la cuenta maestra para los últimos {days} días..."
    )

    try:
        report = get_master_trading_volume(days=days)
        await update.message.reply_text(format_master_volume_report(report))
    except Exception as e:
        print(f"Error en /mivolumen: {e}")
        await update.message.reply_text(
            "❌ Error consultando el volumen propio de la cuenta maestra.\n"
            "Verifica que la API key tenga permiso Read para trading/account."
        )


# ─────────────────────────────
# ADMIN COMMANDS FLANDERS Y FRED
# ─────────────────────────────
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ No autorizado.")
        return

    users = get_all_users()

    if not users:
        await update.message.reply_text("No hay usuarios registrados todavía.")
        return

    texto = "📋 LISTA DE USUARIOS VIP FLANDERS Y FRED\n\n"

    for u in users:
        username = u["username"] or ""
        if username:
            username = f"@{username}"
        texto += f"UID: {u['uid']} | TG: {u['telegram_id']} | {u['first_name'] or ''} {username}\n"

    if len(texto) <= 3900:
        await update.message.reply_text(texto)
    else:
        filename = f"lista_flanders_fred_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.txt"
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(texto)

        await context.bot.send_document(
            chat_id=update.message.from_user.id,
            document=open(filepath, "rb"),
            filename=filename,
            caption="📋 Lista de usuarios VIP Flanders y Fred"
        )


async def sorteo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ No autorizado.")
        return

    users = get_all_users()

    if len(users) < 2:
        await update.message.reply_text("No hay suficientes usuarios para sorteo.")
        return

    winners = random.sample(list(users), 2)

    mensaje = "🎉 SORTEO VIP FLANDERS Y FRED 🎉\n\n"

    for i, w in enumerate(winners, start=1):
        mensaje += f"{i}️⃣ UID: {w['uid']} | TG: {w['telegram_id']}\n"

    await update.message.reply_text(mensaje)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ No autorizado.")
        return

    ranking = []

    await update.message.reply_text("⏳ Consultando TOP volumen del mes Flanders y Fred...")

    for user in get_all_users():
        try:
            vol = get_uid_volume(user["uid"])

            if vol is not None:
                ranking.append((user["uid"], vol))
                update_user_volume_by_uid(user["uid"], vol)

            time.sleep(0.4)

        except Exception as e:
            print(f"Error consultando top UID={user['uid']}: {e}")

    ranking.sort(key=lambda x: x[1], reverse=True)

    mensaje = "🏆 TOP VOLUMEN DEL MES FLANDERS Y FRED\n\n"

    for i, r in enumerate(ranking[:10], start=1):
        mensaje += f"{i}. UID {r[0]} — {r[1]:.0f} USDT\n"

    await update.message.reply_text(mensaje)


# ─────────────────────────────
# ADMIN: VOLUMEN POR UID
# ─────────────────────────────
async def voluid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa este comando en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /voluid 123456789")
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        vol_month = get_uid_volume(uid)

        if vol_month is None:
            await update.message.reply_text("❌ No pude consultar ese UID en OKX.")
            return

        update_user_volume_by_uid(uid, vol_month)

        await update.message.reply_text(
            "📊 Consulta admin por UID\n\n"
            f"UID: {uid}\n"
            f"Volumen acumulado del mes: {vol_month:.0f} USDT"
        )

    except Exception as e:
        print(f"Error admin consultando UID={uid}: {e}")
        await update.message.reply_text("❌ Error consultando el UID.")


# ─────────────────────────────
# ADMIN: REPORTE POR UID
# ─────────────────────────────
async def checkuid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /checkuid 123456789")
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        report = get_uid_report(uid)
        await update.message.reply_text(format_uid_report(report))
    except Exception as e:
        print(f"Error en /checkuid UID={uid}: {e}")
        await update.message.reply_text("❌ Error consultando el UID.")


# ─────────────────────────────
# ADMIN: REPORTE MÚLTIPLE
# ─────────────────────────────
async def checkuids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n"
            "/checkuids 123 456 789\n\n"
            "También puedes separar por comas."
        )
        return

    raw_text = " ".join(context.args)
    uids = split_uids(raw_text)

    if not uids:
        await update.message.reply_text("No encontré UIDs válidos.")
        return

    if len(uids) > 20:
        await update.message.reply_text(
            "Por ahora consulta máximo 20 UIDs por mensaje para evitar rate limit de OKX."
        )
        return

    lines = ["📊 Reporte múltiple de UIDs Flanders y Fred\n"]

    for uid in uids:
        try:
            report = get_uid_report(uid)
            lines.append(format_uid_report_line(report, uid))
            time.sleep(0.4)
        except Exception as e:
            print(f"Error consultando UID={uid}: {e}")
            lines.append(f"⚠️ {uid} | Error consultando")

    text = "\n".join(lines)

    if len(text) <= 3900:
        await update.message.reply_text(text)
    else:
        filename = f"reporte_uids_flanders_fred_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.txt"
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)

        await context.bot.send_document(
            chat_id=admin_id,
            document=open(filepath, "rb"),
            filename=filename,
            caption="📄 Reporte múltiple de UIDs Flanders y Fred"
        )


# ─────────────────────────────
# ADMIN: REPORTE CSV DE LISTA
# ─────────────────────────────
async def checkuidscsv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n"
            "/checkuidscsv 123 456 789\n\n"
            "También puedes separar por comas."
        )
        return

    raw_text = " ".join(context.args)
    uids = split_uids(raw_text)

    if not uids:
        await update.message.reply_text("No encontré UIDs válidos.")
        return

    if len(uids) > 100:
        await update.message.reply_text("Máximo 100 UIDs por CSV para evitar rate limits.")
        return

    rows = []

    for uid in uids:
        try:
            report = get_uid_report(uid)

            if report is None:
                rows.append({
                    "uid": uid,
                    "afiliado_okx": "No",
                    "comunidad_db": "No",
                    "fecha_registro_join": "",
                    "fecha_kyc": "",
                    "region": "",
                    "codigo_afiliado": "",
                    "invitee_level": "",
                    "deposito_total_usdt": 0,
                    "deposito_15d_usdt": "",
                    "volumen_mes_usdt": 0,
                    "volumen_7d_usdt": "",
                    "volumen_total_usdt": 0,
                    "retiros_total_usdt": 0,
                    "primer_trade": "No",
                    "fecha_primer_trade": "",
                    "telegram_id": "",
                    "first_name": "",
                    "username": "",
                    "joined_at": "",
                })
            else:
                rows.append({
                    "uid": uid,
                    "afiliado_okx": "Si",
                    "comunidad_db": "Si" if report.get("is_local_community") else "No",
                    "fecha_registro_join": report.get("register_time") or "",
                    "fecha_kyc": report.get("kyc_time") or "",
                    "region": report.get("region") or "",
                    "codigo_afiliado": report.get("affiliate_code") or "",
                    "invitee_level": report.get("invitee_level") or "",
                    "deposito_total_usdt": report.get("dep_amt") or 0,
                    "deposito_15d_usdt": report.get("dep_15d") if report.get("dep_15d") is not None else "",
                    "volumen_mes_usdt": report.get("vol_month") or 0,
                    "volumen_7d_usdt": report.get("vol_7d") if report.get("vol_7d") is not None else "",
                    "volumen_total_usdt": report.get("total_vol") or 0,
                    "retiros_total_usdt": report.get("wd_amt") or 0,
                    "primer_trade": "Si" if report.get("did_first_trade") else "No",
                    "fecha_primer_trade": report.get("first_trade_time") or "",
                    "telegram_id": report.get("telegram_id") or "",
                    "first_name": report.get("first_name") or "",
                    "username": report.get("username") or "",
                    "joined_at": report.get("joined_at") or "",
                })

            time.sleep(0.4)

        except Exception as e:
            print(f"Error CSV consultando UID={uid}: {e}")
            rows.append({
                "uid": uid,
                "afiliado_okx": "Error",
                "comunidad_db": "",
                "fecha_registro_join": "",
                "fecha_kyc": "",
                "region": "",
                "codigo_afiliado": "",
                "invitee_level": "",
                "deposito_total_usdt": "",
                "deposito_15d_usdt": "",
                "volumen_mes_usdt": "",
                "volumen_7d_usdt": "",
                "volumen_total_usdt": "",
                "retiros_total_usdt": "",
                "primer_trade": "",
                "fecha_primer_trade": "",
                "telegram_id": "",
                "first_name": "",
                "username": "",
                "joined_at": "",
            })

    filename = f"reporte_uids_flanders_fred_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.csv"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    fieldnames = [
        "uid",
        "afiliado_okx",
        "comunidad_db",
        "fecha_registro_join",
        "fecha_kyc",
        "region",
        "codigo_afiliado",
        "invitee_level",
        "deposito_total_usdt",
        "deposito_15d_usdt",
        "volumen_mes_usdt",
        "volumen_7d_usdt",
        "volumen_total_usdt",
        "retiros_total_usdt",
        "primer_trade",
        "fecha_primer_trade",
        "telegram_id",
        "first_name",
        "username",
        "joined_at",
    ]

    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    await context.bot.send_document(
        chat_id=admin_id,
        document=open(filepath, "rb"),
        filename=filename,
        caption="📄 Reporte CSV de UIDs Flanders y Fred / OKX"
    )


# ─────────────────────────────
# ADMIN: DEBUG CAMPOS OKX
# ─────────────────────────────
async def debuguid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /debuguid 123456789")
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        resp = okx_affiliate_detail(uid)

        filename = f"debug_okx_uid_{uid}_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.json"
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(resp, f, ensure_ascii=False, indent=2)

        await context.bot.send_document(
            chat_id=admin_id,
            document=open(filepath, "rb"),
            filename=filename,
            caption=(
                "🧪 Debug OKX Affiliate Detail.\n"
                "Revisa este JSON para confirmar los nombres exactos de campos disponibles."
            )
        )

    except Exception as e:
        print(f"Error en /debuguid UID={uid}: {e}")
        await update.message.reply_text("❌ Error generando debug del UID.")


# ─────────────────────────────
# ADMIN: INFORME CSV
# ─────────────────────────────
async def informe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa /informe en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    users = get_all_users()

    if not users:
        await update.message.reply_text("No hay usuarios registrados todavía.")
        return

    updated_rows = []

    for row in users:
        uid = row["uid"]

        try:
            report = get_uid_report(uid)

            if report is None:
                vol_month = row["last_vol_month"] or 0
                dep_amt = ""
                total_vol = ""
                wd_amt = ""
                first_trade = ""
                dep_15d = ""
                vol_7d = ""
            else:
                vol_month = report.get("vol_month") or 0
                dep_amt = report.get("dep_amt") or 0
                total_vol = report.get("total_vol") or 0
                wd_amt = report.get("wd_amt") or 0
                first_trade = "Si" if report.get("did_first_trade") else "No"
                dep_15d = report.get("dep_15d") if report.get("dep_15d") is not None else ""
                vol_7d = report.get("vol_7d") if report.get("vol_7d") is not None else ""

                update_user_volume_by_uid(uid, vol_month)

            time.sleep(0.4)

        except Exception as e:
            print(f"⚠️ No se pudo actualizar UID={uid}: {e}")
            vol_month = row["last_vol_month"] or 0
            dep_amt = ""
            total_vol = ""
            wd_amt = ""
            first_trade = ""
            dep_15d = ""
            vol_7d = ""

        updated_rows.append({
            "telegram_id": row["telegram_id"],
            "first_name": row["first_name"] or "",
            "username": row["username"] or "",
            "uid": uid,
            "deposito_total_usdt": dep_amt,
            "deposito_15d_usdt": dep_15d,
            "volumen_mes_usdt": vol_month,
            "volumen_7d_usdt": vol_7d,
            "volumen_total_usdt": total_vol,
            "retiros_total_usdt": wd_amt,
            "primer_trade": first_trade,
            "joined_at": row["joined_at"],
            "last_checked_at": now_utc_iso()
        })

    filename = f"informe_flanders_fred_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.csv"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    fieldnames = [
        "telegram_id",
        "first_name",
        "username",
        "uid",
        "deposito_total_usdt",
        "deposito_15d_usdt",
        "volumen_mes_usdt",
        "volumen_7d_usdt",
        "volumen_total_usdt",
        "retiros_total_usdt",
        "primer_trade",
        "joined_at",
        "last_checked_at"
    ]

    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    await context.bot.send_document(
        chat_id=admin_id,
        document=open(filepath, "rb"),
        filename=filename,
        caption="📄 Informe de usuarios Flanders y Fred / OKX"
    )


# ─────────────────────────────
# REPORTES ADMIN PROGRAMADOS
# ─────────────────────────────
async def weekly_admin_report(context: ContextTypes.DEFAULT_TYPE):
    await generate_admin_report(context, "📊 REPORTE SEMANAL ADMIN FLANDERS Y FRED")


async def monthly_admin_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ_AR)

    if now.day != 30:
        return

    await generate_admin_report(context, "📊 REPORTE MENSUAL ADMIN FLANDERS Y FRED")


async def generate_admin_report(context, title):
    total_volumen = 0.0
    usuarios = get_all_users()

    for u in usuarios:
        try:
            vol = get_uid_volume(u["uid"])

            if vol is not None:
                total_volumen += vol
                update_user_volume_by_uid(u["uid"], vol)

            time.sleep(0.4)

        except Exception as e:
            print(f"Error reporte admin UID={u['uid']}: {e}")

    texto = (
        f"{title}\n\n"
        f"Comunidad: {GROUP_NAME}\n"
        f"Usuarios registrados: {len(usuarios)}\n"
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

    # Base
    app.add_handler(CommandHandler("start", start))

    # Comandos públicos / usuario
    app.add_handler(CommandHandler("verificaruid", verificaruid))
    app.add_handler(CommandHandler("volumen", volumen))

    # Comandos admin originales Flanders y Fred
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("sorteo", sorteo))
    app.add_handler(CommandHandler("top", top))

    # Comandos admin nuevos
    app.add_handler(CommandHandler("voluid", voluid))
    app.add_handler(CommandHandler("mivolumen", mivolumen))
    app.add_handler(CommandHandler("checkuid", checkuid))
    app.add_handler(CommandHandler("checkuids", checkuids))
    app.add_handler(CommandHandler("checkuidscsv", checkuidscsv))
    app.add_handler(CommandHandler("debuguid", debuguid))
    app.add_handler(CommandHandler("informe", informe))

    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    app.job_queue.run_daily(
        weekly_admin_report,
        time=datetime.strptime("00:00", "%H:%M").time(),
        days=(6,),
        name="weekly_report_flanders_fred"
    )

    app.job_queue.run_daily(
        monthly_admin_report,
        time=datetime.strptime("00:05", "%H:%M").time(),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="monthly_admin_report_flanders_fred"
    )

    print(f"🤖 BOT {GROUP_NAME} iniciado.")
    print(f"📁 DB_PATH: {DB_PATH}")

    app.run_polling()


if __name__ == "__main__":
    main()
