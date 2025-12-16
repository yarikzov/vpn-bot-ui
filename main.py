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

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8204021215:AAFO3BSZn6e4keyB1gS3AEEA-IylhUWIMro" 
CRYPTO_PAY_TOKEN = "469810:AAD9NszRx10wOih6coLQc1leKhdwcR6n4SR" 
# Сюда вставь свой URL от ngrok (https://....)
WEBAPP_URL = "https://ТВОЙ_URL_ОТ_NGROK_ИЛИ_ДОМЕН" 
ADMIN_ID = 5593856626

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('mrdotavpn.db', check_same_thread=False)
    cur = conn.cursor()
    # Таблица пользователей
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
    # Таблица платежей
    cur.execute('''CREATE TABLE IF NOT EXISTS payments (
        invoice_id INTEGER PRIMARY KEY,
        user_id INTEGER,
        amount REAL,
        status TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# --- ПОЛЕЗНЫЕ ФУНКЦИИ ---

def get_db_connection():
    conn = sqlite3.connect('mrdotavpn.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def add_days_to_sub(user_id, days):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    
    now = datetime.now()
    if row and row['subscription_end']:
        try:
            current_end = datetime.strptime(row['subscription_end'], "%Y-%m-%d %H:%M:%S")
            if current_end < now:
                current_end = now
        except:
            current_end = now
    else:
        current_end = now
        
    new_end = current_end + timedelta(days=days)
    new_end_str = new_end.strftime("%Y-%m-%d %H:%M:%S")
    
    cur.execute("UPDATE users SET subscription_end = ? WHERE user_id = ?", (new_end_str, user_id))
    conn.commit()
    conn.close()
    return new_end_str

def process_referral_reward(user_id, amount_paid):
    """Начисляет 5% рефереру"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Ищем, кто пригласил этого пользователя
    cur.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    
    if row and row['referrer_id']:
        ref_id = row['referrer_id']
        reward = amount_paid * 0.05 # 5 процентов
        
        # Начисляем награду
        cur.execute("""
            UPDATE users 
            SET balance = balance + ?, referral_earnings = referral_earnings + ? 
            WHERE user_id = ?
        """, (reward, reward, ref_id))
        
        try:
            bot.send_message(ref_id, f"🎉 Твой реферал купил подписку! Тебе начислено {reward:.2f} USDT")
        except:
            pass
            
    conn.commit()
    conn.close()

# --- CRYPTO BOT API ---
def create_invoice(user_id, amount):
    url = 'https://pay.crypt.bot/api/createInvoice'
    headers = {'Crypto-Pay-API-Token': CRYPTO_PAY_TOKEN}
    # payload уникален для каждого чека
    payload = str(int(time.time())) + str(user_id) 
    data = {
        'asset': 'USDT',
        'amount': str(amount),
        'description': f'MrdotaVPN Subscription for {user_id}',
        'payload': payload,
        'allow_comments': False
    }
    try:
        response = requests.post(url, json=data, headers=headers).json()
        if response['ok']:
            invoice_id = response['result']['invoice_id']
            # Сохраняем инвойс в базу
            conn = get_db_connection()
            conn.execute("INSERT INTO payments (invoice_id, user_id, amount, status) VALUES (?, ?, ?, ?)",
                         (invoice_id, user_id, amount, 'pending'))
            conn.commit()
            conn.close()
            
            return response['result']['bot_invoice_url']
    except Exception as e:
        print(f"Invoice Error: {e}")
    return None

# --- API ENDPOINTS (ДЛЯ WEB APP) ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/user_info', methods=['POST'])
def user_info():
    data = request.json
    user_id = data.get('user_id')
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    
    if user:
        # Считаем сколько дней с нами
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
    
    link = create_invoice(user_id, price)
    if link:
        return jsonify({'success': True, 'url': link})
    return jsonify({'success': False, 'message': 'Ошибка создания счета'})

# --- WEBHOOK ДЛЯ CRYPTO BOT (АВТО-ОПЛАТА) ---
# Чтобы это работало, нужно в @CryptoBot настроить Webhook на https://твои-домен/webhook/crypto
@app.route('/webhook/crypto', methods=['POST'])
def crypto_webhook():
    data = request.json
    if data.get('update_type') == 'invoice_paid':
        invoice = data['payload'] # данные чека
        invoice_id = invoice['invoice_id']
        amount = float(invoice['amount'])
        # payload, который мы передавали (timestamp+userid) можно распарсить, но у нас есть таблица payments
        
        conn = get_db_connection()
        payment = conn.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchone()
        
        if payment and payment['status'] == 'pending':
            user_id = payment['user_id']
            
            # 1. Обновляем статус платежа
            conn.execute("UPDATE payments SET status = 'paid' WHERE invoice_id = ?", (invoice_id,))
            
            # 2. Выдаем подписку (например, 30 дней за 2 доллара)
            days = 30 if amount < 4 else 90 # Пример логики
            add_days_to_sub(user_id, days)
            
            # 3. Начисляем реферальные (5%)
            process_referral_reward(user_id, amount)
            
            conn.commit()
            bot.send_message(user_id, "✅ Оплата прошла успешно! Подписка активирована.")
            
        conn.close()
    return 'ok', 200

# --- TELEGRAM BOT LOGIC ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    username = message.from_user.username or "User"
    args = message.text.split()
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    
    if not user:
        # Регистрация
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
                     f"👋 Привет, {username}!\n\nДобро пожаловать в **MrdotaVPN**.\nЛучший VPN с Web3 оплатой и партнерской программой.",
                     parse_mode='Markdown', reply_markup=markup)

# --- ЗАПУСК ---
def run_flask():
    app.run(host='0.0.0.0', port=5000)

if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.start()
    bot.polling(none_stop=True)
