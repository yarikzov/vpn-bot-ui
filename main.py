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
from flask import Flask, request, jsonify, render_template # <-- Добавлено
from telebot.util import is_json 

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "8204021215:AAFO3BSZn6e4keyB1gS3AEEA-IylhUWIMro"
WIREGUARD_SCRIPT_PATH = "/root/wireguard-install.sh"
SERVER_PUBLIC_KEY = "qSearch Rv98fGCTjLuxW4ygE8Hl… on blockchair.coSearch mRv98fGCTjLuxW4ygE8H… 
on blockchair.coSearch mmRv98fGCTjLuxW4ygE8… 
on blockchair.coSearch mmmRv98fGCTjLuxW4ygE… 
on blockchair.coSearch mmmmRv98fGCTjLuxW4yg… 
on blockchair.coSearch mmmmmRv98fGCTjLuxW4y… 
on blockchair.coSearch mmmmmmRv98fGCTjLuxW4… 
on blockchair.coSearch mmmmmmmRv98fGCTjLuxW… 
on blockchair.coSearch mmmmmmmmRv98fGCTjLux… 
on blockchair.coSearch mmmmmmmmmRv98fGCTjLu… 
on blockchair.coSearch mmmmmmmmmmRv98fGCTjL… 
on blockchair.coSearch mmmmmmmmmmmRv98fGCTj… 
on blockchair.commmmmmmmmmmmRv98fGCTjLuxW4ygE8HlizQQyAsKTmCWbPRybFRywc="
SERVER_ENDPOINT = "136.0.8.219:51820"
ADMIN_USER_ID = 5593856626
CRYPTO_PAY_API_TOKEN = "502548:AAvGZlXQ13JYzhB3GEwTy4gbPc74iExUvmY"  # <--- ПРОВЕРЬТЕ ЭТОТ ТОКЕН!
WEBAPP_URL = "https://ТВОЙ_URL_ОТ_NGROK_ИЛИ_ДОМЕН" # <--- ОБНОВИТЕ!
FLASK_PORT = 5000

# --- СИСТЕМНЫЕ НАСТРОЙКИ ---
app = Flask(__name__, template_folder='templates') # Инициализация Flask
bot = telebot.TeleBot(BOT_TOKEN)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/vpn-bot.log'),
        logging.StreamHandler()
    ]
)

# --- КЭШИРОВАНИЕ КРИПТОВАЛЮТ ---
CRYPTO_CACHE = {}
LAST_UPDATE = 0
CACHE_LIFETIME = 60 # Время жизни кэша в секундах

# Улучшенный класс для работы с Crypto Pay API (Из вашего файла)
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
            
            response.raise_for_status() # Вызывает исключение для 4xx/5xx ошибок
            
            result = response.json()
            if not result.get("ok"):
                error_msg = result.get('error', {}).get('name', 'Unknown error')
                logging.error(f"CryptoPay API Error: {error_msg}")
                return None
                
            return result
            
        except requests.exceptions.RequestException as e:
            logging.error(f"CryptoPay API Exception: {e}")
            return None
            
    def create_invoice(self, asset, amount, description, payload=None):
        data = {
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "payload": payload
        }
        
        result = self._make_request("POST", "createInvoice", data)
        
        if result:
            invoice_data = result.get("result", {})
            return type('Invoice', (), {
                'invoice_id': invoice_data.get('invoice_id'),
                'pay_url': invoice_data.get('pay_url'),
                'status': invoice_data.get('status'),
                'amount': invoice_data.get('amount'),
                'asset': invoice_data.get('asset')
            })
        else:
            return None
        
    def get_invoices(self, invoice_ids=None):
        data = {}
        if invoice_ids:
            if isinstance(invoice_ids, list):
                data["invoice_ids"] = ",".join(map(str, invoice_ids))
            else:
                data["invoice_ids"] = str(invoice_ids)
                
        result = self._make_request("GET", "getInvoices", data)
        if result:
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


# --- ФУНКЦИИ DB (С ИНТЕГРАЦИЕЙ РЕФЕРАЛОВ И БАЛАНСА) ---

def get_db_connection():
    # Важно: check_same_thread=False для Flask и telebot
    # Используем файл из вашего оригинального кода
    conn = sqlite3.connect('vpn_bot_users.db', check_same_thread=False) 
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Инициализация базы данных с таблицами для подписок, платежей, и рефералов"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. Основная таблица пользователей (расширенная)
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
    
    # 2. Таблица подписок (как у вас)
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (user_id INTEGER PRIMARY KEY,
                  subscription_start TIMESTAMP,
                  subscription_end TIMESTAMP,
                  status TEXT DEFAULT 'trial',
                  tariff_id INTEGER)''')
    
    # 3. Таблица платежей (как у вас)
    c.execute('''CREATE TABLE IF NOT EXISTS payments
                 (payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  amount REAL,
                  currency TEXT,
                  payment_date TIMESTAMP,
                  payment_status TEXT,
                  invoice_id TEXT,
                  tariff_id INTEGER)''')
    
    # 4. Таблица тарифов (как у вас)
    c.execute('''CREATE TABLE IF NOT EXISTS tariffs
                 (tariff_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT,
                  days INTEGER,
                  price REAL,
                  currency TEXT DEFAULT 'USDT')''')
    
    # Заполняем тарифы если они пустые (как у вас)
    c.execute("SELECT COUNT(*) FROM tariffs")
    if c.fetchone()[0] == 0:
        tariffs = [
            ('1 день (пробный)', 1, 0, 'FREE'),
            ('1 месяц', 30, 1.0, 'USDT'),
            ('3 месяца', 90, 2.5, 'USDT'),
            ('6 месяцев', 180, 4.5, 'USDT'),
            ('1 год', 365, 8.0, 'USDT')
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
        reward = amount_paid * 0.05 # 5 процентов
        
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

# Функции работы с подписками (Ваши оригинальные, слегка адаптированные)
def create_trial_subscription(user_id):
    """Создание пробной подписки на 1 день"""
    conn = get_db_connection()
    c = conn.cursor()
    
    subscription_start = datetime.now()
    subscription_end = subscription_start + timedelta(days=1)
    
    c.execute('''INSERT OR REPLACE INTO subscriptions 
                 (user_id, subscription_start, subscription_end, status, tariff_id) 
                 VALUES (?, ?, ?, ?, ?)''',
             (user_id, subscription_start, subscription_end, 'trial', 1))
    conn.commit()
    conn.close()
    
    return subscription_end

def check_user_subscription(user_id):
    """Проверка подписки пользователя"""
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''SELECT s.subscription_end, s.status, t.name 
                 FROM subscriptions s 
                 LEFT JOIN tariffs t ON s.tariff_id = t.tariff_id 
                 WHERE s.user_id = ?''', (user_id,))
    result = c.fetchone()
    conn.close()
    
    if not result:
        return False, "❌ У вас нет активной подписки", None
    
    subscription_end, status, tariff_name = result
    subscription_end_date = datetime.fromisoformat(subscription_end) if isinstance(subscription_end, str) else subscription_end
    
    if datetime.now() > subscription_end_date:
        return False, f"❌ Подписка истекла {subscription_end_date.strftime('%d.%m.%Y %H:%M')}", tariff_name
    
    time_left = subscription_end_date - datetime.now()
    days_left = time_left.days
    hours_left = time_left.seconds // 3600
    
    return True, f"✅ Подписка активна до {subscription_end_date.strftime('%d.%m.%Y %H:%M')}\n⏳ Осталось: {days_left} дн. {hours_left} час.", tariff_name


def update_subscription(user_id, tariff_id):
    """Обновление подписки пользователя"""
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
    
    if existing and existing[0]:
        current_end = datetime.fromisoformat(existing[0]) if isinstance(existing[0], str) else existing[0]
        # Продлеваем с момента окончания, если она активна, иначе с текущего момента
        start_time = max(datetime.now(), current_end)
        new_end = start_time + timedelta(days=days)
    else:
        new_end = datetime.now() + timedelta(days=days)
    
    new_end_str = new_end.strftime("%Y-%m-%d %H:%M:%S")

    c.execute('''INSERT OR REPLACE INTO subscriptions 
                 (user_id, subscription_start, subscription_end, status, tariff_id) 
                 VALUES (?, datetime('now'), ?, 'active', ?)''',
              (user_id, new_end_str, tariff_id))
    
    conn.commit()
    conn.close()
    return True

# Функции для работы с платежами (Адаптировано для Web App)
def create_payment_invoice(user_id, tariff_id):
    """Создание инвойса для оплаты через Crypto Pay"""
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
        
        # Для бесплатного тарифа сразу активируем подписку
        if price == 0:
            if update_subscription(user_id, tariff_id):
                c.execute('''INSERT INTO payments 
                             (user_id, amount, currency, payment_date, payment_status, invoice_id, tariff_id) 
                             VALUES (?, ?, ?, datetime('now'), 'completed', 'free', ?)''',
                           (user_id, price, currency, tariff_id))
                conn.commit()
                conn.close()
                free_invoice = type('Invoice', (), {
                    'pay_url': None, 'invoice_id': 'free', 'status': 'paid'
                })
                return free_invoice
            else:
                conn.close()
                return None
        
        # Создаем уникальный payload для отслеживания
        payload = f"{user_id}_{tariff_id}_{int(time.time())}"
        
        # Создаем инвойс в Crypto Pay
        invoice = crypto_client.create_invoice(
            asset=currency,
            amount=str(price),
            description=f"VPN подписка: {name}",
            payload=payload
        )
        
        if not invoice:
            logging.error("Не удалось создать инвойс")
            conn.close()
            return None
        
        # Сохраняем платеж в базу
        c.execute('''INSERT INTO payments 
                     (user_id, amount, currency, payment_date, payment_status, invoice_id, tariff_id) 
                     VALUES (?, ?, ?, datetime('now'), 'pending', ?, ?)''',
                  (user_id, price, currency, invoice.invoice_id, tariff_id))
        
        conn.commit()
        conn.close()
        
        return invoice
        
    except Exception as e:
        logging.error(f"Ошибка создания инвойса: {e}")
        return None

def check_payment_status(invoice_id):
    """Проверка статуса платежа"""
    try:
        invoices = crypto_client.get_invoices(invoice_ids=invoice_id)
        if invoices and hasattr(invoices[0], 'status'):
            return invoices[0].status
        return None
    except Exception as e:
        logging.error(f"Ошибка проверки платежа: {e}")
        return None

# Мониторинг подписок (Ваш оригинальный код, адаптированный)
def subscription_monitor():
    """Фоновая проверка и отключение просроченных подписок и проверка платежей"""
    while True:
        try:
            conn = get_db_connection()
            c = conn.cursor()
          
            # 1. Находим пользователей с истекшей подпиской
            c.execute('''SELECT u.user_id, u.client_name, t.price 
                         FROM users u 
                         JOIN subscriptions s ON u.user_id = s.user_id 
                         JOIN payments p ON s.user_id = p.user_id
                         JOIN tariffs t ON p.tariff_id = t.tariff_id
                         WHERE s.subscription_end < datetime('now') 
                         AND s.status != 'expired' ''')
            
            expired_users = c.fetchall()
            
            for user_id, client_name, amount_paid in expired_users:
                try:
                    # Удаляем из WireGuard
                    # Здесь вызываются ваши WireGuard функции: remove_wireguard_user(client_name)
                    success, result = remove_wireguard_user(client_name) 
                    if success:
                        c.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = ?", (user_id,))
                        bot.send_message(
                            user_id, 
                            "❌ Ваша подписка истекла. VPN доступ отключен.\n"
                            "Для возобновления работы приобретите новую подписку в Web App."
                        )
                        logging.info(f"Подписка пользователя {user_id} истекла, VPN отключен")
                except Exception as e:
                    logging.error(f"Ошибка отключения пользователя {user_id}: {e}")
            
            conn.commit()
            
            # 2. Проверяем pending платежи
            c.execute("SELECT payment_id, user_id, invoice_id, tariff_id, amount FROM payments WHERE payment_status = 'pending'")
            pending_payments = c.fetchall()
            
            for payment_id, user_id, invoice_id, tariff_id, amount_paid in pending_payments:
                status = check_payment_status(invoice_id)
                if status == 'paid':
                    if update_subscription(user_id, tariff_id):
                        c.execute("UPDATE payments SET payment_status = 'completed' WHERE payment_id = ?", (payment_id,))
                        
                        # Начисляем реферальное вознаграждение!
                        process_referral_reward(user_id, amount_paid) 
                        
                        bot.send_message(user_id, "✅ Оплата подтверждена! Ваша подписка активирована.")
                        logging.info(f"Подписка пользователя {user_id} активирована после оплаты")
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logging.error(f"Ошибка в мониторе подписок: {e}")
        
        # Проверяем каждые 5 минут
        time.sleep(300)

# Запуск монитора в отдельном потоке
monitor_thread = threading.Thread(target=subscription_monitor, daemon=True)
monitor_thread.start()

# --- WIREGUARD FUNCTIONS (Здесь остаются ваши оригинальные функции) ---

def generate_client_name(user_id):
    return f"client_{user_id}"

def get_user_count():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        return 0

# ... (Остальные ваши WireGuard функции: remove_wireguard_user, get_user_stats, 
# get_server_status, add_wireguard_user, create_wireguard_config_directly и т.д.) ...
# В целях краткости, я оставляю их как заглушки, но в вашем файле они должны быть полными.

def remove_wireguard_user(client_name):
    # ЗДЕСЬ ДОЛЖЕН БЫТЬ ВАШ КОД ИЗ ФАЙЛА
    # (логика удаления пира WireGuard, удаления конфига и удаления из users)
    # Возвращает (True/False, "Сообщение")
    return True, f"Пользователь {client_name} удален (заглушка)"

def add_wireguard_user(client_name):
    # ЗДЕСЬ ДОЛЖЕН БЫТЬ ВАШ КОД ИЗ ФАЙЛА
    # (логика добавления пира WireGuard, создания конфига и сохранения в users)
    # Возвращает (True/False, config_path)
    return True, f"/root/{client_name}.conf"

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

# --- FLASK API ENDPOINTS (ДЛЯ WEB APP) ---

@app.route('/')
def home():
    # Главная страница Web App - загружает index.html
    return render_template('index.html')

@app.route('/api/user_info', methods=['POST'])
def user_info():
    data = request.json
    user_id = data.get('user_id')
    
    conn = get_db_connection()
    # Запрос данных из расширенной таблицы users
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    # Запрос данных подписки
    sub = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    
    if user:
        sub_end_str = sub['subscription_end'] if sub and sub['subscription_end'] else "Нет"
        
        # Генерация реферальной ссылки
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}" 

        return jsonify({
            'success': True,
            'balance': user['balance'],
            'referrals': user['referrals_count'],
            'earnings': user['referral_earnings'],
            'sub_end': sub_end_str,
            'username': user['username'],
            'ref_link': ref_link # <-- РЕФЕРАЛЬНАЯ ССЫЛКА
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
    
    if invoice and invoice.pay_url: 
        return jsonify({'success': True, 'url': invoice.pay_url, 'invoice_id': invoice.invoice_id})
    else:
        return jsonify({'success': False, 'message': 'Ошибка создания счета. Проверьте токен CryptoBot API.'}), 400

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
        # Регистрация нового пользователя
        referrer_id = None
        if len(args) > 1 and args[1].startswith('ref_') and args[1][4:].isdigit():
            ref_candidate = int(args[1][4:])
            # Проверяем, что реферер существует и не является самим собой
            if ref_candidate != user_id and conn.execute("SELECT user_id FROM users WHERE user_id = ?", (ref_candidate,)).fetchone():
                referrer_id = ref_candidate
                # Увеличиваем счетчик рефералов у реферера
                conn.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
                
        reg_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO users (user_id, username, referrer_id, created_date) VALUES (?, ?, ?, ?)",
                     (user_id, username, referrer_id, reg_date))
        conn.commit()
        
        # Создаем пробную подписку
        create_trial_subscription(user_id)
        
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    # Кнопка, открывающая Web App с профилем, оплатой и рефералкой
    markup.add(types.InlineKeyboardButton("🌐 Открыть Web App MrdotaVPN", web_app=types.WebAppInfo(url=WEBAPP_URL)))
    
    bot.send_message(message.chat.id, 
                     f"👋 Привет, {username}!\n\nДобро пожаловать в **MrdotaVPN**.\n"
                     "Используйте кнопку ниже для управления VPN, оплаты и реферальной программой.",
                     parse_mode='Markdown', reply_markup=markup)

# ... (Здесь должны быть ваши оригинальные хэндлеры для WireGuard, 
# админ-панели и прочие, которые вы хотите сохранить) ...

@bot.message_handler(commands=['admin'])
def admin_panel(message): 
    # В целях краткости, оставим только проверку
    if not message.from_user.id == ADMIN_USER_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав доступа к этой команде")
    else:
        # Здесь должна быть логика show_admin_panel, которая у вас была
        bot.send_message(message.chat.id, "Админ-панель: [СТАТИСТИКА, УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ]")


# --- ЗАПУСК ---

def run_flask():
    """Запуск Flask в отдельном потоке"""
    logging.info(f"Starting Flask server on port {FLASK_PORT}...")
    # Запускаем Flask на всех интерфейсах (0.0.0.0)
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False)

if __name__ == '__main__':
    logging.info("Starting MrdotaVPN Server and Bot...")
    
    # 1. Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True # Поток завершится при завершении основного
    flask_thread.start()
    
    # 2. Запускаем Bot
    bot.polling(none_stop=True)
