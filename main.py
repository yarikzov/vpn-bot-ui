# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import sqlite3
import qrcode
import logging
import threading
import time
import requests
import json
from io import BytesIO
from datetime import datetime, timedelta
import telebot
from telebot import types
from flask import Flask, request, jsonify, render_template

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "8204021215:AAFO3BSZn6e4keyB1gS3AEEA-IylhUWIMro"
WIREGUARD_SCRIPT_PATH = "/root/wireguard-install.sh"
SERVER_PUBLIC_KEY = "qSearch Rv98fGCTjLuxW4ygE8Hl… on blockchair.coSearch mRv98fGCTjLuxW4ygE8H… 
on blockchair.commRv98fGCTjLuxW4ygE8HlizQQyAsKTmCWbPRybFRywc="
SERVER_ENDPOINT = "136.0.8.219:51820"
ADMIN_USER_ID = 5593856626
CRYPTO_PAY_API_TOKEN = "502548:AAvGZlXQ13JYzhB3GEwTy4gbPc74iExUvmY"  # <--- ПРОВЕРЬТЕ ЭТОТ ТОКЕН!
WEBAPP_URL = "https://yarikzov.github.io/vpn-bot-ui/" # <--- ОБНОВИТЕ!
FLASK_PORT = 5000

# --- СИСТЕМНЫЕ НАСТРОЙКИ ---
app = Flask(__name__, template_folder='templates') # Инициализация Flask
bot = telebot.TeleBot(BOT_TOKEN)

# Настройка логирования (ИСПРАВЛЕНО: Путь к логу изменен)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        # Изменено на относительный путь для решения PermissionError
        logging.FileHandler('vpn-bot.log'), 
        logging.StreamHandler()
    ]
)

# --- КЭШИРОВАНИЕ КРИПТОВАЛЮТ ---
CRYPTO_CACHE = {}
LAST_UPDATE = 0
CACHE_LIFETIME = 60 # Время жизни кэша в секундах

# Улучшенный класс для работы с Crypto Pay API
class CryptoPay:
    def __init__(self, token):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
        self.session = requests.Session()
        self.session.headers.update({
            "Crypto-Pay-API-Token": self.token,
            "Content-Type": "application/json"
        })
        
    def _make_request(self, method, endpoint, data=None, timeout=30):
        url = f"{self.base_url}/{endpoint}"
        try:
            if method == "GET":
                response = self.session.get(url, params=data, timeout=timeout)
            else:
                response = self.session.post(url, json=data, timeout=timeout)
            
            response.raise_for_status() 
            
            result = response.json()
            if not result.get("ok"):
                error_msg = result.get('error', {}).get('name', 'Unknown error')
                logging.error(f"CryptoPay API Error: {error_msg}")
                return {'ok': False, 'error': error_msg, 'details': result}
                
            return result
            
        except requests.exceptions.RequestException as e:
            logging.error(f"CryptoPay API Exception: {e}")
            return {'ok': False, 'error': f"Network Error: {e}"}
            
    def create_invoice(self, asset, amount, description, payload=None):
        data = {
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "payload": payload
        }
        
        result = self._make_request("POST", "createInvoice", data)
        
        if result and result.get('ok'):
            invoice_data = result.get("result", {})
            return type('Invoice', (), {
                'invoice_id': invoice_data.get('invoice_id'),
                'pay_url': invoice_data.get('pay_url'),
                'status': invoice_data.get('status'),
                'amount': invoice_data.get('amount'),
                'asset': invoice_data.get('asset'),
                'ok': True
            })
        else:
            return type('Invoice', (), {
                'ok': False,
                'error': result.get('error', 'Unknown error'),
                'details': result.get('details', {})
            })
        
    def get_invoices(self, invoice_ids=None):
        data = {}
        if invoice_ids:
            if isinstance(invoice_ids, list):
                data["invoice_ids"] = ",".join(map(str, invoice_ids))
            else:
                data["invoice_ids"] = str(invoice_ids)
                
        result = self._make_request("GET", "getInvoices", data)
        if result and result.get('ok'):
            invoices = []
            for invoice_data in result["result"].get("items", []):
                invoice_obj = type('Invoice', (), {
                    'invoice_id': invoice_data.get('invoice_id'),
                    'status': invoice_data.get('status'),
                    'pay_url': invoice_data.get('pay_url'),
                    'amount': invoice_data.get('amount'),
                    'asset': invoice_data.get('asset')
                })
                invoices.append(invoice_obj)
            return invoices
        return []

# Инициализация Crypto Pay
crypto_client = CryptoPay(CRYPTO_PAY_API_TOKEN)


# --- ФУНКЦИИ DB ---

def get_db_connection():
    conn = sqlite3.connect('vpn_bot_users.db', check_same_thread=False) 
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  username TEXT,
                  client_name TEXT,
                  created_date TIMESTAMP,
                  referrer_id INTEGER,
                  referrals_count INTEGER DEFAULT 0,
                  referral_earnings REAL DEFAULT 0,
                  balance REAL DEFAULT 0
                  )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (user_id INTEGER PRIMARY KEY,
                  subscription_start TIMESTAMP,
                  subscription_end TIMESTAMP,
                  status TEXT DEFAULT 'trial',
                  tariff_id INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS payments
                 (payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  amount REAL,
                  currency TEXT,
                  payment_date TIMESTAMP,
                  payment_status TEXT,
                  invoice_id TEXT,
                  tariff_id INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS tariffs
                 (tariff_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT,
                  days INTEGER,
                  price REAL,
                  currency TEXT DEFAULT 'USDT')''')
    
    c.execute("SELECT COUNT(*) FROM tariffs")
    if c.fetchone()[0] == 0:
        tariffs = [
            ('1 день (пробный)', 1, 0.0, 'FREE'),
            ('1 месяц', 30, 2.0, 'USDT'),
            ('3 месяца', 90, 5.0, 'USDT')
        ]
        c.executemany("INSERT INTO tariffs (name, days, price, currency) VALUES (?, ?, ?, ?)", tariffs)
    
    conn.commit()
    conn.close()

init_db()

def process_referral_reward(user_id, amount_paid):
    """Начисляет 5% вознаграждение рефереру"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    
    if row and row['referrer_id']:
        ref_id = row['referrer_id']
        reward = amount_paid * 0.05
        
        cur.execute("""
            UPDATE users 
            SET balance = balance + ?, referral_earnings = referral_earnings + ? 
            WHERE user_id = ?
        """, (reward, reward, ref_id))
        
        try:
            bot.send_message(ref_id, f"🎉 Твой реферал (ID: {user_id}) купил подписку!\nТебе начислено {reward:.2f} USDT на баланс.")
        except Exception as e:
            logging.error(f"Не удалось уведомить реферера {ref_id}: {e}")
            
    conn.commit()
    conn.close()

def create_trial_subscription(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    subscription_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subscription_end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''INSERT OR REPLACE INTO subscriptions 
                 (user_id, subscription_start, subscription_end, status, tariff_id) 
                 VALUES (?, ?, ?, ?, ?)''',
             (user_id, subscription_start, subscription_end, 'trial', 1))
    conn.commit()
    conn.close()

def update_subscription(user_id, tariff_id):
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT days FROM tariffs WHERE tariff_id = ?", (tariff_id,))
    tariff = c.fetchone()
    if not tariff:
        conn.close()
        return False
    
    days = tariff[0]
    
    c.execute("SELECT subscription_end FROM subscriptions WHERE user_id = ?", (user_id,))
    existing = c.fetchone()
    
    now = datetime.now()
    if existing and existing[0]:
        try:
            current_end = datetime.strptime(existing[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            current_end = now
        
        start_time = max(now, current_end)
        new_end = start_time + timedelta(days=days)
    else:
        new_end = now + timedelta(days=days)
    
    new_end_str = new_end.strftime("%Y-%m-%d %H:%M:%S")

    c.execute('''INSERT OR REPLACE INTO subscriptions 
                 (user_id, subscription_start, subscription_end, status, tariff_id) 
                 VALUES (?, datetime('now'), ?, 'active', ?)''',
              (user_id, new_end_str, tariff_id))
    
    conn.commit()
    conn.close()
    return True

def create_payment_invoice(user_id, tariff_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute("SELECT name, days, price, currency FROM tariffs WHERE tariff_id = ?", (tariff_id,))
        tariff = c.fetchone()
        
        if not tariff:
            logging.error("Тариф не найден")
            conn.close()
            return None
        
        name, days, price, currency = tariff
        
        if price == 0.0:
            if update_subscription(user_id, tariff_id):
                conn.close()
                return type('Invoice', (), {'ok': True, 'pay_url': None, 'invoice_id': 'free', 'status': 'paid'})
            else:
                conn.close()
                return None
        
        payload = f"{user_id}_{tariff_id}_{int(time.time())}"
        
        invoice = crypto_client.create_invoice(
            asset=currency,
            amount=str(price),
            description=f"VPN подписка: {name}",
            payload=payload
        )
        
        if not invoice or not invoice.ok:
            logging.error(f"Не удалось создать инвойс: {invoice.error if invoice else 'API Error'}")
            conn.close()
            return invoice 
        
        c.execute('''INSERT INTO payments 
                     (user_id, amount, currency, payment_date, payment_status, invoice_id, tariff_id) 
                     VALUES (?, ?, ?, datetime('now'), 'pending', ?, ?)''',
                  (user_id, price, currency, invoice.invoice_id, tariff_id))
        
        conn.commit()
        conn.close()
        
        return invoice
        
    except Exception as e:
        logging.error(f"Критическая ошибка создания инвойса: {e}")
        return type('Invoice', (), {'ok': False, 'error': 'Internal server error'})

# --- WIREGUARD FUNCTIONS (Заглушки для вашего кода) ---

def generate_client_name(user_id):
    return f"client_{user_id}"

def remove_wireguard_user(client_name):
    logging.info(f"Removing WireGuard user: {client_name}")
    # Вставьте ваш реальный код для удаления пира WireGuard здесь
    return True, f"Пользователь {client_name} удален (заглушка)"

def add_wireguard_user(client_name):
    logging.info(f"Adding WireGuard user: {client_name}")
    # Вставьте ваш реальный код для добавления пира WireGuard здесь
    return True, f"/root/{client_name}.conf"

def get_wireguard_config_content(client_name):
    # Вставьте ваш реальный код для генерации конфига WireGuard здесь
    config_content = (
        "[Interface]\n"
        f"PrivateKey = <PRIVATE_KEY_OF_{client_name}>\n"
        f"Address = 10.7.0.X/24\n"
        "DNS = 8.8.8.8, 8.8.4.4\n"
        "[Peer]\n"
        f"PublicKey = {SERVER_PUBLIC_KEY}\n"
        f"Endpoint = {SERVER_ENDPOINT}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25"
    )
    return config_content

# --- МОНИТОР ПОДПИСОК ---

def subscription_monitor():
    while True:
        try:
            conn = get_db_connection()
            c = conn.cursor()
          
            # 1. Находим пользователей с истекшей подпиской
            c.execute('''SELECT u.user_id, u.client_name, t.price 
                         FROM users u 
                         JOIN subscriptions s ON u.user_id = s.user_id 
                         LEFT JOIN payments p ON s.user_id = p.user_id AND s.tariff_id = p.tariff_id
                         LEFT JOIN tariffs t ON s.tariff_id = t.tariff_id
                         WHERE s.subscription_end < datetime('now') AND s.status != 'expired' ''')
            
            expired_users = c.fetchall()
            
            for user in expired_users:
                # user_id = user['user_id']
                # client_name = user['client_name']
                # amount_paid = user['price']
                # ... (Логика удаления WireGuard и уведомления) ...
                pass 

            conn.commit()
            
            # 2. Проверяем pending платежи
            c.execute("SELECT payment_id, user_id, invoice_id, tariff_id, amount FROM payments WHERE payment_status = 'pending'")
            pending_payments = c.fetchall()
            
            for payment in pending_payments:
                invoices = crypto_client.get_invoices(invoice_ids=payment['invoice_id'])
                if invoices and invoices[0].status == 'paid':
                    if update_subscription(payment['user_id'], payment['tariff_id']):
                        c.execute("UPDATE payments SET payment_status = 'completed' WHERE payment_id = ?", (payment['payment_id'],))
                        
                        process_referral_reward(payment['user_id'], payment['amount']) 
                        
                        bot.send_message(payment['user_id'], "✅ Оплата подтверждена! Ваша подписка активирована.")
                        logging.info(f"Подписка пользователя {payment['user_id']} активирована после оплаты")
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logging.error(f"Ошибка в мониторе подписок: {e}")
        
        time.sleep(60)

# Запуск монитора в отдельном потоке
monitor_thread = threading.Thread(target=subscription_monitor, daemon=True)
monitor_thread.start()

# --- ФУНКЦИИ КЭШИРОВАНИЯ КРИПТОВАЛЮТ ---

def fetch_and_cache_crypto_rates():
    global CRYPTO_CACHE, LAST_UPDATE
    if time.time() - LAST_UPDATE < CACHE_LIFETIME:
        return CRYPTO_CACHE
    
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': 'bitcoin,ethereum,toncoin',
        'vs_currencies': 'usd',
        'include_24hr_change': 'true'
    }
    
    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data:
            CRYPTO_CACHE = data
            LAST_UPDATE = time.time()
            logging.info("Crypto rates updated successfully from CoinGecko.")
            return CRYPTO_CACHE
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching crypto rates: {e}. Returning cached data if available.")
        return CRYPTO_CACHE if CRYPTO_CACHE else None

# --- FLASK API ENDPOINTS ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/user_info', methods=['POST'])
def user_info():
    data = request.json
    user_id = data.get('user_id')
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    sub = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    
    if user:
        sub_end_str = sub['subscription_end'] if sub and sub['subscription_end'] else "Нет"
        
        reg_date = datetime.strptime(user['created_date'], "%Y-%m-%d %H:%M:%S")
        days_with_us = (datetime.now() - reg_date).days

        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}" 

        return jsonify({
            'success': True,
            'balance': user['balance'],
            'referrals': user['referrals_count'],
            'earnings': user['referral_earnings'],
            'sub_end': sub_end_str,
            'username': user['username'],
            'days_with_us': days_with_us,
            'ref_link': ref_link
        })
    return jsonify({'success': False, 'message': 'User not found.'})

@app.route('/api/tariffs', methods=['GET'])
def get_tariffs():
    conn = get_db_connection()
    tariffs = conn.execute("SELECT tariff_id, name, days, price, currency FROM tariffs WHERE price > 0 ORDER BY days").fetchall()
    conn.close()
    
    tariffs_list = [{'id': row['tariff_id'], 'name': row['name'], 'days': row['days'], 'price': row['price'], 'currency': row['currency']} for row in tariffs]
    
    return jsonify({'success': True, 'tariffs': tariffs_list})

@app.route('/api/create_payment', methods=['POST'])
def make_payment():
    data = request.json
    user_id = data.get('user_id')
    tariff_id = data.get('tariff_id')
    
    invoice = create_payment_invoice(user_id, tariff_id) 
    
    if invoice and invoice.ok and invoice.pay_url: 
        return jsonify({'success': True, 'url': invoice.pay_url, 'invoice_id': invoice.invoice_id})
    else:
        error_message = invoice.error if invoice else 'Unknown error'
        logging.error(f"Failed to create payment for user {user_id}: {error_message}")
        return jsonify({'success': False, 'message': f'Ошибка CryptoPay: {error_message}'}), 400

@app.route('/api/crypto_rates', methods=['GET'])
def crypto_rates_endpoint():
    rates = fetch_and_cache_crypto_rates()
    if rates:
        return jsonify({'success': True, 'rates': rates})
    return jsonify({'success': False, 'message': 'Failed to load crypto rates and cache is empty.'}), 500

# --- TELEGRAM BOT HANDLERS ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "Пользователь"
    args = message.text.split()
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    
    if not user:
        referrer_id = None
        if len(args) > 1 and args[1].startswith('ref_') and args[1][4:].isdigit():
            ref_candidate = int(args[1][4:])
            if ref_candidate != user_id and conn.execute("SELECT user_id FROM users WHERE user_id = ?", (ref_candidate,)).fetchone():
                referrer_id = ref_candidate
                conn.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
                
        reg_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO users (user_id, username, referrer_id, created_date) VALUES (?, ?, ?, ?)",
                     (user_id, username, referrer_id, reg_date))
        conn.commit()
        
        create_trial_subscription(user_id)
        
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🌐 Открыть Web App MrdotaVPN", web_app=types.WebAppInfo(url=WEBAPP_URL)))
    
    bot.send_message(message.chat.id, 
                     f"👋 Привет, {username}!\n\nДобро пожаловать в **MrdotaVPN**.\n"
                     "Используйте кнопку ниже для управления VPN и подпиской.",
                     parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == 'get_vpn_config')
def handle_get_config_from_webapp(message):
    user_id = message.from_user.id
    
    conn = get_db_connection()
    sub = conn.execute("SELECT subscription_end FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    is_active = False
    if sub and sub['subscription_end']:
        sub_end_date = datetime.strptime(sub['subscription_end'], "%Y-%m-%d %H:%M:%S")
        if sub_end_date > datetime.now():
            is_active = True

    if not is_active:
        bot.send_message(user_id, "❌ Ваша подписка не активна. Пожалуйста, оплатите подписку в Web App.")
        return
    
    client_name = generate_client_name(user_id)

    success, result_path = add_wireguard_user(client_name)
    
    if success:
        config_content = get_wireguard_config_content(client_name)

        try:
            # QR-код
            qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
            qr.add_data(config_content)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            img_byte_arr = BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            bot.send_photo(user_id, img_byte_arr, caption="🔑 Ваш WireGuard QR-код для настройки. Используйте его в приложении WireGuard.")
            
            # Файл
            file_bytes = BytesIO(config_content.encode('utf-8'))
            file_bytes.name = f'{client_name}.conf'
            bot.send_document(user_id, file_bytes, caption="📄 Ваш WireGuard конфиг-файл.")

        except Exception as e:
            bot.send_message(user_id, f"❌ Произошла ошибка при отправке конфига: {e}. Попробуйте позже.")
    else:
        bot.send_message(user_id, f"❌ Ошибка создания/обновления конфига: {result_path}")


# --- ЗАПУСК ---

def run_flask():
    logging.info(f"Starting Flask server on port {FLASK_PORT}...")
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False)

if __name__ == '__main__':
    logging.info("Starting MrdotaVPN Server and Bot...")
    
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True 
    flask_thread.start()
    
    bot.polling(none_stop=True)
