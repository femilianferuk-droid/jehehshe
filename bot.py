import asyncio
import logging
import sqlite3
import re
import json
import requests
import aiohttp
from datetime import datetime
from typing import Optional, Dict, List
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from pyrogram import Client
from dotenv import load_dotenv
import os

# Загрузка переменных окружения
load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
CRYPTOBOT_TOKEN = os.getenv('CRYPTOBOT_TOKEN', '452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX')
API_ID = int(os.getenv('API_ID', 32480523))
API_HASH = os.getenv('API_HASH', '147839735c9fa4e83451209e9b55cfc5')
ADMIN_ID = int(os.getenv('ADMIN_ID', 7973988177))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния для FSM
class AdminStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_account_price = State()
    waiting_account_country = State()

class PaymentStates(StatesGroup):
    waiting_payment_method = State()
    waiting_payment_confirmation = State()

# Класс для получения актуальных курсов валют
class CurrencyRates:
    def __init__(self):
        self.usd_to_rub = 90
        self.ton_to_usd = 5.5
        self.last_update = None
    
    async def update_rates(self):
        """Обновление курсов валют"""
        try:
            # Получаем курс USD/RUB с Центробанка РФ
            async with aiohttp.ClientSession() as session:
                # Курс USD к RUB
                async with session.get('https://www.cbr-xml-daily.ru/daily_json.js') as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.usd_to_rub = float(data['Valute']['USD']['Value'])
                
                # Курс TON к USD с CoinGecko
                async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd') as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.ton_to_usd = float(data['the-open-network']['usd'])
                
                self.last_update = datetime.now()
                logger.info(f"Rates updated: USD/RUB={self.usd_to_rub}, TON/USD={self.ton_to_usd}")
        except Exception as e:
            logger.error(f"Error updating rates: {e}")
    
    async def get_usd_to_rub(self):
        """Получить курс USD к RUB"""
        if not self.last_update or (datetime.now() - self.last_update).seconds > 3600:
            await self.update_rates()
        return self.usd_to_rub
    
    async def get_ton_to_usd(self):
        """Получить курс TON к USD"""
        if not self.last_update or (datetime.now() - self.last_update).seconds > 3600:
            await self.update_rates()
        return self.ton_to_usd
    
    async def rub_to_usd(self, rub_amount: float) -> float:
        rate = await self.get_usd_to_rub()
        return round(rub_amount / rate, 2)
    
    async def rub_to_ton(self, rub_amount: float) -> float:
        usd_amount = await self.rub_to_usd(rub_amount)
        ton_rate = await self.get_ton_to_usd()
        return round(usd_amount / ton_rate, 4)
    
    async def rub_to_usdt(self, rub_amount: float) -> float:
        return await self.rub_to_usd(rub_amount)

# Класс для работы с Crypto Bot API
class CryptoBotAPI:
    def __init__(self, token):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
    
    async def get_me(self):
        """Получение информации о боте"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Crypto-Pay-API-Token": self.token}
                async with session.get(f"{self.base_url}/getMe", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('result')
            return None
        except Exception as e:
            logger.error(f"Error getting me: {e}")
            return None
    
    async def create_invoice(self, amount: float, currency: str, description: str, payload: str = None):
        """Создание счета в Crypto Bot"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Crypto-Pay-API-Token": self.token,
                    "Content-Type": "application/json"
                }
                data = {
                    "asset": currency.upper(),
                    "amount": str(amount),
                    "description": description[:128],
                    "paid_btn_name": "openBot",
                    "paid_btn_url": f"https://t.me/{(await bot.me()).username}",
                    "payload": payload or f"order_{int(datetime.now().timestamp())}"
                }
                
                async with session.post(f"{self.base_url}/createInvoice", headers=headers, json=data) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get('result')
                    else:
                        error_text = await resp.text()
                        logger.error(f"Crypto Bot API error: {error_text}")
                        return None
        except Exception as e:
            logger.error(f"Error creating invoice: {e}")
            return None
    
    async def get_invoice_status(self, invoice_id: str):
        """Получение статуса счета"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Crypto-Pay-API-Token": self.token}
                params = {"invoice_ids": invoice_id}
                
                async with session.get(f"{self.base_url}/getInvoices", headers=headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        invoices = data.get('result', {}).get('items', [])
                        if invoices:
                            return invoices[0].get('status')
            return None
        except Exception as e:
            logger.error(f"Error checking invoice: {e}")
            return None

# Инициализация API
currency_rates = CurrencyRates()
crypto_api = CryptoBotAPI(CRYPTOBOT_TOKEN)

# Класс для работы с базой данных
class Database:
    def __init__(self, db_name='shop.db'):
        self.db_name = db_name
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица аккаунтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    session_string TEXT,
                    country TEXT DEFAULT 'Unknown',
                    price_rub INTEGER DEFAULT 100,
                    is_active INTEGER DEFAULT 1,
                    is_sold INTEGER DEFAULT 0,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sold_date TIMESTAMP
                )
            ''')
            
            # Таблица заказов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    amount_usd REAL,
                    amount_ton REAL,
                    amount_usdt REAL,
                    currency TEXT,
                    crypto_invoice_id TEXT,
                    crypto_pay_url TEXT,
                    status TEXT DEFAULT 'pending',
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    paid_date TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts (id)
                )
            ''')
            
            # Таблица выданных аккаунтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    order_id INTEGER NOT NULL,
                    issued_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts (id),
                    FOREIGN KEY (order_id) REFERENCES orders (id)
                )
            ''')
            
            # Таблица стран
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS countries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    code TEXT UNIQUE NOT NULL
                )
            ''')
            
            # Добавляем популярные страны
            countries = [
                ('Россия', 'RU'), ('Украина', 'UA'), ('Казахстан', 'KZ'),
                ('Беларусь', 'BY'), ('США', 'US'), ('Великобритания', 'GB'),
                ('Германия', 'DE'), ('Франция', 'FR'), ('Италия', 'IT'),
                ('Испания', 'ES'), ('Китай', 'CN'), ('Индия', 'IN'),
                ('Virtual', 'VN'), ('Unknown', 'UN')
            ]
            
            for name, code in countries:
                try:
                    cursor.execute('INSERT OR IGNORE INTO countries (name, code) VALUES (?, ?)', (name, code))
                except:
                    pass
            
            conn.commit()
    
    def add_account(self, phone: str, session_string: str, country: str = "Unknown", price_rub: int = 100) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO accounts (phone, session_string, country, price_rub) VALUES (?, ?, ?, ?)',
                (phone, session_string, country, price_rub)
            )
            conn.commit()
            return cursor.lastrowid
    
    def update_account_country(self, account_id: int, country: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE accounts SET country = ? WHERE id = ?', (country, account_id))
            conn.commit()
    
    def update_account_price(self, account_id: int, price_rub: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE accounts SET price_rub = ? WHERE id = ?', (price_rub, account_id))
            conn.commit()
    
    def get_available_accounts(self, country: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if country and country != "Все":
                cursor.execute('''
                    SELECT * FROM accounts 
                    WHERE is_active = 1 AND is_sold = 0 AND country = ?
                    ORDER BY price_rub ASC
                ''', (country,))
            else:
                cursor.execute('''
                    SELECT * FROM accounts 
                    WHERE is_active = 1 AND is_sold = 0
                    ORDER BY price_rub ASC
                ''')
            return cursor.fetchall()
    
    def get_account_by_id(self, account_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM accounts WHERE id = ?', (account_id,))
            return cursor.fetchone()
    
    def get_countries_with_accounts(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT country, COUNT(*) as count 
                FROM accounts 
                WHERE is_active = 1 AND is_sold = 0 
                GROUP BY country
                ORDER BY count DESC
            ''')
            return cursor.fetchall()
    
    def get_all_countries(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT name FROM countries ORDER BY name')
            return [row['name'] for row in cursor.fetchall()]
    
    def create_order(self, user_id: int, account_id: int, amount_rub: int, currency: str, invoice_id: str, pay_url: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Получаем курсы для конвертации
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            usd_amount = loop.run_until_complete(currency_rates.rub_to_usd(amount_rub))
            ton_amount = loop.run_until_complete(currency_rates.rub_to_ton(amount_rub))
            
            cursor.execute('''
                INSERT INTO orders 
                (user_id, account_id, amount_rub, amount_usd, amount_ton, amount_usdt, currency, crypto_invoice_id, crypto_pay_url, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (user_id, account_id, amount_rub, usd_amount, ton_amount, usd_amount, currency, invoice_id, pay_url))
            
            order_id = cursor.lastrowid
            conn.commit()
            return order_id
    
    def confirm_order(self, invoice_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Обновляем статус заказа
            cursor.execute('''
                UPDATE orders 
                SET status = 'paid', paid_date = CURRENT_TIMESTAMP 
                WHERE crypto_invoice_id = ?
            ''', (invoice_id,))
            
            # Получаем информацию о заказе
            cursor.execute('''
                SELECT o.id, o.user_id, o.account_id 
                FROM orders o 
                WHERE o.crypto_invoice_id = ?
            ''', (invoice_id,))
            
            order = cursor.fetchone()
            if order:
                # Помечаем аккаунт как проданный
                cursor.execute('UPDATE accounts SET is_sold = 1, sold_date = CURRENT_TIMESTAMP WHERE id = ?', (order['account_id'],))
                
                # Добавляем запись в user_accounts
                cursor.execute('''
                    INSERT INTO user_accounts (user_id, account_id, order_id) 
                    VALUES (?, ?, ?)
                ''', (order['user_id'], order['account_id'], order['id']))
                
                conn.commit()
                return order
            return None
    
    def get_order_by_invoice(self, invoice_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM orders WHERE crypto_invoice_id = ?', (invoice_id,))
            return cursor.fetchone()
    
    def get_user_purchases(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT a.*, o.amount_rub, o.currency, o.paid_date 
                FROM user_accounts ua
                JOIN accounts a ON ua.account_id = a.id
                JOIN orders o ON ua.order_id = o.id
                WHERE ua.user_id = ?
                ORDER BY ua.issued_date DESC
            ''', (user_id,))
            return cursor.fetchall()
    
    def get_statistics(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            total_accounts = cursor.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
            available_accounts = cursor.execute('SELECT COUNT(*) FROM accounts WHERE is_active = 1 AND is_sold = 0').fetchone()[0]
            sold_accounts = cursor.execute('SELECT COUNT(*) FROM accounts WHERE is_sold = 1').fetchone()[0]
            
            total_orders = cursor.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
            paid_orders = cursor.execute('SELECT COUNT(*) FROM orders WHERE status = "paid"').fetchone()[0]
            
            total_revenue = cursor.execute('SELECT SUM(amount_rub) FROM orders WHERE status = "paid"').fetchone()[0] or 0
            
            return {
                'total_accounts': total_accounts,
                'available_accounts': available_accounts,
                'sold_accounts': sold_accounts,
                'total_orders': total_orders,
                'paid_orders': paid_orders,
                'total_revenue': total_revenue
            }

# Инициализация базы данных
db = Database()

# Словарь для хранения временных данных авторизации
temp_auth_data: Dict[str, dict] = {}

# Клавиатуры
def get_start_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🛍 Купить аккаунт", callback_data="buy_account"),
        InlineKeyboardButton(text="📋 Мои покупки", callback_data="my_purchases"),
        InlineKeyboardButton(text="ℹ️ О магазине", callback_data="about")
    )
    builder.adjust(1)
    return builder.as_markup()

def get_countries_keyboard():
    builder = InlineKeyboardBuilder()
    countries = db.get_countries_with_accounts()
    
    builder.add(InlineKeyboardButton(text="🌍 Все страны", callback_data="country_Все"))
    
    for country, count in countries:
        builder.add(InlineKeyboardButton(
            text=f"{country} ({count})", 
            callback_data=f"country_{country}"
        ))
    
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    builder.adjust(1)
    return builder.as_markup()

def get_accounts_keyboard(accounts: list, page: int = 0, items_per_page: int = 5):
    builder = InlineKeyboardBuilder()
    
    start = page * items_per_page
    end = start + items_per_page
    accounts_page = accounts[start:end]
    
    for account in accounts_page:
        # Определяем эмодзи для страны
        country_emoji = {
            'Россия': '🇷🇺', 'Украина': '🇺🇦', 'Казахстан': '🇰🇿',
            'Беларусь': '🇧🇾', 'США': '🇺🇸', 'Virtual': '🌐'
        }.get(account['country'], '🌍')
        
        button_text = f"{country_emoji} {account['country']} | {account['price_rub']}₽"
        builder.add(InlineKeyboardButton(
            text=button_text,
            callback_data=f"account_{account['id']}"
        ))
    
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"page_{page-1}"))
    if end < len(accounts):
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"page_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.add(InlineKeyboardButton(text="🔙 Назад к странам", callback_data="back_to_countries"))
    builder.adjust(1)
    return builder.as_markup()

def get_payment_methods_keyboard(account_id: int):
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="💎 TON", callback_data=f"pay_ton_{account_id}"),
        InlineKeyboardButton(text="💵 USDT", callback_data=f"pay_usdt_{account_id}"),
        InlineKeyboardButton(text="💰 Crypto Bot", callback_data=f"pay_crypto_{account_id}")
    )
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_account_{account_id}"))
    builder.adjust(1)
    return builder.as_markup()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="admin_add"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton(text="💰 Изменить цену", callback_data="admin_change_price"),
        InlineKeyboardButton(text="🌍 Изменить страну", callback_data="admin_change_country"),
        InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="admin_list"),
        InlineKeyboardButton(text="📈 Продажи", callback_data="admin_sales")
    )
    builder.adjust(1)
    return builder.as_markup()

def get_back_to_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_to_main"))
    return builder.as_markup()

# Обработчики пользовательских команд
@dp.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = """
🌟 <b>Добро пожаловать в Telegram Account Shop!</b> 🌟

Здесь вы можете купить готовые Telegram аккаунты.

<b>📌 Что мы предлагаем:</b>
• Аккаунты из разных стран
• Мгновенная выдача после оплаты
• Поддержка TON, USDT, Crypto Bot
• Актуальные курсы валют

<b>🛍 Как купить:</b>
1. Выберите страну аккаунта
2. Выберите подходящий аккаунт
3. Оплатите удобным способом
4. Получите данные для входа

Нажмите "Купить аккаунт" для начала!
    """
    
    await message.answer(
        welcome_text,
        reply_markup=get_start_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    about_text = """
ℹ️ <b>О магазине</b>

Мы предоставляем качественные Telegram аккаунты для различных целей.

<b>✅ Преимущества:</b>
• Все аккаунты проверены
• Мгновенная автоматическая выдача
• Поддержка криптовалют
• Честные цены
• Поддержка 24/7

<b>💳 Оплата:</b>
• TON (по курсу)
• USDT (TRC-20)
• Crypto Bot

<b>📞 Поддержка:</b> @admin
    """
    
    await callback.message.edit_text(
        about_text,
        reply_markup=get_back_to_menu_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "my_purchases")
async def my_purchases(callback: CallbackQuery):
    user_id = callback.from_user.id
    purchases = db.get_user_purchases(user_id)
    
    if not purchases:
        await callback.message.edit_text(
            "📭 <b>У вас пока нет покупок</b>\n\n"
            "Нажмите 'Купить аккаунт' чтобы сделать первый заказ!",
            reply_markup=get_back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = "📋 <b>Ваши покупки:</b>\n\n"
    for purchase in purchases:
        text += f"📱 <code>{purchase['phone'][:6]}****{purchase['phone'][-4:]}</code> | {purchase['amount_rub']}₽ | {purchase['paid_date'][:10]}\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_back_to_menu_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_account")
async def buy_account(callback: CallbackQuery):
    countries = db.get_countries_with_accounts()
    
    if not countries:
        await callback.message.edit_text(
            "😔 <b>К сожалению, сейчас нет доступных аккаунтов</b>\n\n"
            "Попробуйте позже.",
            reply_markup=get_back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = "🌍 <b>Выберите страну:</b>\n\n"
    await callback.message.edit_text(
        text,
        reply_markup=get_countries_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("country_"))
async def show_accounts_by_country(callback: CallbackQuery):
    country = callback.data.replace("country_", "")
    
    if country == "Все":
        accounts = db.get_available_accounts()
    else:
        accounts = db.get_available_accounts(country)
    
    if not accounts:
        await callback.message.edit_text(
            f"😔 <b>Нет доступных аккаунтов для страны {country}</b>\n\n"
            "Попробуйте выбрать другую страну.",
            reply_markup=get_countries_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = f"📱 <b>Доступные аккаунты ({len(accounts)}):</b>\n\n"
    await callback.message.edit_text(
        text,
        reply_markup=get_accounts_keyboard(accounts, 0),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("page_"))
async def paginate_accounts(callback: CallbackQuery):
    page = int(callback.data.split("_")[1])
    
    # Получаем текущую страну из предыдущего сообщения (нужно сохранять контекст)
    # Для простоты показываем все аккаунты
    accounts = db.get_available_accounts()
    
    await callback.message.edit_text(
        f"📱 <b>Доступные аккаунты ({len(accounts)}):</b>\n\n",
        reply_markup=get_accounts_keyboard(accounts, page),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("account_"))
async def show_account_details(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[1])
    account = db.get_account_by_id(account_id)
    
    if not account or account['is_sold']:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>\n\n"
            "Выберите другой аккаунт.",
            reply_markup=get_countries_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Получаем актуальные курсы
    usd_price = await currency_rates.rub_to_usd(account['price_rub'])
    ton_price = await currency_rates.rub_to_ton(account['price_rub'])
    usdt_price = await currency_rates.rub_to_usdt(account['price_rub'])
    
    text = f"""
📱 <b>Детали аккаунта:</b>

🌍 <b>Страна:</b> {account['country']}
💰 <b>Цена:</b> {account['price_rub']} ₽

<b>💱 В других валютах:</b>
• 💵 USD: {usd_price}$
• 💎 TON: {ton_price} TON
• 💲 USDT: {usdt_price} USDT

<b>Выберите способ оплаты:</b>
    """
    
    await callback.message.edit_text(
        text,
        reply_markup=get_payment_methods_keyboard(account_id),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def process_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    method = parts[1]
    account_id = int(parts[2])
    
    account = db.get_account_by_id(account_id)
    if not account or account['is_sold']:
        await callback.message.edit_text(
            "❌ <b>Этот аккаунт уже продан</b>\n\n"
            "Выберите другой аккаунт.",
            reply_markup=get_countries_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Определяем валюту
    currency_map = {
        'ton': 'TON',
        'usdt': 'USDT',
        'crypto': 'USDT'  # Crypto Bot использует USDT
    }
    currency = currency_map.get(method, 'USDT')
    
    # Получаем сумму в нужной валюте
    if method == 'ton':
        amount = await currency_rates.rub_to_ton(account['price_rub'])
    else:
        amount = await currency_rates.rub_to_usdt(account['price_rub'])
    
    # Создаем счет в Crypto Bot
    description = f"Покупка Telegram аккаунта ({account['country']})"
    payload = f"acc_{account_id}_{callback.from_user.id}_{int(datetime.now().timestamp())}"
    
    invoice = await crypto_api.create_invoice(amount, currency, description, payload)
    
    if not invoice:
        await callback.message.edit_text(
            "❌ <b>Ошибка создания платежа</b>\n\n"
            "Попробуйте позже или выберите другой способ оплаты.",
            reply_markup=get_payment_methods_keyboard(account_id),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Создаем заказ в базе
    order_id = db.create_order(
        callback.from_user.id,
        account_id,
        account['price_rub'],
        currency,
        invoice['invoice_id'],
        invoice['pay_url']
    )
    
    payment_text = f"""
🧾 <b>Счет на оплату</b>

📱 <b>Аккаунт:</b> {account['country']}
💰 <b>Сумма:</b> {amount} {currency}
💳 <b>Способ:</b> {method.upper()}

<b>Инструкция:</b>
1. Нажмите кнопку "Оплатить"
2. Оплатите счет в боте @CryptoBot
3. После оплаты аккаунт придет автоматически

⏳ <b>Счет действителен 30 минут</b>
    """
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text=f"💳 Оплатить {amount} {currency}", url=invoice['pay_url']))
    builder.add(InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment_{invoice['invoice_id']}"))
    builder.add(InlineKeyboardButton(text="🔙 Отмена", callback_data=f"back_to_account_{account_id}"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        payment_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback: CallbackQuery):
    invoice_id = callback.data.replace("check_payment_", "")
    
    # Проверяем статус платежа
    status = await crypto_api.get_invoice_status(invoice_id)
    
    if status == "paid":
        # Подтверждаем заказ
        order = db.confirm_order(invoice_id)
        
        if order:
            account = db.get_account_by_id(order['account_id'])
            
            success_text = f"""
✅ <b>Оплата получена!</b>

📱 <b>Ваш аккаунт:</b>
<code>{account['phone']}</code>

🔑 <b>Код подтверждения:</b>
Нажмите кнопку ниже, чтобы получить код

⚠️ <b>Важно:</b> Код нужно запросить в течение 5 минут!
            """
            
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(text="📱 ПОЛУЧИТЬ КОД", callback_data=f"get_code_{account['id']}"))
            builder.add(InlineKeyboardButton(text="🔙 В магазин", callback_data="back_to_main"))
            
            await callback.message.edit_text(
                success_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "❌ <b>Ошибка при обработке платежа</b>\n\n"
                "Обратитесь в поддержку.",
                reply_markup=get_back_to_menu_keyboard()
            )
    else:
        await callback.answer("❌ Платеж еще не найден. Оплатите счет и нажмите снова.", show_alert=True)

@dp.callback_query(F.data.startswith("get_code_"))
async def get_account_code(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    account = db.get_account_by_id(account_id)
    
    if not account:
        await callback.message.edit_text(
            "❌ <b>Аккаунт не найден</b>",
            reply_markup=get_back_to_menu_keyboard()
        )
        await callback.answer()
        return
    
    loading_msg = await callback.message.edit_text(
        "🔍 <b>Получение кода подтверждения...</b>\n\n"
        "Пожалуйста, подождите.",
        parse_mode="HTML"
    )
    
    try:
        client = Client(
            f"session_{account_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=account['session_string'],
            in_memory=True
        )
        
        await client.start()
        
        try:
            # Получаем диалоги
            dialogs = []
            async for dialog in client.get_dialogs():
                dialogs.append(dialog)
            
            if dialogs:
                # Берем первый чат
                first_chat = dialogs[0].chat
                
                # Получаем последние сообщения
                messages = []
                async for msg in client.get_chat_history(first_chat.id, limit=10):
                    messages.append(msg)
                
                # Ищем 5-значный код
                found_code = None
                for msg in messages:
                    if msg.text:
                        # Ищем 5 цифр подряд
                        code_match = re.search(r'\b(\d{5})\b', msg.text)
                        if code_match:
                            found_code = code_match.group(1)
                            break
                        
                        # Ищем код после слова "код"
                        code_match = re.search(r'[Кк][оО][дД][:\s]*(\d{5})', msg.text)
                        if code_match:
                            found_code = code_match.group(1)
                            break
                
                if found_code:
                    await loading_msg.edit_text(
                        f"✅ <b>Код подтверждения:</b>\n\n"
                        f"🔑 <code>{found_code}</code>\n\n"
                        f"Используйте номер и код для входа в Telegram.",
                        reply_markup=get_back_to_menu_keyboard(),
                        parse_mode="HTML"
                    )
                else:
                    await loading_msg.edit_text(
                        "❌ <b>Не удалось найти код</b>\n\n"
                        "Запросите код в приложении Telegram и нажмите кнопку еще раз.",
                        reply_markup=InlineKeyboardBuilder().add(
                            InlineKeyboardButton(text="🔄 Повторить", callback_data=f"get_code_{account_id}")
                        ).as_markup(),
                        parse_mode="HTML"
                    )
            else:
                await loading_msg.edit_text(
                    "❌ <b>Нет диалогов в аккаунте</b>",
                    reply_markup=get_back_to_menu_keyboard()
                )
        
        finally:
            await client.stop()
    
    except Exception as e:
        logger.error(f"Error getting code: {e}")
        await loading_msg.edit_text(
            f"❌ <b>Ошибка:</b> {str(e)[:100]}",
            reply_markup=get_back_to_menu_keyboard()
        )

# Админ-панель
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    await message.answer(
        "🔧 <b>Панель администратора</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    stats = db.get_statistics()
    
    text = f"""
📊 <b>Статистика магазина</b>

📱 <b>Аккаунты:</b>
• Всего: {stats['total_accounts']}
• Доступно: {stats['available_accounts']}
• Продано: {stats['sold_accounts']}

🛍 <b>Заказы:</b>
• Всего: {stats['total_orders']}
• Оплачено: {stats['paid_orders']}

💰 <b>Выручка:</b> {stats['total_revenue']} ₽
    """
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_sales")
async def admin_sales(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.*, a.phone, a.country 
            FROM orders o
            JOIN accounts a ON o.account_id = a.id
            WHERE o.status = 'paid'
            ORDER BY o.paid_date DESC
            LIMIT 20
        ''')
        sales = cursor.fetchall()
    
    if not sales:
        await callback.message.edit_text(
            "📭 <b>Нет продаж</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = "📈 <b>Последние 20 продаж:</b>\n\n"
    for sale in sales:
        text += f"👤 {sale['user_id']} | {sale['country']} | {sale['amount_rub']}₽ | {sale['paid_date'][:10]}\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_list")
async def admin_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM accounts 
            ORDER BY added_date DESC 
            LIMIT 20
        ''')
        accounts = cursor.fetchall()
    
    if not accounts:
        await callback.message.edit_text(
            "📭 <b>Нет аккаунтов</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = "📋 <b>Последние 20 аккаунтов:</b>\n\n"
    for acc in accounts:
        status = "✅" if not acc['is_sold'] else "❌"
        text += f"{status} {acc['country']} | {acc['price_rub']}₽ | {acc['phone'][:6]}**** | {acc['added_date'][:10]}\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_change_price")
async def admin_change_price_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, phone, country, price_rub FROM accounts WHERE is_sold = 0 ORDER BY id DESC LIMIT 10')
        accounts = cursor.fetchall()
    
    text = "💰 <b>Изменить цену аккаунта</b>\n\n"
    text += "Выберите аккаунт из списка:\n\n"
    
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        short_phone = acc['phone'][:6] + "****"
        builder.add(InlineKeyboardButton(
            text=f"{acc['country']} | {short_phone} | {acc['price_rub']}₽",
            callback_data=f"price_acc_{acc['id']}"
        ))
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("price_acc_"))
async def admin_change_price_input(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    account_id = int(callback.data.split("_")[2])
    await state.update_data(account_id=account_id)
    
    await callback.message.edit_text(
        "💰 <b>Введите новую цену в рублях:</b>\n\n"
        "Например: 150",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_account_price)
    await callback.answer()

@dp.message(AdminStates.waiting_account_price)
async def admin_change_price_process(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    try:
        price = int(message.text.strip())
        if price < 1:
            raise ValueError()
    except:
        await message.answer("❌ Введите корректное число (больше 0)")
        return
    
    data = await state.get_data()
    account_id = data.get('account_id')
    
    db.update_account_price(account_id, price)
    
    await message.answer(
        f"✅ Цена аккаунта изменена на {price}₽",
        reply_markup=get_admin_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "admin_change_country")
async def admin_change_country_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, phone, country, price_rub FROM accounts WHERE is_sold = 0 ORDER BY id DESC LIMIT 10')
        accounts = cursor.fetchall()
    
    text = "🌍 <b>Изменить страну аккаунта</b>\n\n"
    text += "Выберите аккаунт из списка:\n\n"
    
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        short_phone = acc['phone'][:6] + "****"
        builder.add(InlineKeyboardButton(
            text=f"{acc['country']} | {short_phone} | {acc['price_rub']}₽",
            callback_data=f"country_acc_{acc['id']}"
        ))
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("country_acc_"))
async def admin_change_country_input(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    account_id = int(callback.data.split("_")[2])
    await state.update_data(account_id=account_id)
    
    countries = db.get_all_countries()
    
    text = "🌍 <b>Выберите страну:</b>\n\n"
    
    builder = InlineKeyboardBuilder()
    for country in countries:
        builder.add(InlineKeyboardButton(text=country, callback_data=f"set_country_{country}"))
    builder.adjust(2)
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_account_country)
    await callback.answer()

@dp.callback_query(AdminStates.waiting_account_country)
async def admin_set_country(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    if callback.data.startswith("set_country_"):
        country = callback.data.replace("set_country_", "")
        
        data = await state.get_data()
        account_id = data.get('account_id')
        
        db.update_account_country(account_id, country)
        
        await callback.message.edit_text(
            f"✅ Страна аккаунта изменена на {country}",
            reply_markup=get_admin_keyboard()
        )
        await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "📱 <b>Добавление нового аккаунта</b>\n\n"
        "Отправьте номер телефона в формате:\n"
        "<code>+79001234567</code>\n\n"
        "или /cancel для отмены",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_phone)
    await callback.answer()

@dp.message(AdminStates.waiting_phone)
async def process_admin_phone(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    if message.text == "/cancel":
        await state.clear()
        await message.answer(
            "❌ Операция отменена",
            reply_markup=get_admin_keyboard()
        )
        return
    
    phone = message.text.strip()
    
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer(
            "❌ Неверный формат. Используйте: +79001234567",
            parse_mode="HTML"
        )
        return
    
    try:
        client = Client(
            f"temp_{phone}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True
        )
        
        await client.connect()
        sent_code = await client.send_code(phone)
        
        temp_auth_data[phone] = {
            'client': client,
            'phone_code_hash': sent_code.phone_code_hash
        }
        
        await state.update_data(phone=phone)
        
        await message.answer(
            "✅ Код отправлен!\n\n"
            "Введите 5-значный код из Telegram:",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_code)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()

@dp.message(AdminStates.waiting_code)
async def process_admin_code(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    code = message.text.strip()
    
    if not re.match(r'^\d{5}$', code):
        await message.answer("❌ Введите 5 цифр")
        return
    
    data = await state.get_data()
    phone = data.get('phone')
    auth_data = temp_auth_data.get(phone)
    
    if not auth_data:
        await message.answer("❌ Сессия устарела. Начните заново.")
        await state.clear()
        return
    
    try:
        client = auth_data['client']
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=auth_data['phone_code_hash'],
            phone_code=code
        )
        
        session_string = await client.export_session_string()
        
        # Определяем страну (по умолчанию Virtual)
        country = "Virtual"
        
        db.add_account(phone, session_string, country, 100)
        
        await client.disconnect()
        del temp_auth_data[phone]
        
        await message.answer(
            f"✅ Аккаунт {phone} добавлен!\n\n"
            f"Страна: {country}\n"
            f"Цена: 100₽ (можно изменить)",
            reply_markup=get_admin_keyboard()
        )
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
    
    await state.clear()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "🔧 <b>Панель администратора</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

# Запуск бота
async def main():
    # Проверяем подключение к Crypto Bot
    me = await crypto_api.get_me()
    if me:
        logger.info(f"Crypto Bot connected: {me.get('name')}")
    else:
        logger.warning("Crypto Bot connection failed")
    
    # Обновляем курсы валют
    await currency_rates.update_rates()
    
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
