from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import sqlite3
import time
import os
import json
import uuid as _uuid
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()  # загружаем .env до os.getenv

try:
    from xui_api import XUIApi
    xui = XUIApi()
except ImportError:
    xui = None

app = Flask(__name__, static_folder="webapp")
CORS(app)

DB_PATH = "vpn_bot.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            sub_end INTEGER DEFAULT 0,
            balance REAL DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            days INTEGER,
            price_usd REAL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id TEXT UNIQUE,
            user_id INTEGER,
            tariff_id INTEGER,
            amount REAL,
            currency TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    ''')

    cur = conn.execute("SELECT COUNT(*) FROM tariffs")
    if cur.fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO tariffs (name, days, price_usd) VALUES (?,?,?)",
            [("Личный · 1 мес", 30, 3.00),
             ("Семейный · 1 мес", 30, 6.00),
             ("Максимальный · 1 мес", 30, 9.00),
             ("Личный · 1 год", 365, 25.00),
             ("Семейный · 1 год", 365, 40.00),
             ("Максимальный · 1 год", 365, 55.00)]
        )
    conn.commit()
    conn.close()

init_db()

@app.route("/")
@app.route("/webapp/index.html")
def index():
    return send_from_directory("webapp", "index.html")

@app.route("/api/tariffs")
def tariffs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tariffs").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/profile")
def profile():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"error": "no user_id"}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    if not user:
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    conn.close()

    now = int(time.time() * 1000)
    sub_end = user["sub_end"] or 0
    active = sub_end > now

    return jsonify({
        "user_id": user["user_id"],
        "username": user["username"] or str(user_id),
        "sub_active": active,
        "sub_end": sub_end,
        "days_left": max(0, int((sub_end - now) / 86400000)) if active else 0,
        "balance": user["balance"] or 0,
        "ref_count": 0,
        "ref_earned": 0,
        "traffic_up": 0,
        "traffic_down": 0,
        "created_at": user["created_at"] or 0,
        "payments": []
    })

# ============= КРИПТОПЛАТЕЖИ через CryptoBot =============
def create_crypto_invoice(amount, currency, tariff_id, user_id):
    """Создаёт инвойс в CryptoBot"""
    if not CRYPTO_TOKEN:
        return None, "CRYPTO_PAY_TOKEN не задан"

    asset = currency.upper().strip()
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    payload = {
        "currency_type": "crypto",
        "asset": asset,
        "amount": str(round(float(amount), 2)),
        "description": f"MrdotaVPN · тариф #{tariff_id}",
        "payload": json.dumps({"user_id": user_id, "tariff_id": tariff_id}),
        "paid_btn_name": "openBot",
        "paid_btn_url": "https://t.me/MrdotaVPNrobot",
        "allow_comments": False,
        "allow_anonymous": True,
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        data = r.json()
        if data.get("ok"):
            return data["result"], None
        err = data.get("error", {})
        msg = err.get("name", str(err)) if isinstance(err, dict) else str(err)
        print(f"CryptoPay API error: {data}")
        return None, f"CryptoPay: {msg}"
    except Exception as e:
        print(f"Crypto invoice exception: {e}")
        return None, str(e)

def check_invoice_status(invoice_id):
    """Проверяет статус инвойса"""
    if not CRYPTO_TOKEN:
        return None

    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    params = {"invoice_ids": invoice_id}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        data = r.json()
        if data.get("ok") and data["result"]["items"]:
            return data["result"]["items"][0]["status"]
    except Exception as e:
        print(f"Check invoice error: {e}")
    return None

@app.route("/api/create_invoice", methods=["POST"])
def create_invoice():
    data = request.get_json()
    user_id = data.get("user_id")
    tariff_id = data.get("tariff_id")
    currency = data.get("currency", "USDT")

    if not user_id or not tariff_id:
        return jsonify({"error": "missing params"}), 400

    conn = get_db()
    tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (tariff_id,)).fetchone()
    conn.close()

    if not tariff:
        return jsonify({"error": "tariff not found"}), 404

    amount = tariff["price_usd"]

    # Создаём инвойс в CryptoBot
    invoice, err_msg = create_crypto_invoice(amount, currency, tariff_id, user_id)

    if not invoice:
        return jsonify({"error": err_msg or "failed to create invoice"}), 500

    # Сохраняем в БД
    conn = get_db()
    conn.execute(
        "INSERT INTO payments (invoice_id, user_id, tariff_id, amount, currency) VALUES (?,?,?,?,?)",
        (invoice["invoice_id"], user_id, tariff_id, amount, currency)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "invoice_id": invoice["invoice_id"],
        "pay_url": invoice["pay_url"],
        "amount": amount,
        "currency": currency
    })

@app.route("/api/check_payment", methods=["POST"])
def check_payment():
    data = request.get_json()
    invoice_id = data.get("invoice_id")
    user_id = data.get("user_id")

    if not invoice_id:
        return jsonify({"error": "no invoice_id"}), 400

    status = check_invoice_status(invoice_id)

    if status == "paid":
        conn = get_db()
        # Проверяем не активирована ли уже
        pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        if pay and pay["status"] != "paid":
            # Активируем подписку
            tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (pay["tariff_id"],)).fetchone()
            if tariff:
                now_ms = int(time.time() * 1000)
                new_expiry = now_ms + (tariff["days"] * 86400000)

                # Обновляем подписку пользователя
                existing = conn.execute("SELECT sub_end FROM users WHERE user_id=?", (user_id,)).fetchone()
                current_end = existing["sub_end"] if existing else 0
                if current_end > now_ms:
                    new_expiry = current_end + (tariff["days"] * 86400000)

                conn.execute(
                    "UPDATE users SET sub_end = ? WHERE user_id=?",
                    (new_expiry, user_id)
                )
                conn.execute(
                    "UPDATE payments SET status='paid' WHERE invoice_id=?",
                    (invoice_id,)
                )
                conn.commit()
        conn.close()
        return jsonify({"success": True, "paid": True})

    return jsonify({"success": True, "paid": False, "status": status})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

@app.route("/api/get_config")
def get_config():
    """Return VLESS config link directly for miniapp display."""
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"error": "no user_id"}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "user not found"}), 404

    now_ms = int(time.time() * 1000)
    sub_end = user["sub_end"] or 0

    if sub_end <= now_ms:
        return jsonify({"error": "no_sub", "message": "Нет активной подписки"}), 403

    vless_link = None
    if xui and user["uuid"] and user["client_email"]:
        try:
            vless_link = xui.get_vless_link(user["client_email"], user["uuid"])
        except Exception as e:
            print(f"XUI get_vless_link error: {e}")

    if not vless_link:
        uid = user["uuid"]
        email = user["client_email"]
        if uid and email:
            import urllib.parse as _up
            host = os.getenv("XUI_HOST", os.getenv("XUI_URL", "").split("://")[-1].split(":")[0])
            port = os.getenv("XUI_PORT", "443")
            pbk  = os.getenv("XUI_PBK", "")
            sni  = os.getenv("XUI_SNI", "")
            sid  = os.getenv("XUI_SID", "")
            if host and pbk:
                params = _up.urlencode({
                    "type": "tcp", "security": "reality",
                    "pbk": pbk, "fp": "chrome",
                    "sni": sni or host, "sid": sid,
                    "spx": "%2F", "flow": "xtls-rprx-vision",
                })
                vless_link = f"vless://{uid}@{host}:{port}?{params}#{_up.quote(email)}"

    if not vless_link:
        return jsonify({
            "error": "config_unavailable",
            "message": "Конфиг временно недоступен. Запросите через бота: /config"
        }), 503

    return jsonify({
        "success": True,
        "vless": vless_link,
        "sub_end": sub_end,
        "days_left": max(0, int((sub_end - now_ms) / 86400000)),
    })


@app.route("/api/webhook/cryptopay", methods=["POST"])
def cryptopay_webhook():
    """CryptoBot webhook — вызывается автоматически при оплате."""
    raw_body = request.get_data(as_text=True)

    import hmac as _hmac, hashlib as _hashlib
    secret = _hashlib.sha256(CRYPTO_TOKEN.encode()).digest() if CRYPTO_TOKEN else b''
    sig = request.headers.get("crypto-pay-api-signature", "")
    expected = _hmac.new(secret, raw_body.encode(), _hashlib.sha256).hexdigest()
    if CRYPTO_TOKEN and sig != expected:
        return jsonify({"error": "bad signature"}), 403

    try:
        event = json.loads(raw_body)
    except Exception:
        return jsonify({"error": "bad json"}), 400

    if event.get("update_type") != "invoice_paid":
        return jsonify({"ok": True})

    invoice = event.get("payload", {})
    invoice_id = str(invoice.get("invoice_id", ""))
    if invoice.get("status") != "paid":
        return jsonify({"ok": True})

    try:
        meta = json.loads(invoice.get("payload", "{}"))
        user_id  = int(meta.get("user_id", 0))
        tariff_id = int(meta.get("tariff_id", 0))
    except Exception:
        return jsonify({"error": "bad payload"}), 400

    if not user_id or not tariff_id:
        return jsonify({"error": "missing meta"}), 400

    conn = get_db()
    pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    if pay and pay["status"] == "paid":
        conn.close()
        return jsonify({"ok": True})

    tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (tariff_id,)).fetchone()
    if not tariff:
        conn.close()
        return jsonify({"error": "tariff not found"}), 404

    now_ms = int(time.time() * 1000)
    existing = conn.execute("SELECT sub_end, uuid, client_email FROM users WHERE user_id=?", (user_id,)).fetchone()
    current_end = existing["sub_end"] if existing else 0
    base = current_end if (current_end and current_end > now_ms) else now_ms
    new_expiry = base + (tariff["days"] * 86400000)

    conn.execute("UPDATE users SET sub_end=? WHERE user_id=?", (new_expiry, user_id))
    conn.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,))
    conn.commit()

    # Provision XUI client
    if xui and existing:
        if existing["uuid"] and existing["client_email"]:
            xui.update_client_expiry(existing["client_email"], existing["uuid"], new_expiry, 0)
        else:
            new_uuid = str(_uuid.uuid4())
            email = f"tg_{user_id}"
            if xui.add_client(new_uuid, email, new_expiry, 0):
                conn.execute(
                    "UPDATE users SET uuid=?, client_email=? WHERE user_id=?",
                    (new_uuid, email, user_id)
                )
                conn.commit()

    conn.close()
    return jsonify({"ok": True})
