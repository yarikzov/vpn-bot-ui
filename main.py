# -*- coding: utf-8 -*-
import telebot
from telebot import types
from flask import Flask, request, jsonify, render_template
import sqlite3
import requests
import threading
import time
import logging
from datetime import datetime, timedelta
import json

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "" 
CRYPTO_PAY_TOKEN = "469810:AAD9NszRx10wOih6coLQc1leKhdwcR6n4SR" # <--- ПРОВЕРЬТЕ ЭТОТ ТОКЕН!
# ВАЖНО: Сюда вставь свой HTTPS URL от ngrok или домена
WEBAPP_URL = "https://ТВОЙ_URL_ОТ_NGROK_ИЛИ_ДОМЕН" 
ADMIN_ID = 5593856626

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- КЭШИРОВАНИЕ КРИПТОВАЛЮТ ---
CRYPTO_CACHE = {}
LAST_UPDATE = 0
CACHE_LIFETIME = 60 # Время жизни кэша в секундах (60 секунд)

# --- БАЗА ДАННЫХ ---
def get_db_connection():
    # Важно: check_same_thread=False для Flask и telebot
    conn = sqlite3.connect('mrdotavpn.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance REAL DEFAULT 0,
        referrer_id INTEGER,
        referrals_count INTEGER DEFAULT 0,
        referral_earnings REAL DEFAULT 0,
        subscription_end TEXT,
        reg_date TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS payments (
        invoice_id INTEGER PRIMARY KEY,
        user_id INTEGER,
        amount REAL,
        status TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# --- ФУНКЦИИ ОПЛАТЫ И ЛОГИРОВАНИЕ ОШИБОК ---

def create_invoice(user_id, amount):
    url = 'https://pay.crypt.bot/api/createInvoice'
    # Используем токен, который ты предоставил
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    payload = str(int(time.time())) + str(user_id) 
    data = {
        'asset': 'USDT',
        'amount': str(amount),
        'description': f'MrdotaVPN Subscription for {user_id}',
        'payload': payload,
        'allow_comments': False
    }
    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status() # Вызывает ошибку для HTTP 4xx/5xx
        json_data = response.json()
        
        if json_data.get('ok'):
            invoice_id = json_data['result']['invoice_id']
            # Сохраняем инвойс в базу
            conn = get_db_connection()
            conn.execute("INSERT INTO payments (invoice_id, user_id, amount, status) VALUES (?, ?, ?, ?)",
                         (invoice_id, user_id, amount, 'pending'))
            conn.commit()
            conn.close()
            
            return json_data['result']['bot_invoice_url']
        else:
            # ЛОГИРОВАНИЕ КРИТИЧЕСКОЙ ОШИБКИ С API CRYPTOBOT
            error_message = json_data.get('error', 'Unknown CryptoBot API error')
            logging.error(f"CryptoBot API Error for user {user_id}: {error_message}")
            logging.error(f"Full response: {json_data}")
            return {'error': error_message}
            
    except requests.exceptions.HTTPError as e:
        error_message = f"HTTP Error: {e.response.status_code}. Проверьте токен CryptoPay!"
        logging.error(error_message)
        return {'error': error_message}
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error connecting to CryptoBot API: {e}")
        return {'error': 'Ошибка сети при подключении к CryptoBot.'}
    except Exception as e:
        logging.error(f"Unexpected error in create_invoice: {e}")
        return {'error': 'Непредвиденная ошибка сервера.'}

# --- КЭШИРОВАНИЕ КРИПТОВАЛЮТ ---

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

# --- API ENDPOINTS (ДЛЯ WEB APP) ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/user_info', methods=['POST'])
def user_info():
    # ... (логика осталась та же)
    data = request.json
    user_id = data.get('user_id')
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    
    if user:
        reg_date = datetime.strptime(user['reg_date'], "%Y-%m-%d %H:%M:%S")
        days_with_us = (datetime.now() - reg_date).days
        
        return jsonify({
            'success': True,
            'balance': user['balance'],
            'referrals': user['referrals_count'],
            'earnings': user['referral_earnings'],
            'sub_end': user['subscription_end'] if user['subscription_end'] else "Не активна",
            'days_with_us': days_with_us,
            'username': user['username'],
            'ref_link': f"https://t.me/{bot.get_me().username}?start={user_id}"
        })
    return jsonify({'success': False})

@app.route('/api/create_payment', methods=['POST'])
def make_payment():
    data = request.json
    user_id = data.get('user_id')
    price = data.get('price')
    
    result = create_invoice(user_id, price) 
    
    if isinstance(result, str): # Успех, вернулась ссылка
        return jsonify({'success': True, 'url': result})
    else: # Ошибка, вернулся словарь с ошибкой
        return jsonify({'success': False, 'message': result.get('error', 'Неизвестная ошибка')}), 400

@app.route('/api/crypto_rates', methods=['GET'])
def crypto_rates_endpoint():
    rates = fetch_and_cache_crypto_rates()
    if rates:
        return jsonify({'success': True, 'rates': rates})
    return jsonify({'success': False, 'message': 'Failed to load crypto rates and cache is empty.'}), 500

# --- TELEGRAM BOT LOGIC ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "Пользователь"
    args = message.text.split()
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    
    if not user:
        referrer_id = None
        if len(args) > 1 and args[1].isdigit():
            ref_candidate = int(args[1])
            if ref_candidate != user_id:
                referrer_id = ref_candidate
                conn.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
        
        reg_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO users (user_id, username, referrer_id, reg_date) VALUES (?, ?, ?, ?)",
                     (user_id, username, referrer_id, reg_date))
        conn.commit()
        
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🌐 Открыть MrdotaVPN", web_app=types.WebAppInfo(url=WEBAPP_URL)))
    
    bot.send_message(message.chat.id, 
                     f"👋 Привет, {username}!\n\nДобро пожаловать в **MrdotaVPN**.\nЖми кнопку ниже.",
                     parse_mode='Markdown', reply_markup=markup)

# --- ЗАПУСК ---
def run_flask():
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    logging.info("Starting MrdotaVPN Server...")
    t = threading.Thread(target=run_flask)
    t.start()
    bot.polling(none_stop=True)
