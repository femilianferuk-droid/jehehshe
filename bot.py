"""
Telegram Bot для управления аккаунтами через Pyrogram
ИСПРАВЛЕННАЯ ВЕРСИЯ - все вводы работают
"""

import os
import asyncio
import logging
import re
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneNumberInvalid, PhoneCodeInvalid, 
    PasswordHashInvalid, AuthKeyUnregistered, FloodWait, 
    UsernameNotOccupied, ChatAdminRequired, UserIsBlocked,
    PeerIdInvalid, ChatIdInvalid
)
from pyrogram.types import User as PyroUser, Chat as PyroChat
from pyrogram.enums import ChatType, ChatMemberStatus
from dotenv import load_dotenv

# ================== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==================
load_dotenv()

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# ADMIN_IDS теперь необязательный
ADMIN_IDS = []
if os.getenv("ADMIN_IDS"):
    try:
        ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS").split(",")]
    except:
        logging.warning("Не удалось распарсить ADMIN_IDS")

SUBSCRIPTION_PRICES = {
    "1_month": 100,
    "3_months": 250,
    "forever": 500
}
TEST_PERIOD_DAYS = 3
SPAM_BOT_USERNAME = "spambot"
SPAM_BOT_TIMEOUT = 15
CHATS_PER_PAGE = 15
MAX_2FA_ATTEMPTS = 3
MAX_CODE_ATTEMPTS = 3

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================== БАЗА ДАННЫХ ==================
DATABASE_PATH = "bot_database.db"

async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Таблица пользователей
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                api_id INTEGER,
                api_hash TEXT,
                session_string TEXT,
                phone_number TEXT,
                account_username TEXT,
                account_first_name TEXT,
                account_last_name TEXT,
                account_id INTEGER,
                subscription_until TIMESTAMP,
                language TEXT DEFAULT 'ru',
                auto_clean_spam INTEGER DEFAULT 0,
                notify_expiration INTEGER DEFAULT 1,
                auto_respond INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица для временных данных авторизации
        await db.execute('''
            CREATE TABLE IF NOT EXISTS auth_temp (
                user_id INTEGER PRIMARY KEY,
                api_id INTEGER,
                api_hash TEXT,
                phone_number TEXT,
                code_attempts INTEGER DEFAULT 0,
                password_attempts INTEGER DEFAULT 0,
                temp_client_data TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для статистики использования
        await db.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для избранных чатов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS favorite_chats (
                user_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                chat_type TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        await db.commit()
        logger.info("Database initialized")

# ================== FSM СОСТОЯНИЯ ==================
class AuthStates(StatesGroup):
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa_password = State()

class ChatSelectionStates(StatesGroup):
    waiting_chat_number = State()

class ChatInfoStates(StatesGroup):
    waiting_chat_input = State()

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Получить данные пользователя из БД"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

async def save_user_session(user_id: int, session_string: str, account_info: PyroUser, phone_number: str, api_id: int, api_hash: str):
    """Сохранить сессию пользователя"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        subscription_until = (datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).isoformat()
        
        await db.execute('''
            INSERT OR REPLACE INTO users 
            (user_id, username, session_string, phone_number, account_username, 
             account_first_name, account_last_name, account_id, subscription_until, api_id, api_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, 
            account_info.username or f"user_{user_id}",
            session_string,
            phone_number,
            account_info.username,
            account_info.first_name,
            account_info.last_name or "",
            account_info.id,
            subscription_until,
            api_id,
            api_hash
        ))
        
        await db.execute("DELETE FROM auth_temp WHERE user_id = ?", (user_id,))
        await db.commit()
        
        logger.info(f"User {user_id} saved session for @{account_info.username}")

async def clear_user_session(user_id: int):
    """Очистить сессию пользователя"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM auth_temp WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM usage_stats WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM favorite_chats WHERE user_id = ?", (user_id,))
        await db.commit()
        logger.info(f"User {user_id} session cleared")

async def check_subscription(user_id: int) -> Tuple[bool, Optional[str]]:
    """Проверить подписку пользователя"""
    user = await get_user(user_id)
    if not user:
        return False, "Пользователь не найден"
    
    if not user.get('subscription_until'):
        return False, "Нет информации о подписке"
    
    try:
        subscription_until = datetime.fromisoformat(user['subscription_until'])
        if datetime.now() > subscription_until:
            return False, f"Срок подписки истек {subscription_until.strftime('%d.%m.%Y')}"
    except:
        return False, "Ошибка в дате подписки"
    
    return True, None

async def get_pyro_client(user_id: int) -> Optional[Client]:
    """Получить Pyrogram клиент для пользователя"""
    user = await get_user(user_id)
    if not user or not user.get('session_string'):
        return None
    
    try:
        client = Client(
            name=f"user_{user_id}",
            session_string=user['session_string'],
            api_id=user['api_id'],
            api_hash=user['api_hash'],
            in_memory=True
        )
        await client.start()
        
        me = await client.get_me()
        if me:
            logger.info(f"Client started for user {user_id} (@{me.username or me.first_name})")
            return client
        else:
            await client.stop()
            return None
            
    except AuthKeyUnregistered:
        logger.error(f"Auth key unregistered for user {user_id}")
        await clear_user_session(user_id)
        return None
    except Exception as e:
        logger.error(f"Error starting pyro client for {user_id}: {e}")
        return None

async def log_usage(user_id: int, action: str, details: str = ""):
    """Логировать использование функций"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO usage_stats (user_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, details)
        )
        await db.commit()

# ================== КЛАВИАТУРЫ ==================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚫 Проверить спам-блок")],
            [KeyboardButton(text="💬 Мои чаты"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="📊 Инфо о чате")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_profile_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура профиля"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 ПРОДЛИТЬ ПОДПИСКУ", callback_data="extend_subscription"))
    builder.row(InlineKeyboardButton(text="🔄 СБРОСИТЬ СЕССИЮ", callback_data="reset_session"))
    builder.row(InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

def get_settings_keyboard(user: Dict) -> InlineKeyboardMarkup:
    """Клавиатура настроек"""
    builder = InlineKeyboardBuilder()
    
    auto_clean = user.get('auto_clean_spam', 0)
    notify = user.get('notify_expiration', 1)
    
    clean_status = "✅ Вкл" if auto_clean else "❌ Выкл"
    notify_status = "✅ Вкл" if notify else "❌ Выкл"
    
    builder.row(InlineKeyboardButton(text=f"🧹 Автоочистка спам-бота: {clean_status}", callback_data="toggle_auto_clean"))
    builder.row(InlineKeyboardButton(text=f"🔔 Уведомления: {notify_status}", callback_data="toggle_notify"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile"))
    
    return builder.as_markup()

def get_subscription_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура продления подписки"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="1 месяц - 100₽", callback_data="sub_1_month"))
    builder.row(InlineKeyboardButton(text="3 месяца - 250₽", callback_data="sub_3_months"))
    builder.row(InlineKeyboardButton(text="Навсегда - 500₽", callback_data="sub_forever"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile"))
    return builder.as_markup()

def get_back_button() -> InlineKeyboardMarkup:
    """Кнопка назад"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

# ================== ОСНОВНЫЕ ФУНКЦИИ ==================

async def check_spam_status(user_id: int) -> Tuple[bool, str, Optional[str]]:
    """Проверка спам-блока через @spambot"""
    client = None
    try:
        client = await get_pyro_client(user_id)
        if not client:
            return False, "Ошибка подключения к аккаунту", None
        
        me = await client.get_me()
        
        try:
            spambot = await client.get_users(SPAM_BOT_USERNAME)
        except UsernameNotOccupied:
            return False, "Бот @spambot не найден", None
        
        await client.send_message(spambot.id, "/start")
        await asyncio.sleep(3)
        
        messages = []
        async for msg in client.get_chat_history(spambot.id, limit=10):
            if msg.from_user and msg.from_user.id == spambot.id:
                messages.append(msg)
        
        if not messages:
            return False, "Нет ответа от @spambot", None
        
        last_msg = messages[0]
        text = last_msg.text or last_msg.caption or ""
        
        is_restricted = False
        unlock_date = None
        
        if re.search(r'(ограничены|заблокированы|имеются ограничения)', text.lower()):
            is_restricted = True
        elif re.search(r'(добро пожаловать|вы не ограничены|нет ограничений)', text.lower()):
            is_restricted = False
        
        if re.search(r'(restricted|limited|banned)', text.lower()):
            is_restricted = True
        elif re.search(r'(welcome|is not restricted|no restrictions)', text.lower()):
            is_restricted = False
        
        date_patterns = [
            r'до (\d{2}\.\d{2}\.\d{4})',
            r'until (\w+ \d{1,2},? \d{4})',
            r'(\d{4}-\d{2}-\d{2})',
            r'(\d{2}\.\d{2}\.\d{4})'
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                unlock_date = match.group(1)
                break
        
        user = await get_user(user_id)
        if user and user.get('auto_clean_spam'):
            try:
                async for msg in client.get_chat_history(spambot.id, limit=10):
                    if msg.from_user and msg.from_user.is_self:
                        await msg.delete()
            except:
                pass
        
        status_text = "✅ Аккаунт НЕ в спам-блоке" if not is_restricted else "🚫 Аккаунт В СПАМ-БЛОКЕ!"
        return True, status_text, unlock_date
        
    except FloodWait as e:
        return False, f"Слишком много запросов. Подождите {e.value}с", None
    except Exception as e:
        logger.error(f"Error checking spam: {e}")
        return False, f"Ошибка: {str(e)[:100]}", None
    finally:
        if client:
            await client.stop()

async def get_all_dialogs(client: Client) -> List[Dict]:
    """Получить ВСЕ диалоги пользователя"""
    dialogs = []
    try:
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            dialog_info = {
                'id': chat.id,
                'title': chat.title or chat.first_name or "Без названия",
                'type': chat.type,
                'username': chat.username,
                'is_bot': getattr(chat, 'is_bot', False),
                'last_message_date': dialog.top_message.date if dialog.top_message else None,
                'unread_count': dialog.unread_messages_count,
                'pinned': dialog.is_pinned
            }
            
            if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
                try:
                    if chat.type == ChatType.CHANNEL:
                        full_chat = await client.get_chat(chat.id)
                        dialog_info['members_count'] = getattr(full_chat, 'members_count', 0)
                    else:
                        dialog_info['members_count'] = await client.get_chat_members_count(chat.id)
                except:
                    dialog_info['members_count'] = 0
            else:
                dialog_info['members_count'] = 0
            
            dialogs.append(dialog_info)
    except Exception as e:
        logger.error(f"Error getting dialogs: {e}")
    
    return dialogs

# ================== ОБРАБОТЧИКИ КОМАНД ==================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    user = await get_user(user_id)
    
    if user:
        await message.answer(
            f"👋 С возвращением, {user['account_first_name']}!\n\n"
            f"Подключен аккаунт: @{user['account_username'] or 'нет'}\n"
            f"Выберите действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        await state.set_state(AuthStates.waiting_api_id)
        await message.answer(
            "👋 Добро пожаловать в UserBox Manager!\n\n"
            "🔑 **ШАГ 1 ИЗ 4:** Введите API ID\n\n"
            "API ID - это целое число, которое можно получить на https://my.telegram.org/apps\n\n"
            "Отправьте API ID:",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )

# ================== АВТОРИЗАЦИЯ ==================

@dp.message(AuthStates.waiting_api_id)
async def auth_get_api_id(message: types.Message, state: FSMContext):
    """Получение API ID"""
    try:
        api_id = int(message.text.strip())
        if api_id <= 0:
            raise ValueError
        
        await state.update_data(api_id=api_id)
        await state.set_state(AuthStates.waiting_api_hash)
        
        await message.answer(
            "🔑 **ШАГ 2 ИЗ 4:** Введите API HASH\n\n"
            "API Hash - это строка из 32 символов, которую вы получили на my.telegram.org/apps\n\n"
            "Отправьте API Hash:",
            parse_mode="Markdown"
        )
        
    except ValueError:
        await message.answer("❌ API ID должен быть положительным числом. Попробуйте снова:")

@dp.message(AuthStates.waiting_api_hash)
async def auth_get_api_hash(message: types.Message, state: FSMContext):
    """Получение API Hash"""
    api_hash = message.text.strip()
    
    if len(api_hash) != 32:
        await message.answer("❌ API Hash должен быть 32 символа. Попробуйте снова:")
        return
    
    data = await state.get_data()
    api_id = data['api_id']
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO auth_temp (user_id, api_id, api_hash, code_attempts, password_attempts)
            VALUES (?, ?, ?, 0, 0)
        ''', (user_id, api_id, api_hash))
        await db.commit()
    
    await state.set_state(AuthStates.waiting_phone)
    
    await message.answer(
        "📱 **ШАГ 3 ИЗ 4:** Введите номер телефона\n\n"
        "Формат: +79001234567\n\n"
        "Отправьте номер:",
        parse_mode="Markdown"
    )

@dp.message(AuthStates.waiting_phone)
async def auth_get_phone(message: types.Message, state: FSMContext):
    """Получение номера телефона"""
    phone = message.text.strip()
    
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer("❌ Неверный формат номера. Используйте +79001234567")
        return
    
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT api_id, api_hash FROM auth_temp WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        
        if not row:
            await message.answer("❌ Ошибка: данные не найдены. Начните заново с /start")
            await state.clear()
            return
        
        api_id, api_hash = row
        
        await db.execute(
            "UPDATE auth_temp SET phone_number = ? WHERE user_id = ?",
            (phone, user_id)
        )
        await db.commit()
    
    try:
        client = Client(
            name=f"auth_{user_id}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True
        )
        
        await client.connect()
        sent_code = await client.send_code(phone)
        
        await state.update_data(
            phone=phone,
            client=client,
            phone_code_hash=sent_code.phone_code_hash
        )
        
        await state.set_state(AuthStates.waiting_code)
        
        await message.answer(
            "✅ Код подтверждения отправлен!\n\n"
            "📱 **ШАГ 4 ИЗ 4:** Введите код из Telegram\n\n"
            "Если код не приходит в течение минуты, проверьте правильность номера.",
            parse_mode="Markdown"
        )
        
    except PhoneNumberInvalid:
        await message.answer("❌ Неверный номер телефона. Попробуйте снова:")
        await state.set_state(AuthStates.waiting_phone)
    except FloodWait as e:
        await message.answer(f"⚠️ Слишком много попыток. Подождите {e.value} секунд")
        await state.clear()
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await message.answer("❌ Ошибка при отправке кода. Попробуйте позже.")
        await state.clear()

@dp.message(AuthStates.waiting_code)
async def auth_get_code(message: types.Message, state: FSMContext):
    """Получение кода подтверждения"""
    code = message.text.strip()
    user_id = message.from_user.id
    
    data = await state.get_data()
    client = data.get('client')
    phone = data.get('phone')
    phone_code_hash = data.get('phone_code_hash')
    
    if not client:
        await message.answer("❌ Ошибка сессии. Начните заново с /start")
        await state.clear()
        return
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT code_attempts FROM auth_temp WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        attempts = row[0] if row else 0
        
        if attempts >= MAX_CODE_ATTEMPTS:
            await message.answer(f"❌ Превышено количество попыток ({MAX_CODE_ATTEMPTS}). Начните заново с /start")
            await client.disconnect()
            await state.clear()
            await db.execute("DELETE FROM auth_temp WHERE user_id = ?", (user_id,))
            await db.commit()
            return
        
        await db.execute(
            "UPDATE auth_temp SET code_attempts = ? WHERE user_id = ?",
            (attempts + 1, user_id)
        )
        await db.commit()
    
    try:
        user = await client.sign_in(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=code
        )
        
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT api_id, api_hash FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            api_id, api_hash = row if row else (None, None)
        
        session_string = await client.export_session_string()
        await save_user_session(user_id, session_string, user, phone, api_id, api_hash)
        
        await client.disconnect()
        await state.clear()
        
        await message.answer(
            f"✅ Аккаунт @{user.username or user.first_name} успешно подключен!\n\n"
            f"Вам активирован тестовый период на {TEST_PERIOD_DAYS} дня.\n"
            f"Дата окончания: {(datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).strftime('%d.%m.%Y')}\n\n"
            f"Доступные команды в меню ниже:",
            reply_markup=get_main_keyboard()
        )
        
        logger.info(f"User {user_id} successfully authorized account @{user.username}")
        
    except SessionPasswordNeeded:
        await state.set_state(AuthStates.waiting_2fa_password)
        await message.answer(
            "🔐 Требуется пароль двухфакторной аутентификации (2FA)\n\n"
            f"Максимум попыток: {MAX_2FA_ATTEMPTS}\n\n"
            "Введите пароль:"
        )
        
    except PhoneCodeInvalid:
        await message.answer("❌ Неверный код. Попробуйте снова:")
    except Exception as e:
        logger.error(f"Error during sign in: {e}")
        await message.answer("❌ Ошибка при входе. Попробуйте позже.")
        await client.disconnect()
        await state.clear()

@dp.message(AuthStates.waiting_2fa_password)
async def auth_get_2fa(message: types.Message, state: FSMContext):
    """Обработка 2FA пароля"""
    password = message.text.strip()
    user_id = message.from_user.id
    
    data = await state.get_data()
    client = data.get('client')
    phone = data.get('phone')
    
    if not client:
        await message.answer("❌ Ошибка сессии. Начните заново с /start")
        await state.clear()
        return
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT password_attempts FROM auth_temp WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        attempts = row[0] if row else 0
        
        if attempts >= MAX_2FA_ATTEMPTS:
            await message.answer(f"❌ Превышено количество попыток ({MAX_2FA_ATTEMPTS}). Начните заново с /start")
            await client.disconnect()
            await state.clear()
            await db.execute("DELETE FROM auth_temp WHERE user_id = ?", (user_id,))
            await db.commit()
            return
        
        await db.execute(
            "UPDATE auth_temp SET password_attempts = ? WHERE user_id = ?",
            (attempts + 1, user_id)
        )
        await db.commit()
    
    try:
        user = await client.check_password(password)
        
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT api_id, api_hash FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            api_id, api_hash = row if row else (None, None)
        
        session_string = await client.export_session_string()
        await save_user_session(user_id, session_string, user, phone, api_id, api_hash)
        
        await client.disconnect()
        await state.clear()
        
        await message.answer(
            f"✅ Аккаунт @{user.username or user.first_name} успешно подключен!\n\n"
            f"Вам активирован тестовый период на {TEST_PERIOD_DAYS} дня.\n"
            f"Дата окончания: {(datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).strftime('%d.%m.%Y')}\n\n"
            f"Доступные команды в меню ниже:",
            reply_markup=get_main_keyboard()
        )
        
        logger.info(f"User {user_id} successfully authorized with 2FA account @{user.username}")
        
    except PasswordHashInvalid:
        remaining = MAX_2FA_ATTEMPTS - (attempts + 1)
        await message.answer(f"❌ Неверный пароль. Осталось попыток: {remaining}")
    except Exception as e:
        logger.error(f"Error during 2FA: {e}")
        await message.answer("❌ Ошибка при проверке пароля. Попробуйте позже.")
        await client.disconnect()
        await state.clear()

# ================== ПРОВЕРКА СПАМ-БЛОКА ==================

@dp.message(Command("spam"))
@dp.message(F.text == "🚫 Проверить спам-блок")
async def cmd_spam(message: types.Message, state: FSMContext):
    """Проверка спам-блока"""
    user_id = message.from_user.id
    
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку в профиле 👤",
            reply_markup=get_main_keyboard()
        )
        return
    
    status_msg = await message.answer("🔄 Проверяю статус аккаунта у @spambot...")
    
    success, status, unlock_date = await check_spam_status(user_id)
    
    if not success:
        await status_msg.edit_text(
            f"❌ {status}\n\n"
            "Попробуйте позже или проверьте вручную: @spambot"
        )
        return
    
    result_text = f"🔍 **РЕЗУЛЬТАТ ПРОВЕРКИ:**\n\n{status}"
    if unlock_date:
        result_text += f"\n📅 **Предполагаемая дата разблокировки:** {unlock_date}"
    
    await status_msg.edit_text(result_text, parse_mode="Markdown")
    await log_usage(user_id, "spam_check", status)

# ================== ПРОСМОТР ЧАТОВ ==================

@dp.message(Command("chats"))
@dp.message(F.text == "💬 Мои чаты")
async def cmd_chats(message: types.Message, state: FSMContext):
    """Просмотр всех чатов"""
    user_id = message.from_user.id
    
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку.",
            reply_markup=get_main_keyboard()
        )
        return
    
    status_msg = await message.answer("🔄 Загружаю список всех чатов...")
    
    client = await get_pyro_client(user_id)
    if not client:
        await status_msg.edit_text(
            "❌ Ошибка: не удалось подключиться к аккаунту.\n"
            "Попробуйте переавторизоваться в профиле."
        )
        return
    
    try:
        dialogs = await get_all_dialogs(client)
        
        if not dialogs:
            await status_msg.edit_text(
                "📭 У вас нет активных чатов.\n\n"
                "Возможные причины:\n"
                "• Аккаунт новый\n"
                "• Все чаты архивированы\n"
                "• Ошибка доступа"
            )
            await client.stop()
            return
        
        dialogs.sort(key=lambda x: (-x.get('pinned', 0), x.get('last_message_date') or datetime.min), reverse=True)
        
        await state.update_data(all_dialogs=dialogs, current_page=0)
        
        await show_chats_page(message, dialogs, 0, status_msg)
        
        await log_usage(user_id, "view_chats", f"Total: {len(dialogs)}")
        
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка при загрузке чатов: {str(e)[:100]}\n\n"
            "Попробуйте позже."
        )
    finally:
        await client.stop()

async def show_chats_page(message: types.Message, dialogs: List[Dict], page: int, edit_msg: types.Message = None):
    """Показать страницу чатов"""
    start = page * CHATS_PER_PAGE
    end = start + CHATS_PER_PAGE
    page_dialogs = dialogs[start:end]
    
    if not page_dialogs:
        if edit_msg:
            await edit_msg.edit_text("Чатов больше нет")
        return
    
    total_pages = (len(dialogs) + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE
    text = f"💬 **Мои чаты** (страница {page + 1}/{total_pages}):\n\n"
    
    for i, dialog in enumerate(page_dialogs, start + 1):
        if dialog['type'] == ChatType.PRIVATE:
            icon = "🤖" if dialog['is_bot'] else "👤"
        elif dialog['type'] in [ChatType.GROUP, ChatType.SUPERGROUP]:
            icon = "👥"
        elif dialog['type'] == ChatType.CHANNEL:
            icon = "📢"
        else:
            icon = "💬"
        
        name = dialog['title']
        if len(name) > 30:
            name = name[:27] + "..."
        
        info = []
        if dialog.get('pinned'):
            info.append("📌")
        if dialog.get('unread_count', 0) > 0:
            info.append(f"💬{dialog['unread_count']}")
        if dialog.get('members_count', 0) > 0:
            info.append(f"👥{dialog['members_count']}")
        
        info_str = f" [{', '.join(info)}]" if info else ""
        
        text += f"{i}. {icon} {name}{info_str}\n"
    
    builder = InlineKeyboardBuilder()
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"chats_page_{page-1}"))
    if end < len(dialogs):
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"chats_page_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.row(InlineKeyboardButton(text="🔍 Выбрать чат", callback_data="select_chat"))
    builder.row(InlineKeyboardButton(text="⭐ Избранное", callback_data="show_favorites"))
    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main"))
    
    if edit_msg:
        await edit_msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("chats_page_"))
async def chats_page_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обработка пагинации чатов"""
    page = int(callback.data.replace("chats_page_", ""))
    data = await state.get_data()
    dialogs = data.get('all_dialogs', [])
    
    if dialogs:
        await show_chats_page(callback.message, dialogs, page, callback.message)
    
    await callback.answer()

@dp.callback_query(F.data == "select_chat")
async def select_chat(callback: types.CallbackQuery, state: FSMContext):
    """Выбор чата для действий"""
    await callback.message.edit_text(
        "🔍 **Выберите чат**\n\n"
        "Отправьте номер чата из списка:",
        parse_mode="Markdown"
    )
    await state.set_state(ChatSelectionStates.waiting_chat_number)
    await callback.answer()

@dp.message(ChatSelectionStates.waiting_chat_number)
async def process_chat_selection(message: types.Message, state: FSMContext):
    """Обработка выбора чата по номеру"""
    if not message.text.isdigit():
        await message.answer("❌ Пожалуйста, введите номер чата цифрами")
        return
    
    chat_num = int(message.text) - 1
    data = await state.get_data()
    dialogs = data.get('all_dialogs', [])
    
    if 0 <= chat_num < len(dialogs):
        chat = dialogs[chat_num]
        await show_chat_actions(message, chat)
        await state.clear()
    else:
        await message.answer(f"❌ Чат с номером {message.text} не найден. Введите номер от 1 до {len(dialogs)}")

async def show_chat_actions(message: types.Message, chat: Dict):
    """Показать действия для чата"""
    text = f"**Чат:** {chat['title']}\n"
    text += f"**Тип:** {str(chat['type']).split('.')[-1]}\n"
    if chat.get('username'):
        text += f"**Username:** @{chat['username']}\n"
    if chat.get('members_count'):
        text += f"**Участников:** {chat['members_count']}\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add_{chat['id']}|{chat['title']}"),
        InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"chat_info_{chat['id']}")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_chats"))
    
    await message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

# ================== ИНФОРМАЦИЯ О ЧАТЕ ==================

@dp.message(F.text == "📊 Инфо о чате")
async def cmd_chat_info(message: types.Message, state: FSMContext):
    """Получить информацию о чате"""
    await message.answer(
        "📊 **Информация о чате**\n\n"
        "Отправьте ссылку, ID или username чата:\n"
        "Пример: @username или -100123456789",
        parse_mode="Markdown"
    )
    await state.set_state(ChatInfoStates.waiting_chat_input)

@dp.message(ChatInfoStates.waiting_chat_input)
async def process_chat_info(message: types.Message, state: FSMContext):
    """Обработка запроса информации о чате"""
    user_id = message.from_user.id
    
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(f"⚠️ {error_msg}")
        await state.clear()
        return
    
    status_msg = await message.answer("🔄 Получаю информацию...")
    
    client = await get_pyro_client(user_id)
    if not client:
        await status_msg.edit_text("❌ Ошибка подключения к аккаунту")
        await state.clear()
        return
    
    try:
        chat_input = message.text.strip()
        
        if chat_input.startswith('@'):
            chat = await client.get_chat(chat_input)
        elif chat_input.lstrip('-').isdigit():
            chat = await client.get_chat(int(chat_input))
        else:
            await status_msg.edit_text("❌ Неверный формат. Используйте @username или ID")
            await client.stop()
            await state.clear()
            return
        
        text = f"📊 **Информация о чате**\n\n"
        text += f"**Название:** {chat.title or chat.first_name or 'Без названия'}\n"
        text += f"**Тип:** {str(chat.type).split('.')[-1]}\n"
        
        if chat.username:
            text += f"**Username:** @{chat.username}\n"
        
        if chat.description:
            desc = chat.description[:100] + "..." if len(chat.description) > 100 else chat.description
            text += f"**Описание:** {desc}\n"
        
        if hasattr(chat, 'members_count') and chat.members_count:
            text += f"**Участников:** {chat.members_count}\n"
        
        if chat.type == ChatType.PRIVATE:
            if chat.first_name:
                text += f"**Имя:** {chat.first_name} {chat.last_name or ''}\n"
            if chat.is_bot:
                text += f"**Это бот:** Да\n"
            if hasattr(chat, 'phone_number') and chat.phone_number:
                text += f"**Телефон:** {chat.phone_number}\n"
        
        text += f"\n**ID:** `{chat.id}`"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add_{chat.id}|{chat.title or chat.first_name}"))
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
        
        await status_msg.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        
        await log_usage(user_id, "chat_info", str(chat.id))
        
    except UsernameNotOccupied:
        await status_msg.edit_text("❌ Чат с таким username не найден")
    except ChatIdInvalid:
        await status_msg.edit_text("❌ Неверный ID чата")
    except Exception as e:
        logger.error(f"Error getting chat info: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    finally:
        await client.stop()
        await state.clear()

# ================== ИЗБРАННОЕ ==================

@dp.message(F.text == "⭐ Избранное")
async def cmd_favorites(message: types.Message):
    """Просмотр избранных чатов"""
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM favorite_chats WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,)
        )
        favorites = await cursor.fetchall()
    
    if not favorites:
        await message.answer(
            "⭐ **Избранное**\n\n"
            "У вас пока нет избранных чатов.\n"
            "Добавляйте чаты в избранное из списка чатов или через информацию о чате.",
            parse_mode="Markdown"
        )
        return
    
    builder = InlineKeyboardBuilder()
    
    for fav in favorites:
        title = fav['chat_title'][:30] + "..." if len(fav['chat_title']) > 30 else fav['chat_title']
        builder.row(InlineKeyboardButton(
            text=f"⭐ {title}",
            callback_data=f"fav_open_{fav['chat_id']}"
        ))
    
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await message.answer(
        "⭐ **Избранные чаты:**",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("fav_add_"))
async def add_to_favorites(callback: types.CallbackQuery):
    """Добавить чат в избранное"""
    user_id = callback.from_user.id
    data = callback.data.replace("fav_add_", "")
    
    if '|' in data:
        chat_id, chat_title = data.split('|', 1)
        chat_id = int(chat_id)
    else:
        chat_id = int(data)
        chat_title = f"Чат {chat_id}"
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO favorite_chats (user_id, chat_id, chat_title)
            VALUES (?, ?, ?)
        ''', (user_id, chat_id, chat_title))
        await db.commit()
    
    await callback.answer("✅ Добавлено в избранное")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("fav_open_"))
async def open_favorite(callback: types.CallbackQuery):
    """Открыть избранный чат"""
    chat_id = int(callback.data.replace("fav_open_", ""))
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"chat_info_{chat_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await callback.message.answer(
        f"Выберите действие для чата:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

# ================== ПРОФИЛЬ И ПОДПИСКА ==================

@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: types.Message):
    """Просмотр профиля"""
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await message.answer(
            "❌ Аккаунт не найден. Начните с /start для авторизации."
        )
        return
    
    sub_until = datetime.fromisoformat(user['subscription_until'])
    days_left = (sub_until - datetime.now()).days
    
    if days_left > 0:
        status = f"✅ Активна (осталось {days_left} дн.)"
    else:
        status = "❌ Истекла"
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM usage_stats WHERE user_id = ?",
            (user_id,)
        )
        total_actions = (await cursor.fetchone())[0]
    
    profile_text = (
        f"👤 **ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ**\n\n"
        f"**ID в боте:** `{user_id}`\n"
        f"**Подключенный аккаунт:** @{user['account_username'] or 'не указан'}\n"
        f"**Имя:** {user['account_first_name']} {user['account_last_name'] or ''}\n"
        f"**Номер:** {user['phone_number']}\n"
        f"**ID аккаунта:** `{user.get('account_id', 'неизвестно')}`\n\n"
        f"📅 **Подписка активна до:** {sub_until.strftime('%d.%m.%Y')}\n"
        f"**Статус:** {status}\n"
        f"**Всего действий:** {total_actions}\n\n"
        f"*Нажмите кнопку для управления*"
    )
    
    await message.answer(
        profile_text,
        reply_markup=get_profile_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: types.CallbackQuery):
    """Статистика использования"""
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM usage_stats WHERE user_id = ?",
            (user_id,)
        )
        total = (await cursor.fetchone())[0]
        
        cursor = await db.execute('''
            SELECT date(timestamp), COUNT(*) 
            FROM usage_stats 
            WHERE user_id = ? 
            GROUP BY date(timestamp) 
            ORDER BY date(timestamp) DESC 
            LIMIT 7
        ''', (user_id,))
        daily = await cursor.fetchall()
        
        cursor = await db.execute('''
            SELECT action, COUNT(*) 
            FROM usage_stats 
            WHERE user_id = ? 
            GROUP BY action 
            ORDER BY COUNT(*) DESC
        ''', (user_id,))
        by_action = await cursor.fetchall()
    
    text = f"📊 **Ваша статистика**\n\n"
    text += f"Всего действий: {total}\n\n"
    
    text += "**Последние 7 дней:**\n"
    for date, count in daily:
        text += f"• {date}: {count}\n"
    
    text += "\n**По функциям:**\n"
    for action, count in by_action[:5]:
        action_name = {
            "spam_check": "Проверка спама",
            "view_chats": "Просмотр чатов",
            "chat_info": "Инфо о чате",
        }.get(action, action)
        text += f"• {action_name}: {count}\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
        ),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "extend_subscription")
async def extend_subscription(callback: types.CallbackQuery):
    """Меню продления подписки"""
    await callback.message.edit_text(
        "💳 **Продление подписки**\n\n"
        "Выберите тариф:\n"
        "• 1 месяц — 100₽\n"
        "• 3 месяца — 250₽\n"
        "• Навсегда — 500₽\n\n"
        "Для оплаты свяжитесь с @admin",
        reply_markup=get_subscription_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("sub_"))
async def process_subscription(callback: types.CallbackQuery):
    """Обработка выбора тарифа"""
    sub_type = callback.data.replace("sub_", "")
    
    price = SUBSCRIPTION_PRICES.get(sub_type, 0)
    period = {
        "1_month": "1 месяц",
        "3_months": "3 месяца",
        "forever": "навсегда"
    }.get(sub_type, sub_type)
    
    await callback.message.edit_text(
        f"✅ Вы выбрали тариф: **{period}** — {price}₽\n\n"
        f"Для оплаты:\n"
        f"1. Переведите {price}₽ на карту: `1234 5678 9012 3456`\n"
        f"2. Отправьте скриншот оплаты @admin\n"
        f"3. После подтверждения подписка будет активирована",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="extend_subscription")]
            ]
        ),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "reset_session")
async def reset_session(callback: types.CallbackQuery):
    """Сброс сессии"""
    user_id = callback.from_user.id
    
    await callback.message.edit_text(
        "⚠️ **Вы уверены?**\n\n"
        "Это действие удалит текущую сессию и потребует повторной авторизации.\n"
        "Все данные будут удалены безвозвратно.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, сбросить", callback_data="confirm_reset")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_profile")]
            ]
        ),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение сброса сессии"""
    user_id = callback.from_user.id
    
    await clear_user_session(user_id)
    await state.clear()
    
    await callback.message.edit_text(
        "✅ Сессия успешно сброшена.\n\n"
        "Для повторной авторизации нажмите /start"
    )
    await callback.answer()

# ================== НАСТРОЙКИ ==================

@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    """Настройки пользователя"""
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await message.answer("Сначала авторизуйтесь через /start")
        return
    
    await message.answer(
        "⚙️ **Настройки**\n\n"
        "Здесь вы можете настроить поведение бота:",
        reply_markup=get_settings_keyboard(user),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "toggle_auto_clean")
async def toggle_auto_clean(callback: types.CallbackQuery):
    """Переключить автоочистку спам-бота"""
    user_id = callback.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await callback.answer("Ошибка")
        return
    
    new_value = 0 if user.get('auto_clean_spam', 0) else 1
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET auto_clean_spam = ? WHERE user_id = ?",
            (new_value, user_id)
        )
        await db.commit()
    
    user['auto_clean_spam'] = new_value
    await callback.message.edit_reply_markup(
        reply_markup=get_settings_keyboard(user)
    )
    await callback.answer("✅ Настройка сохранена")

@dp.callback_query(F.data == "toggle_notify")
async def toggle_notify(callback: types.CallbackQuery):
    """Переключить уведомления"""
    user_id = callback.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await callback.answer("Ошибка")
        return
    
    new_value = 0 if user.get('notify_expiration', 1) else 1
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET notify_expiration = ? WHERE user_id = ?",
            (new_value, user_id)
        )
        await db.commit()
    
    user['notify_expiration'] = new_value
    await callback.message.edit_reply_markup(
        reply_markup=get_settings_keyboard(user)
    )
    await callback.answer("✅ Настройка сохранена")

# ================== ВОЗВРАТ В МЕНЮ ==================

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    """Вернуться в главное меню"""
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: types.CallbackQuery):
    """Вернуться в профиль"""
    await cmd_profile(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "back_to_chats")
async def back_to_chats(callback: types.CallbackQuery, state: FSMContext):
    """Вернуться к списку чатов"""
    data = await state.get_data()
    dialogs = data.get('all_dialogs', [])
    
    if dialogs:
        await show_chats_page(callback.message, dialogs, 0, callback.message)
    else:
        await cmd_chats(callback.message, state)
    
    await callback.answer()

# ================== АДМИН-ПАНЕЛЬ (УПРОЩЕННАЯ) ==================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Админ-панель"""
    await message.answer(
        "👨‍💻 **Панель администратора**\n\n"
        "Доступные команды:\n"
        "• /extend @user дни - продлить подписку\n"
        "• /stats - статистика бота\n"
        "• /users - список пользователей\n\n"
        "*Админ-панель доступна всем в демо-режиме*",
        parse_mode="Markdown"
    )

@dp.message(Command("extend"))
async def cmd_extend(message: types.Message):
    """Продлить подписку пользователя"""
    parts = message.text.split()
    
    if len(parts) != 3:
        await message.answer(
            "❌ Неправильный формат. Используйте:\n"
            "`/extend @username дни`\n"
            "или\n"
            "`/extend 123456789 дни`",
            parse_mode="Markdown"
        )
        return
    
    target, days_str = parts[1], parts[2]
    
    if target.startswith('@'):
        username = target[1:]
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE account_username = ?",
                (username,)
            )
            row = await cursor.fetchone()
            if not row:
                await message.answer(f"❌ Пользователь {target} не найден")
                return
            target_id = row[0]
    else:
        try:
            target_id = int(target)
        except ValueError:
            await message.answer("❌ Неверный формат ID")
            return
    
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("❌ Количество дней должно быть числом")
        return
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT subscription_until FROM users WHERE user_id = ?",
            (target_id,)
        )
        row = await cursor.fetchone()
        
        if not row:
            await message.answer(f"❌ Пользователь с ID {target_id} не найден")
            return
        
        current = row[0]
        if current:
            new_date = datetime.fromisoformat(current) + timedelta(days=days)
        else:
            new_date = datetime.now() + timedelta(days=days)
        
        await db.execute(
            "UPDATE users SET subscription_until = ? WHERE user_id = ?",
            (new_date.isoformat(), target_id)
        )
        await db.commit()
    
    await message.answer(
        f"✅ Подписка пользователя {target} продлена на {days} дней\n"
        f"Новая дата окончания: {new_date.strftime('%d.%m.%Y')}"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Статистика бота"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]
        
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE subscription_until > ?",
            (now,)
        )
        active_subs = (await cursor.fetchone())[0]
        
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE created_at > ?",
            (yesterday,)
        )
        new_today = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT COUNT(*) FROM usage_stats")
        total_actions = (await cursor.fetchone())[0]
    
    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Активных подписок: {active_subs}\n"
        f"📈 Новых за 24ч: {new_today}\n"
        f"🔄 Всего действий: {total_actions}"
    )
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("users"))
async def cmd_users(message: types.Message):
    """Список пользователей"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, account_username, account_first_name, subscription_until, created_at FROM users ORDER BY created_at DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
    
    if not rows:
        await message.answer("Пользователей пока нет")
        return
    
    text = "📋 **Последние 20 пользователей:**\n\n"
    
    for row in rows:
        user_id = row['user_id']
        username = row['account_username'] or 'нет'
        name = row['account_first_name'] or 'Unknown'
        sub_until = row['subscription_until']
        
        if sub_until:
            try:
                sub_date = datetime.fromisoformat(sub_until).strftime('%d.%m.%Y')
                if datetime.fromisoformat(sub_until) > datetime.now():
                    status = "✅"
                else:
                    status = "❌"
            except:
                sub_date = "ошибка"
                status = "❌"
        else:
            sub_date = "нет"
            status = "❌"
        
        created = datetime.fromisoformat(row['created_at']).strftime('%d.%m')
        
        text += f"`{user_id}` | @{username} | {name} | {status} до {sub_date} | 📅 {created}\n"
    
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await message.answer(part, parse_mode="Markdown")
    else:
        await message.answer(text, parse_mode="Markdown")

# ================== ОБЩИЕ ОБРАБОТЧИКИ ==================

@dp.message()
async def handle_unknown(message: types.Message, state: FSMContext):
    """Обработка неизвестных команд"""
    current_state = await state.get_state()
    
    if current_state:
        # Если мы в каком-то состоянии, игнорируем - сообщение уже обработано
        return
    
    if message.text and message.text.startswith('/'):
        await message.answer(
            "❌ Неизвестная команда.\n"
            "Доступные команды: /start, /spam, /chats, /profile, /admin"
        )

# ================== ФОНОВЫЕ ЗАДАЧИ ==================

async def check_subscription_expirations():
    """Проверка истекающих подписок"""
    while True:
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                warning_date = (datetime.now() + timedelta(days=3)).isoformat()
                cursor = await db.execute('''
                    SELECT user_id, account_first_name, subscription_until 
                    FROM users 
                    WHERE subscription_until <= ? AND notify_expiration = 1
                ''', (warning_date,))
                expiring = await cursor.fetchall()
                
                for user_id, name, sub_until in expiring:
                    try:
                        sub_date = datetime.fromisoformat(sub_until).strftime('%d.%m.%Y')
                        await bot.send_message(
                            user_id,
                            f"⚠️ **Уведомление о подписке**\n\n"
                            f"Уважаемый {name}, ваша подписка истекает {sub_date}.\n"
                            f"Продлите её в профиле, чтобы продолжить пользоваться ботом.",
                            parse_mode="Markdown"
                        )
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.error(f"Error sending expiration notice to {user_id}: {e}")
            
            await asyncio.sleep(86400)  # Проверка раз в день
            
        except Exception as e:
            logger.error(f"Error in subscription checker: {e}")
            await asyncio.sleep(3600)

# ================== ЗАПУСК БОТА ==================

async def on_startup():
    """Действия при запуске"""
    await init_db()
    asyncio.create_task(check_subscription_expirations())
    logger.info("Bot started!")
    logger.info(f"Bot token: {BOT_TOKEN[:10]}...")

async def on_shutdown():
    """Действия при остановке"""
    logger.info("Bot stopped!")

async def main():
    """Главная функция"""
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
