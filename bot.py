import asyncio
import logging
import sqlite3
import re
from datetime import datetime
from typing import Optional, Dict
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import pyrogram
from pyrogram import Client
from dotenv import load_dotenv
import os

# Загрузка переменных окружения
load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
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
    waiting_account_name = State()

# Класс для работы с базой данных
class Database:
    def __init__(self, db_name='accounts.db'):
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
                    is_active INTEGER DEFAULT 1,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    used_count INTEGER DEFAULT 0
                )
            ''')
            
            # Таблица выданных аккаунтов пользователям
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    issued_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    FOREIGN KEY (account_id) REFERENCES accounts (id),
                    UNIQUE(user_id, account_id)
                )
            ''')
            
            # Таблица для временных сессий добавления аккаунтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS temp_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    session_data TEXT,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
    
    def add_account(self, phone: str, session_string: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO accounts (phone, session_string) VALUES (?, ?)',
                (phone, session_string)
            )
            conn.commit()
            return cursor.lastrowid
    
    def get_available_accounts(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM accounts 
                WHERE is_active = 1 
                AND id NOT IN (
                    SELECT account_id FROM user_accounts 
                    WHERE is_active = 1
                )
            ''')
            return cursor.fetchall()
    
    def get_random_account(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM accounts 
                WHERE is_active = 1 
                AND id NOT IN (
                    SELECT account_id FROM user_accounts 
                    WHERE is_active = 1
                )
                ORDER BY RANDOM() LIMIT 1
            ''')
            return cursor.fetchone()
    
    def assign_account_to_user(self, user_id: int, account_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    'INSERT INTO user_accounts (user_id, account_id) VALUES (?, ?)',
                    (user_id, account_id)
                )
                cursor.execute(
                    'UPDATE accounts SET used_count = used_count + 1, last_used = CURRENT_TIMESTAMP WHERE id = ?',
                    (account_id,)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
    
    def get_user_account(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT a.* FROM accounts a
                JOIN user_accounts ua ON a.id = ua.account_id
                WHERE ua.user_id = ? AND ua.is_active = 1
            ''', (user_id,))
            return cursor.fetchone()
    
    def deactivate_account(self, account_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE accounts SET is_active = 0 WHERE id = ?', (account_id,))
            cursor.execute('UPDATE user_accounts SET is_active = 0 WHERE account_id = ?', (account_id,))
            conn.commit()
    
    def get_statistics(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            total = cursor.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
            active = cursor.execute('SELECT COUNT(*) FROM accounts WHERE is_active = 1').fetchone()[0]
            issued = cursor.execute('SELECT COUNT(DISTINCT user_id) FROM user_accounts WHERE is_active = 1').fetchone()[0]
            available = cursor.execute('''
                SELECT COUNT(*) FROM accounts 
                WHERE is_active = 1 
                AND id NOT IN (
                    SELECT account_id FROM user_accounts WHERE is_active = 1
                )
            ''').fetchone()[0]
            return {
                'total': total,
                'active': active,
                'issued': issued,
                'available': available
            }
    
    def save_temp_session(self, phone: str, session_data: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO temp_sessions (phone, session_data) VALUES (?, ?)',
                (phone, session_data)
            )
            conn.commit()
    
    def get_temp_session(self, phone: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM temp_sessions WHERE phone = ?', (phone,))
            return cursor.fetchone()
    
    def delete_temp_session(self, phone: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM temp_sessions WHERE phone = ?', (phone,))
            conn.commit()

# Инициализация базы данных
db = Database()

# Словарь для хранения клиентов Pyrogram
pyro_clients: Dict[str, Client] = {}

# Клавиатуры
def get_start_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="🔥 ПОЛУЧИТЬ АККАУНТ",
        callback_data="get_account"
    ))
    # Кастомизация кнопки (красный цвет)
    return builder.as_markup()

def get_code_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="📱 ПОЛУЧИТЬ КОД",
        callback_data="get_code"
    ))
    return builder.as_markup()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="admin_add"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton(text="🔴 Деактивировать", callback_data="admin_deactivate")
    )
    builder.adjust(1)
    return builder.as_markup()

# Обработчики пользовательских команд
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Нажмите кнопку ниже, чтобы получить аккаунт Telegram:",
        reply_markup=get_start_keyboard()
    )

@dp.callback_query(F.data == "get_account")
async def process_get_account(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Проверяем, есть ли уже аккаунт у пользователя
    existing_account = db.get_user_account(user_id)
    if existing_account:
        await callback.message.edit_text(
            f"❌ У вас уже есть активный аккаунт:\n"
            f"📞 Номер: `{existing_account['phone']}`\n\n"
            f"Вы можете получить код подтверждения:",
            reply_markup=get_code_keyboard()
        )
        await callback.answer()
        return
    
    # Получаем случайный свободный аккаунт
    account = db.get_random_account()
    if not account:
        await callback.message.edit_text(
            "😔 К сожалению, свободных аккаунтов нет. Попробуйте позже."
        )
        await callback.answer()
        return
    
    # Выдаем аккаунт пользователю
    db.assign_account_to_user(user_id, account['id'])
    
    await callback.message.edit_text(
        f"✅ Вам выдан аккаунт!\n\n"
        f"📞 Номер телефона: `{account['phone']}`\n\n"
        f"Теперь нажмите кнопку ниже, чтобы получить код подтверждения:",
        reply_markup=get_code_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "get_code")
async def process_get_code(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Получаем аккаунт пользователя
    account = db.get_user_account(user_id)
    if not account:
        await callback.message.edit_text(
            "❌ У вас нет активного аккаунта. Сначала получите аккаунт.",
            reply_markup=get_start_keyboard()
        )
        await callback.answer()
        return
    
    try:
        # Создаем клиент Pyrogram для аккаунта
        client = Client(
            f"session_{account['phone']}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=account['session_string'],
            in_memory=True
        )
        
        await client.start()
        
        try:
            # Ищем чат с ботом +42777
            async for dialog in client.get_dialogs():
                if dialog.chat.username == "spambot" or "42777" in str(dialog.chat.id):
                    # Получаем последнее сообщение
                    messages = []
                    async for msg in client.get_chat_history(dialog.chat.id, limit=1):
                        messages.append(msg)
                    
                    if messages:
                        last_msg = messages[0]
                        # Ищем код в сообщении (обычно 5-6 цифр)
                        code_match = re.search(r'\b\d{5,6}\b', last_msg.text or "")
                        if code_match:
                            code = code_match.group()
                            await callback.message.edit_text(
                                f"✅ Код подтверждения:\n\n"
                                f"🔑 `{code}`\n\n"
                                f"Используйте его для входа в аккаунт."
                            )
                        else:
                            await callback.message.edit_text(
                                "❌ Не удалось найти код в последнем сообщении.\n"
                                "Попробуйте запросить код еще раз в приложении Telegram."
                            )
                    else:
                        await callback.message.edit_text(
                            "❌ Нет сообщений от бота. Отправьте /start боту +42777 в этом аккаунте."
                        )
                    break
            else:
                await callback.message.edit_text(
                    "❌ Не найден чат с ботом +42777.\n"
                    "Напишите /start боту +42777 в этом аккаунте."
                )
        
        finally:
            await client.stop()
    
    except Exception as e:
        logger.error(f"Error getting code: {e}")
        await callback.message.edit_text(
            "❌ Произошла ошибка при получении кода. Попробуйте позже."
        )

# Обработчики админ-панели
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    await message.answer(
        "🔧 Админ-панель\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    stats = db.get_statistics()
    
    await callback.message.edit_text(
        f"📊 Статистика:\n\n"
        f"📱 Всего аккаунтов: {stats['total']}\n"
        f"✅ Активных: {stats['active']}\n"
        f"👥 Выдано пользователям: {stats['issued']}\n"
        f"🆓 Свободно: {stats['available']}",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "📱 Отправьте номер телефона аккаунта в формате:\n"
        "`+79001234567`"
    )
    await state.set_state(AdminStates.waiting_phone)
    await callback.answer()

@dp.message(AdminStates.waiting_phone)
async def process_admin_phone(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    phone = message.text.strip()
    
    # Базовая валидация номера
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer("❌ Неверный формат номера. Попробуйте снова:")
        return
    
    try:
        # Создаем временный клиент для авторизации
        client = Client(
            f"temp_{phone}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True
        )
        
        await client.connect()
        
        # Отправляем код подтверждения
        sent_code = await client.send_code(phone)
        
        # Сохраняем данные в состояние
        await state.update_data(
            phone=phone,
            phone_code_hash=sent_code.phone_code_hash,
            client=client
        )
        
        pyro_clients[phone] = client
        
        await message.answer(
            "✅ Код подтверждения отправлен!\n"
            "Введите код из Telegram:"
        )
        await state.set_state(AdminStates.waiting_code)
        
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.clear()

@dp.message(AdminStates.waiting_code)
async def process_admin_code(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    client = pyro_clients.get(phone)
    
    if not client:
        await message.answer("❌ Сессия устарела. Начните заново.")
        await state.clear()
        return
    
    try:
        # Подтверждаем код
        await client.sign_in(phone, code)
        
        # Получаем session string
        session_string = await client.export_session_string()
        
        # Сохраняем аккаунт в базу
        db.add_account(phone, session_string)
        
        # Очищаем временные данные
        await client.disconnect()
        del pyro_clients[phone]
        
        await message.answer(
            f"✅ Аккаунт {phone} успешно добавлен!"
        )
        
        # Возвращаемся в админ-панель
        await message.answer(
            "🔧 Админ-панель",
            reply_markup=get_admin_keyboard()
        )
        
    except Exception as e:
        logger.error(f"Error signing in: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
    
    await state.clear()

@dp.callback_query(F.data == "admin_deactivate")
async def admin_deactivate(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    # Получаем список выданных аккаунтов
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.id, a.phone, ua.user_id 
            FROM accounts a
            JOIN user_accounts ua ON a.id = ua.account_id
            WHERE a.is_active = 1 AND ua.is_active = 1
        ''')
        accounts = cursor.fetchall()
    
    if not accounts:
        await callback.message.edit_text(
            "📭 Нет выданных активных аккаунтов",
            reply_markup=get_admin_keyboard()
        )
        await callback.answer()
        return
    
    # Создаем клавиатуру с аккаунтами
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        builder.add(InlineKeyboardButton(
            text=f"{acc['phone']} (user: {acc['user_id']})",
            callback_data=f"deactivate_{acc['id']}"
        ))
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "🔴 Выберите аккаунт для деактивации:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("deactivate_"))
async def process_deactivate(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    account_id = int(callback.data.split("_")[1])
    db.deactivate_account(account_id)
    
    await callback.message.edit_text(
        "✅ Аккаунт деактивирован",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    await callback.message.edit_text(
        "🔧 Админ-панель",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

# Запуск бота
async def main():
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
