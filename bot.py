       import os
import csv
import hmac
import json
import time
import re
import base64
import hashlib
import sqlite3
import requests
import tempfile
import random

from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.helpers import mention_html
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Defaults,
)

# -----------------------------
# ENV
# -----------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
VIP_CHAT_ID = int(os.environ["VIP_CHAT_ID"])

PUBLIC_CHAT_ID = int(os.environ.get("PUBLIC_CHAT_ID", "-1003133540062"))

OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

BYPASS_CODE = os.environ.get("BYPASS_CODE", "00000000010101010")
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

DB_PATH = os.environ.get("DB_PATH", "/var/data/flanders_fred_bot.db")

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")
GROUP_NAME = "Comunidad Flanders y Fred VIP by OKX"
OKX_BASE_URL = "https://www.okx.com"
INSTRUMENTS_CACHE = {}

OKX_JOIN_LINK = "https://www.okx.com/join/FLANDERSYFRED"
REBIND_FORM_LINK = "https://www.okx.com/ul/J6l2R5"
FLANDERS_PRIVATE_LINK = "https://t.me/ivandp93"
PUBLIC_BOT_REPLY_LINK = "https://t.me/+1hKD3O8rj8ZiOTQx"

VALID_REF_CODES_TEXT = (
    "71790605\n"
    "27221066\n"
    "FLANDERSYFRED\n"
    "ELTRADERROLO"
)


def get_affiliate_accounts():
    accounts = []

    accounts.append({
        "name": os.environ.get("KOL1_NAME", "Flanders y Fred").strip() or "Flanders y Fred",
        "api_key": OKX_API_KEY,
        "api_secret": OKX_API_SECRET,
        "passphrase": OKX_API_PASSPHRASE,
    })

    for i in range(2, 11):
        name = os.environ.get(f"KOL{i}_NAME", f"KOL{i}").strip() or f"KOL{i}"
        api_key = os.environ.get(f"OKX_API_KEY_KOL{i}", "").strip()
        api_secret = os.environ.get(f"OKX_API_SECRET_KOL{i}", "").strip()
        passphrase = os.environ.get(f"OKX_API_PASSPHRASE_KOL{i}", "").strip()

        if api_key and api_secret and passphrase:
            accounts.append({
                "name": name,
                "api_key": api_key,
                "api_secret": api_secret,
                "passphrase": passphrase,
            })

    return accounts


# -----------------------------
# UTILS
# -----------------------------
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


def contains_bot_keyword(text: str) -> bool:
    if not text:
        return False

    return re.search(r"\b(bot|bots)\b", text.lower()) is not None


# -----------------------------
# DATABASE
# -----------------------------
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_states (
        telegram_id INTEGER PRIMARY KEY,
        uid TEXT,
        first_name TEXT,
        username TEXT,
        flow TEXT,
        status TEXT,
        source TEXT,
        created_at TEXT,
        last_interaction_at TEXT
    )
    """)

    conn.commit()
    conn.close()

    print(f"DB inicializada en: {DB_PATH}")


def save_user_state(
    telegram_id,
    uid=None,
    first_name=None,
    username=None,
    flow=None,
    status="pending",
    source=""
):
    conn = db()
    cur = conn.cursor()

    now = now_utc_iso()

    cur.execute("SELECT * FROM user_states WHERE telegram_id = ?", (telegram_id,))
    existing = cur.fetchone()

    if existing:
        final_uid = uid if uid is not None else existing["uid"]
        final_flow = flow if flow is not None else existing["flow"]
        final_status = status if status is not None else existing["status"]

        cur.execute("""
            UPDATE user_states
            SET uid = ?,
                first_name = ?,
                username = ?,
                flow = ?,
                status = ?,
                source = ?,
                last_interaction_at = ?
            WHERE telegram_id = ?
        """, (
            final_uid,
            first_name,
            username,
            final_flow,
            final_status,
            source,
            now,
            telegram_id
        ))
    else:
        cur.execute("""
            INSERT INTO user_states (
                telegram_id,
                uid,
                first_name,
                username,
                flow,
                status,
                source,
                created_at,
                last_interaction_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            telegram_id,
            uid,
            first_name,
            username,
            flow or "",
            status or "pending",
            source or "",
            now,
            now
        ))

    conn.commit()
    conn.close()


def get_user_state(telegram_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM user_states WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    conn.close()
    return row


def get_all_user_states():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT telegram_id, uid, first_name, username, flow, status, source, created_at, last_interaction_at
        FROM user_states
        ORDER BY created_at ASC
    """)
    rows = cur.fetchall()

    conn.close()
    return rows


def get_user_state_counts():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN uid IS NOT NULL AND uid != '' THEN 1 ELSE 0 END) AS con_uid,
            SUM(CASE WHEN uid IS NULL OR uid = '' THEN 1 ELSE 0 END) AS sin_uid,
            SUM(CASE WHEN status = 'validated' THEN 1 ELSE 0 END) AS validados,
            SUM(CASE WHEN status = 'not_affiliated' THEN 1 ELSE 0 END) AS no_afiliados,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pendientes
        FROM user_states
    """)
    row = cur.fetchone()

    conn.close()
    return row


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

    print(f"Usuario guardado: TG={telegram_id} UID={uid} VOL={last_vol_month}")


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


# -----------------------------
# OKX BASE
# -----------------------------
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


def sign_okx_with_credentials(method, path, api_secret, body=""):
    timestamp = get_okx_server_time_iso()
    message = timestamp + method + path + body

    mac = hmac.new(
        api_secret.encode(),
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


def okx_get_with_credentials(path, account):
    ts, signature = sign_okx_with_credentials(
        method="GET",
        path=path,
        api_secret=account["api_secret"]
    )

    headers = {
        "OK-ACCESS-KEY": account["api_key"],
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": account["passphrase"],
        "Content-Type": "application/json"
    }

    url = OKX_BASE_URL + path
    return requests.get(url, headers=headers, timeout=20).json()


def okx_public_get(path):
    url = OKX_BASE_URL + path
    return requests.get(url, timeout=20).json()


# -----------------------------
# OKX AFFILIATE MULTI-KOL
# -----------------------------
def okx_affiliate_detail(uid, account=None):
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"

    if account is None:
        return okx_get(path)

    return okx_get_with_credentials(path, account)


def find_uid_in_affiliates(uid):
    accounts = get_affiliate_accounts()
    errors = []

    for account in accounts:
        try:
            resp = okx_affiliate_detail(uid, account)

            print(f"UID CONSULTADO: {uid}")
            print(f"KOL CONSULTADO: {account['name']}")
            print(f"RESPUESTA OKX: {resp}")

            if resp.get("code") == "0" and resp.get("data"):
                data = resp["data"][0]
                vol_month = safe_float(data.get("volMonth"))

                return {
                    "found": True,
                    "account_name": account["name"],
                    "data": data,
                    "vol_month": vol_month,
                    "raw_resp": resp,
                }

            errors.append({
                "account_name": account["name"],
                "code": resp.get("code"),
                "msg": resp.get("msg"),
            })

            time.sleep(0.25)

        except Exception as e:
            print(f"Error consultando UID={uid} en {account['name']}: {e}")
            errors.append({
                "account_name": account["name"],
                "error": str(e),
            })

    return {
        "found": False,
        "account_name": "",
        "data": None,
        "vol_month": 0,
        "raw_resp": None,
        "errors": errors,
    }


def is_uid_affiliated(uid):
    result = find_uid_in_affiliates(uid)

    if result["found"]:
        return True, result["data"], result["vol_month"], result

    return False, None, 0, result


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
    result = find_uid_in_affiliates(uid)

    if not result["found"]:
        return None

    return result["vol_month"]


def get_uid_report(uid):
    result = find_uid_in_affiliates(uid)

    if not result["found"]:
        return None

    parsed = parse_okx_invitee_detail(result["raw_resp"])

    if parsed is None:
        return None

    local_user = get_user_by_uid(uid)

    return {
        "uid": uid,
        "is_affiliate": True,
        "affiliate_account_name": result["account_name"],
        "is_local_community": local_user is not None,
        "telegram_id": local_user["telegram_id"] if local_user else "",
        "first_name": local_user["first_name"] if local_user else "",
        "username": local_user["username"] if local_user else "",
        "joined_at": local_user["joined_at"] if local_user else "",
        **parsed
    }


# -----------------------------
# OKX CUENTA PROPIA: VOLUMEN MAESTRO
# -----------------------------
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
