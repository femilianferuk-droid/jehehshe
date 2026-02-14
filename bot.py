"""
Telegram Bot для управления аккаунтами через Pyrogram
Исправленная версия с новыми функциями
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
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneNumberInvalid, PhoneCodeInvalid, 
    PasswordHashInvalid, AuthKeyUnregistered, FloodWait, PeerIdInvalid,
    UsernameNotOccupied, ChatAdminRequired
)
from pyrogram.types import User as PyroUser
from pyrogram.enums import ChatType
from dotenv import load_dotenv

# ================== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==================
load_dotenv()

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# ADMIN_IDS теперь необязательный - функционал админки будет доступен всем
ADMIN_IDS = []
if os.getenv("ADMIN_IDS"):
    try:
        ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS").split(",")]
    except:
        logging.warning("Не удалось распарсить ADMIN_IDS, админ-панель будет доступна всем")

SUBSCRIPTION_PRICES = {
    "1_month": 100,
    "3_months": 250,
    "forever": 500
}
TEST_PERIOD_DAYS = 3
SPAM_BOT_USERNAME = "spambot"
SPAM_BOT_TIMEOUT = 10  # Увеличил таймаут
CHATS_PER_PAGE = 10
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
                subscription_until TIMESTAMP,
                language TEXT DEFAULT 'ru',
                auto_clean_spam INTEGER DEFAULT 0,
                notify_expiration INTEGER DEFAULT 1,
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
        
        # Таблица кэша чатов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chats_cache (
                user_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                chat_type TEXT,
                participants_count INTEGER,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для offset при пагинации
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_pagination (
                user_id INTEGER PRIMARY KEY,
                last_offset_id INTEGER DEFAULT 0,
                all_loaded BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для статистики использования
        await db.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
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

class AdminStates(StatesGroup):
    waiting_extend_user_id = State()
    waiting_extend_days = State()

class BroadcastStates(StatesGroup):
    waiting_message = State()
    confirm_send = State()

class SettingsStates(StatesGroup):
    waiting_auto_clean = State()
    waiting_notify = State()

class ForwardStates(StatesGroup):
    waiting_from_chat = State()
    waiting_to_chat = State()
    waiting_confirm = State()

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
             account_first_name, account_last_name, subscription_until, api_id, api_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, 
            account_info.username or f"user_{user_id}",
            session_string,
            phone_number,
            account_info.username,
            account_info.first_name,
            account_info.last_name or "",
            subscription_until,
            api_id,
            api_hash
        ))
        
        # Очищаем временные данные
        await db.execute("DELETE FROM auth_temp WHERE user_id = ?", (user_id,))
        await db.commit()
        
        logger.info(f"User {user_id} saved session for @{account_info.username}")

async def clear_user_session(user_id: int):
    """Очистить сессию пользователя"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM auth_temp WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM chats_cache WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM chat_pagination WHERE user_id = ?", (user_id,))
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
    
    subscription_until = datetime.fromisoformat(user['subscription_until'])
    if datetime.now() > subscription_until:
        return False, f"Срок подписки истек {subscription_until.strftime('%d.%m.%Y')}"
    
    # Проверяем, не скоро ли истекает (за 3 дня)
    days_left = (subscription_until - datetime.now()).days
    if days_left <= 3 and days_left > 0 and user.get('notify_expiration', 1):
        # Здесь можно отправить уведомление
        pass
    
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
        
        # Проверяем, что клиент работает
        me = await client.get_me()
        if me:
            logger.info(f"Client started for user {user_id} (@{me.username})")
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

async def log_usage(user_id: int, action: str):
    """Логировать использование функций"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO usage_stats (user_id, action) VALUES (?, ?)",
            (user_id, action)
        )
        await db.commit()

# ================== НОВЫЕ ФУНКЦИИ ==================

async def check_spam_bot_direct(user_id: int) -> Tuple[bool, str, Optional[str]]:
    """
    Проверка спам-блока через прямой запрос к @spambot
    Возвращает: (успех, статус, дата разблокировки)
    """
    client = None
    try:
        client = await get_pyro_client(user_id)
        if not client:
            return False, "Ошибка подключения к аккаунту", None
        
        # Получаем информацию о пользователе
        me = await client.get_me()
        
        # Пытаемся найти @spambot
        try:
            spambot = await client.get_users(SPAM_BOT_USERNAME)
        except UsernameNotOccupied:
            return False, "Бот @spambot не найден", None
        
        # Отправляем команду /start
        await client.send_message(spambot.id, "/start")
        
        # Ждем ответ (увеличил таймаут до 10 секунд)
        await asyncio.sleep(2)
        
        # Получаем последние сообщения
        messages = []
        async for msg in client.get_chat_history(spambot.id, limit=5):
            if msg.from_user and msg.from_user.id == spambot.id:
                messages.append(msg)
        
        if not messages:
            return False, "Нет ответа от @spambot", None
        
        # Анализируем последнее сообщение
        last_msg = messages[0]
        text = last_msg.text or last_msg.caption or ""
        
        # Определяем статус
        is_restricted = False
        unlock_date = None
        
        # Русский язык
        if re.search(r'(ограничены|заблокированы|имеются ограничения)', text.lower()):
            is_restricted = True
        elif re.search(r'(добро пожаловать|вы не ограничены|нет ограничений)', text.lower()):
            is_restricted = False
        
        # Английский язык
        if re.search(r'(restricted|limited|banned)', text.lower()):
            is_restricted = True
        elif re.search(r'(welcome|is not restricted|no restrictions)', text.lower()):
            is_restricted = False
        
        # Ищем дату
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
        
        # Очищаем чат если включена настройка
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

async def get_user_dialogs(client: Client, offset_id: int = 0, limit: int = CHATS_PER_PAGE) -> List[Dict]:
    """Получить диалоги пользователя с дополнительной информацией"""
    dialogs = []
    try:
        async for dialog in client.get_dialogs(offset_id=offset_id, limit=limit):
            chat = dialog.chat
            dialog_info = {
                'id': chat.id,
                'title': chat.title or chat.first_name or "Unknown",
                'type': chat.type,
                'username': chat.username,
                'is_bot': getattr(chat, 'is_bot', False),
                'last_message_date': dialog.top_message.date if dialog.top_message else None,
                'unread_count': dialog.unread_messages_count
            }
            
            # Получаем количество участников для групп/каналов
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

async def get_chat_info(client: Client, chat_id: int) -> Optional[Dict]:
    """Получить подробную информацию о чате"""
    try:
        chat = await client.get_chat(chat_id)
        info = {
            'id': chat.id,
            'title': chat.title or chat.first_name or "Unknown",
            'type': chat.type,
            'username': chat.username,
            'description': chat.description,
            'members_count': getattr(chat, 'members_count', 0),
            'linked_chat_id': getattr(chat, 'linked_chat_id', None)
        }
        
        if chat.type == ChatType.PRIVATE:
            info['first_name'] = chat.first_name
            info['last_name'] = chat.last_name
            info['is_bot'] = chat.is_bot
            info['phone_number'] = getattr(chat, 'phone_number', None)
        
        return info
    except Exception as e:
        logger.error(f"Error getting chat info: {e}")
        return None

async def add_to_favorites(user_id: int, chat_id: int, chat_title: str):
    """Добавить чат в избранное"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO favorite_chats (user_id, chat_id, chat_title)
            VALUES (?, ?, ?)
        ''', (user_id, chat_id, chat_title))
        await db.commit()

async def remove_from_favorites(user_id: int, chat_id: int):
    """Удалить чат из избранного"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM favorite_chats WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        await db.commit()

async def get_favorites(user_id: int) -> List[Dict]:
    """Получить список избранных чатов"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM favorite_chats WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

# ================== КЛАВИАТУРЫ ==================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚫 Проверить спам-блок")],
            [KeyboardButton(text="💬 Мои чаты"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="📊 Анализ чата"), KeyboardButton(text="📨 Рассылка")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )

def get_profile_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура профиля"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 ПРОДЛИТЬ ПОДПИСКУ", callback_data="extend_subscription"))
    builder.row(InlineKeyboardButton(text="🔄 СБРОСИТЬ СЕССИЮ", callback_data="reset_session"))
    builder.row(InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"))
    builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

def get_settings_keyboard(auto_clean: int, notify: int) -> InlineKeyboardMarkup:
    """Клавиатура настроек"""
    builder = InlineKeyboardBuilder()
    
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

def get_chats_keyboard(has_more: bool = False, chat_id: int = None) -> InlineKeyboardMarkup:
    """Клавиатура для чатов"""
    builder = InlineKeyboardBuilder()
    
    if chat_id:
        builder.row(
            InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add_{chat_id}"),
            InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"chat_info_{chat_id}")
        )
    
    if has_more:
        builder.row(InlineKeyboardButton(text="⬇️ Загрузить еще 10", callback_data="load_more_chats"))
    
    builder.row(
        InlineKeyboardButton(text="⭐ Избранное", callback_data="show_favorites"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")
    )
    
    return builder.as_markup()

def get_favorites_keyboard(favorites: List[Dict]) -> InlineKeyboardMarkup:
    """Клавиатура избранных чатов"""
    builder = InlineKeyboardBuilder()
    
    for fav in favorites[:10]:  # Показываем только 10 последних
        title = fav['chat_title'][:20] + "..." if len(fav['chat_title']) > 20 else fav['chat_title']
        builder.row(InlineKeyboardButton(
            text=f"⭐ {title}",
            callback_data=f"fav_open_{fav['chat_id']}"
        ))
    
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    return builder.as_markup()

def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Админ-клавиатура"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="➕ Продлить подписку", callback_data="admin_extend"))
    builder.row(InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_users"))
    builder.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast"))
    builder.row(InlineKeyboardButton(text="📈 Активность", callback_data="admin_activity"))
    return builder.as_markup()

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
            "🔑 ШАГ 1 ИЗ 4: Введите API ID\n\n"
            "API ID - это целое число, которое можно получить на https://my.telegram.org/apps\n\n"
            "Отправьте API ID:",
            reply_markup=ReplyKeyboardRemove()
        )

# ================== ИСПРАВЛЕННАЯ ПРОВЕРКА СПАМ-БЛОКА ==================

@dp.message(Command("spam"))
@dp.message(F.text == "🚫 Проверить спам-блок")
async def cmd_spam(message: types.Message, state: FSMContext):
    """Проверка спам-блока"""
    user_id = message.from_user.id
    
    # Проверка подписки
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку в профиле 👤",
            reply_markup=get_main_keyboard()
        )
        return
    
    status_msg = await message.answer("🔄 Проверяю статус аккаунта у @spambot...")
    
    # Используем новую функцию
    success, status, unlock_date = await check_spam_bot_direct(user_id)
    
    if not success:
        await status_msg.edit_text(
            f"❌ {status}\n\n"
            "Попробуйте позже или проверьте вручную: @spambot"
        )
        return
    
    # Формируем результат
    result_text = f"🔍 **РЕЗУЛЬТАТ ПРОВЕРКИ:**\n\n{status}"
    if unlock_date:
        result_text += f"\n📅 **Предполагаемая дата разблокировки:** {unlock_date}"
    
    # Добавляем информацию о пользователе
    client = await get_pyro_client(user_id)
    if client:
        try:
            me = await client.get_me()
            result_text = f"Аккаунт: @{me.username or me.first_name}\n\n" + result_text
            await client.stop()
        except:
            pass
    
    await status_msg.edit_text(result_text, parse_mode="Markdown")
    await log_usage(user_id, "spam_check")

# ================== ИСПРАВЛЕННЫЙ ПРОСМОТР ЧАТОВ ==================

@dp.message(Command("chats"))
@dp.message(F.text == "💬 Мои чаты")
async def cmd_chats(message: types.Message, state: FSMContext):
    """Просмотр чатов"""
    user_id = message.from_user.id
    
    # Проверка подписки
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку в профиле 👤",
            reply_markup=get_main_keyboard()
        )
        return
    
    status_msg = await message.answer("🔄 Загружаю список чатов...")
    
    # Получаем клиент
    client = await get_pyro_client(user_id)
    if not client:
        await status_msg.edit_text(
            "❌ Ошибка: не удалось подключиться к аккаунту.\n"
            "Попробуйте переавторизоваться в профиле."
        )
        return
    
    try:
        # Получаем offset из БД
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT last_offset_id FROM chat_pagination WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            offset_id = row[0] if row else 0
        
        # Получаем диалоги
        dialogs = await get_user_dialogs(client, offset_id=offset_id, limit=CHATS_PER_PAGE)
        
        if not dialogs:
            await status_msg.edit_text("📭 У вас нет активных чатов.")
            await client.stop()
            return
        
        # Сохраняем в кэш и обновляем offset
        async with aiosqlite.connect(DATABASE_PATH) as db:
            for dialog in dialogs:
                await db.execute('''
                    INSERT OR REPLACE INTO chats_cache 
                    (user_id, chat_id, chat_title, chat_type, participants_count)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    user_id, dialog['id'], 
                    dialog['title'],
                    str(dialog['type']).split(".")[-1],
                    dialog.get('members_count', 0)
                ))
            
            # Обновляем offset
            last_id = dialogs[-1]['id'] if dialogs else 0
            has_more = len(dialogs) == CHATS_PER_PAGE
            
            await db.execute('''
                INSERT OR REPLACE INTO chat_pagination (user_id, last_offset_id, all_loaded)
                VALUES (?, ?, ?)
            ''', (user_id, last_id, not has_more))
            
            await db.commit()
        
        # Формируем сообщение
        chats_text = "💬 **Мои чаты:**\n\n"
        
        for i, dialog in enumerate(dialogs, 1):
            # Определяем иконку
            if dialog['type'] == ChatType.PRIVATE:
                icon = "🤖" if dialog['is_bot'] else "👤"
            elif dialog['type'] in [ChatType.GROUP, ChatType.SUPERGROUP]:
                icon = "👥"
            elif dialog['type'] == ChatType.CHANNEL:
                icon = "📢"
            else:
                icon = "💬"
            
            # Название
            name = dialog['title']
            
            # Тип и количество участников
            type_str = str(dialog['type']).split(".")[-1].lower()
            members = dialog.get('members_count', 0)
            members_str = f", {members} уч." if members else ""
            
            # Непрочитанные
            unread = dialog.get('unread_count', 0)
            unread_str = f" [{unread} новых]" if unread else ""
            
            chats_text += f"{i}. {icon} {name} ({type_str}{members_str}){unread_str}\n"
        
        # Добавляем ID первого чата для кнопок
        first_chat_id = dialogs[0]['id'] if dialogs else None
        
        await status_msg.edit_text(
            chats_text,
            reply_markup=get_chats_keyboard(has_more, first_chat_id),
            parse_mode="Markdown"
        )
        
        await log_usage(user_id, "view_chats")
        
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка при загрузке чатов: {str(e)[:100]}\n\n"
            "Попробуйте позже."
        )
    finally:
        await client.stop()

@dp.callback_query(F.data == "load_more_chats")
async def load_more_chats(callback: types.CallbackQuery, state: FSMContext):
    """Загрузить еще чаты"""
    user_id = callback.from_user.id
    
    # Проверка подписки
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await callback.message.edit_text(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку."
        )
        return
    
    # Получаем клиент
    client = await get_pyro_client(user_id)
    if not client:
        await callback.message.edit_text("❌ Ошибка подключения к аккаунту.")
        return
    
    try:
        # Получаем offset
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT last_offset_id FROM chat_pagination WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            offset_id = row[0] if row else 0
        
        # Получаем диалоги
        dialogs = await get_user_dialogs(client, offset_id=offset_id, limit=CHATS_PER_PAGE)
        
        if not dialogs:
            await callback.message.edit_text(
                callback.message.text,
                reply_markup=get_chats_keyboard(False)
            )
            await callback.answer("Больше чатов нет")
            return
        
        # Обновляем offset
        async with aiosqlite.connect(DATABASE_PATH) as db:
            last_id = dialogs[-1]['id'] if dialogs else 0
            has_more = len(dialogs) == CHATS_PER_PAGE
            
            await db.execute('''
                INSERT OR REPLACE INTO chat_pagination (user_id, last_offset_id, all_loaded)
                VALUES (?, ?, ?)
            ''', (user_id, last_id, not has_more))
            await db.commit()
        
        # Добавляем к существующему списку
        current_text = callback.message.text or callback.message.caption or ""
        
        # Считаем текущее количество чатов
        lines = current_text.split('\n')
        chat_lines = [line for line in lines if line and line[0].isdigit()]
        current_count = len(chat_lines)
        
        for i, dialog in enumerate(dialogs, current_count + 1):
            if dialog['type'] == ChatType.PRIVATE:
                icon = "🤖" if dialog['is_bot'] else "👤"
            elif dialog['type'] in [ChatType.GROUP, ChatType.SUPERGROUP]:
                icon = "👥"
            elif dialog['type'] == ChatType.CHANNEL:
                icon = "📢"
            else:
                icon = "💬"
            
            name = dialog['title']
            type_str = str(dialog['type']).split(".")[-1].lower()
            members = dialog.get('members_count', 0)
            members_str = f", {members} уч." if members else ""
            unread = dialog.get('unread_count', 0)
            unread_str = f" [{unread} новых]" if unread else ""
            
            current_text += f"\n{i}. {icon} {name} ({type_str}{members_str}){unread_str}"
        
        await callback.message.edit_text(
            current_text,
            reply_markup=get_chats_keyboard(has_more)
        )
        
    except Exception as e:
        logger.error(f"Error loading more chats: {e}")
        await callback.answer("❌ Ошибка при загрузке", show_alert=True)
    finally:
        await client.stop()
    
    await callback.answer()

# ================== НОВЫЕ ФУНКЦИИ ==================

@dp.message(F.text == "⭐ Избранное")
async def cmd_favorites(message: types.Message):
    """Просмотр избранных чатов"""
    user_id = message.from_user.id
    
    favorites = await get_favorites(user_id)
    
    if not favorites:
        await message.answer(
            "⭐ У вас пока нет избранных чатов.\n"
            "Добавляйте чаты в избранное из списка чатов."
        )
        return
    
    text = "⭐ **Избранные чаты:**\n\n"
    for i, fav in enumerate(favorites[:10], 1):
        text += f"{i}. {fav['chat_title']}\n"
    
    await message.answer(
        text,
        reply_markup=get_favorites_keyboard(favorites),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("fav_add_"))
async def add_to_favorites_callback(callback: types.CallbackQuery):
    """Добавить чат в избранное"""
    user_id = callback.from_user.id
    chat_id = int(callback.data.replace("fav_add_", ""))
    chat_title = callback.message.text.split('\n')[int(chat_id) - 1] if chat_id <= 10 else "Чат"
    
    await add_to_favorites(user_id, chat_id, chat_title)
    
    await callback.answer("✅ Добавлено в избранное", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "📊 Анализ чата")
async def cmd_analyze_chat(message: types.Message, state: FSMContext):
    """Анализ чата"""
    user_id = message.from_user.id
    
    # Проверка подписки
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку."
        )
        return
    
    await message.answer(
        "📊 **Анализ чата**\n\n"
        "Отправьте ссылку или ID чата для анализа:\n"
        "Пример: @username или -100123456789"
    )
    await state.set_state("waiting_chat_for_analysis")

@dp.message(F.text == "📨 Рассылка")
async def cmd_quick_broadcast(message: types.Message, state: FSMContext):
    """Быстрая рассылка по чатам"""
    user_id = message.from_user.id
    
    # Проверка подписки
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку."
        )
        return
    
    await message.answer(
        "📨 **Быстрая рассылка**\n\n"
        "Отправьте сообщение для рассылки по всем чатам:"
    )
    await state.set_state("waiting_broadcast_message")

@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    """Настройки пользователя"""
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await message.answer("Сначала авторизуйтесь через /start")
        return
    
    auto_clean = user.get('auto_clean_spam', 0)
    notify = user.get('notify_expiration', 1)
    
    await message.answer(
        "⚙️ **Настройки**\n\n"
        "Здесь вы можете настроить поведение бота:",
        reply_markup=get_settings_keyboard(auto_clean, notify),
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
    
    await callback.message.edit_reply_markup(
        reply_markup=get_settings_keyboard(new_value, user.get('notify_expiration', 1))
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
    
    await callback.message.edit_reply_markup(
        reply_markup=get_settings_keyboard(user.get('auto_clean_spam', 0), new_value)
    )
    await callback.answer("✅ Настройка сохранена")

@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: types.CallbackQuery):
    """Статистика использования"""
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Общее количество действий
        cursor = await db.execute(
            "SELECT COUNT(*) FROM usage_stats WHERE user_id = ?",
            (user_id,)
        )
        total_actions = (await cursor.fetchone())[0]
        
        # По дням
        cursor = await db.execute('''
            SELECT date(timestamp), COUNT(*) 
            FROM usage_stats 
            WHERE user_id = ? 
            GROUP BY date(timestamp) 
            ORDER BY date(timestamp) DESC 
            LIMIT 7
        ''', (user_id,))
        daily_stats = await cursor.fetchall()
        
        # По типам действий
        cursor = await db.execute('''
            SELECT action, COUNT(*) 
            FROM usage_stats 
            WHERE user_id = ? 
            GROUP BY action 
            ORDER BY COUNT(*) DESC
        ''', (user_id,))
        action_stats = await cursor.fetchall()
    
    text = f"📊 **Ваша статистика**\n\n"
    text += f"Всего действий: {total_actions}\n\n"
    
    text += "**Последние 7 дней:**\n"
    for date, count in daily_stats:
        text += f"• {date}: {count} действий\n"
    
    text += "\n**По типам:**\n"
    for action, count in action_stats[:5]:
        action_name = {
            "spam_check": "Проверка спама",
            "view_chats": "Просмотр чатов",
            "chat_analysis": "Анализ чата",
            "broadcast": "Рассылка"
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

# ================== АВТОРИЗАЦИЯ ==================

@dp.message(AuthStates.waiting_api_id)
async def auth_get_api_id(message: types.Message, state: FSMContext):
    """Получение API ID"""
    try:
        api_id = int(message.text.strip())
        if api_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ API ID должен быть положительным числом. Попробуйте снова:")
        return
    
    await state.update_data(api_id=api_id)
    await state.set_state(AuthStates.waiting_api_hash)
    
    await message.answer(
        "🔑 ШАГ 2 ИЗ 4: Введите API HASH\n\n"
        "API Hash - это строка из 32 символов, которую вы получили на my.telegram.org/apps\n\n"
        "Отправьте API Hash:"
    )

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
    
    # Сохраняем во временную таблицу
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO auth_temp (user_id, api_id, api_hash, code_attempts, password_attempts)
            VALUES (?, ?, ?, 0, 0)
        ''', (user_id, api_id, api_hash))
        await db.commit()
    
    await state.set_state(AuthStates.waiting_phone)
    
    await message.answer(
        "📱 ШАГ 3 ИЗ 4: Введите номер телефона\n\n"
        "Формат: +79001234567\n\n"
        "Отправьте номер:"
    )

@dp.message(AuthStates.waiting_phone)
async def auth_get_phone(message: types.Message, state: FSMContext):
    """Получение номера телефона"""
    phone = message.text.strip()
    
    # Простая валидация
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer("❌ Неверный формат номера. Используйте +79001234567")
        return
    
    user_id = message.from_user.id
    
    # Получаем данные из временной таблицы
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
        
        # Сохраняем номер
        await db.execute(
            "UPDATE auth_temp SET phone_number = ? WHERE user_id = ?",
            (phone, user_id)
        )
        await db.commit()
    
    # Отправляем код
    try:
        # Создаем временный клиент
        client = Client(
            name=f"auth_{user_id}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True
        )
        
        # Отправляем код
        await client.connect()
        sent_code = await client.send_code(phone)
        
        # Сохраняем информацию о сессии
        await state.update_data(
            phone=phone,
            client=client,
            phone_code_hash=sent_code.phone_code_hash
        )
        
        await state.set_state(AuthStates.waiting_code)
        
        await message.answer(
            "✅ Код подтверждения отправлен!\n\n"
            "📱 ШАГ 4 ИЗ 4: Введите код из Telegram\n\n"
            "Если код не приходит в течение минуты, проверьте правильность номера."
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
    
    # Проверяем количество попыток
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
        
        # Увеличиваем счетчик
        await db.execute(
            "UPDATE auth_temp SET code_attempts = ? WHERE user_id = ?",
            (attempts + 1, user_id)
        )
        await db.commit()
    
    try:
        # Пытаемся войти с кодом
        user = await client.sign_in(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=code
        )
        
        # Получаем api данные
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT api_id, api_hash FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            api_id, api_hash = row if row else (None, None)
        
        # Успешный вход
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
        # Требуется 2FA
        await state.set_state(AuthStates.waiting_2fa_password)
        await message.answer(
            "🔐 Требуется пароль двухфакторной аутентификации (2FA)\n\n"
            f"Максимум попыток: {MAX_2FA_ATTEMPTS}\n\n"
            "Введите пароль:"
        )
        
    except PhoneCodeInvalid:
        await message.answer("❌ Неверный код. Попробуйте снова:")
        # Остаемся в том же состоянии
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
    
    # Проверяем количество попыток
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
        
        # Увеличиваем счетчик
        await db.execute(
            "UPDATE auth_temp SET password_attempts = ? WHERE user_id = ?",
            (attempts + 1, user_id)
        )
        await db.commit()
    
    try:
        # Пытаемся войти с паролем
        user = await client.check_password(password)
        
        # Получаем api данные
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT api_id, api_hash FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            api_id, api_hash = row if row else (None, None)
        
        # Успешный вход
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
    
    # Формируем информацию о подписке
    sub_until = datetime.fromisoformat(user['subscription_until'])
    days_left = (sub_until - datetime.now()).days
    
    if days_left > 0:
        status = f"✅ Активна (осталось {days_left} дн.)"
    else:
        status = "❌ Истекла"
    
    profile_text = (
        f"👤 **ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ**\n\n"
        f"**Подключенный аккаунт:** @{user['account_username'] or 'не указан'}\n"
        f"**Имя:** {user['account_first_name']} {user['account_last_name'] or ''}\n"
        f"**Номер:** {user['phone_number']}\n\n"
        f"📅 **Подписка активна до:** {sub_until.strftime('%d.%m.%Y')}\n"
        f"**Статус:** {status}\n\n"
        f"*Нажмите кнопку для управления*"
    )
    
    await message.answer(
        profile_text,
        reply_markup=get_profile_keyboard(),
        parse_mode="Markdown"
    )

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
    
    # Подтверждение
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

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: types.CallbackQuery):
    """Вернуться в профиль"""
    user_id = callback.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await callback.message.edit_text("Пользователь не найден. Нажмите /start")
        await callback.answer()
        return
    
    sub_until = datetime.fromisoformat(user['subscription_until'])
    days_left = (sub_until - datetime.now()).days
    
    if days_left > 0:
        status = f"✅ Активна (осталось {days_left} дн.)"
    else:
        status = "❌ Истекла"
    
    profile_text = (
        f"👤 **ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ**\n\n"
        f"**Подключенный аккаунт:** @{user['account_username'] or 'не указан'}\n"
        f"**Имя:** {user['account_first_name']} {user['account_last_name'] or ''}\n"
        f"**Номер:** {user['phone_number']}\n\n"
        f"📅 **Подписка активна до:** {sub_until.strftime('%d.%m.%Y')}\n"
        f"**Статус:** {status}"
    )
    
    await callback.message.edit_text(
        profile_text,
        reply_markup=get_profile_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    """Вернуться в главное меню"""
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "settings")
async def settings_callback(callback: types.CallbackQuery):
    """Перейти к настройкам"""
    user_id = callback.from_user.id
    user = await get_user(user_id)
    
    if user:
        auto_clean = user.get('auto_clean_spam', 0)
        notify = user.get('notify_expiration', 1)
        
        await callback.message.edit_text(
            "⚙️ **Настройки**\n\n"
            "Здесь вы можете настроить поведение бота:",
            reply_markup=get_settings_keyboard(auto_clean, notify),
            parse_mode="Markdown"
        )
    await callback.answer()

# ================== АДМИН-ПАНЕЛЬ ==================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Админ-панель"""
    await message.answer(
        "👨‍💻 **Панель администратора**\n\n"
        "Доступные команды:\n"
        "• /extend @user дни - продлить подписку\n"
        "• /stats - статистика бота\n"
        "• /users - список пользователей\n"
        "• /broadcast - рассылка всем\n\n"
        "*Админ-панель доступна всем в демо-режиме*",
        reply_markup=get_admin_keyboard(),
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
    
    # Парсим ID или username
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
    
    # Продлеваем подписку
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
    
    logger.info(f"Subscription extended for {target_id} by {message.from_user.id} for {days} days")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Статистика бота"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Всего пользователей
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]
        
        # Активных подписок
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE subscription_until > ?",
            (now,)
        )
        active_subs = (await cursor.fetchone())[0]
        
        # Пользователей за последние 24 часа
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE created_at > ?",
            (yesterday,)
        )
        new_today = (await cursor.fetchone())[0]
        
        # Всего действий
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
            sub_date = datetime.fromisoformat(sub_until).strftime('%d.%m.%Y')
            if datetime.fromisoformat(sub_until) > datetime.now():
                status = "✅"
            else:
                status = "❌"
        else:
            sub_date = "нет"
            status = "❌"
        
        created = datetime.fromisoformat(row['created_at']).strftime('%d.%m')
        
        text += f"`{user_id}` | @{username} | {name} | {status} до {sub_date} | 📅 {created}\n"
    
    # Разбиваем на части если слишком длинное
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await message.answer(part, parse_mode="Markdown")
    else:
        await message.answer(text, parse_mode="Markdown")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    """Рассылка сообщения всем пользователям"""
    await message.answer(
        "📨 **Режим рассылки**\n\n"
        "Отправьте сообщение, которое хотите разослать всем пользователям бота.\n\n"
        "Поддерживаются: текст, фото, видео, документы.",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_message)

@dp.message(BroadcastStates.waiting_message)
async def broadcast_get_message(message: types.Message, state: FSMContext):
    """Получить сообщение для рассылки"""
    # Сохраняем сообщение в состоянии
    await state.update_data(
        broadcast_content=message.html_text,
        broadcast_caption=message.caption,
        broadcast_photo=message.photo[-1].file_id if message.photo else None,
        broadcast_video=message.video.file_id if message.video else None,
        broadcast_document=message.document.file_id if message.document else None
    )
    
    # Показываем предпросмотр
    preview_text = "📨 **Предпросмотр рассылки:**\n\n"
    preview_text += message.html_text or message.caption or "(пустое сообщение)"
    
    await message.answer(
        preview_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast_send")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")]
            ]
        )
    )

@dp.callback_query(F.data == "broadcast_send")
async def broadcast_send(callback: types.CallbackQuery, state: FSMContext):
    """Отправить рассылку"""
    data = await state.get_data()
    
    # Получаем всех пользователей
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        users = await cursor.fetchall()
    
    await callback.message.edit_text(f"📨 Начинаю рассылку {len(users)} пользователям...")
    
    sent = 0
    failed = 0
    
    for (user_id,) in users:
        try:
            if data.get('broadcast_photo'):
                await bot.send_photo(
                    user_id,
                    data['broadcast_photo'],
                    caption=data.get('broadcast_caption'),
                    parse_mode="HTML"
                )
            elif data.get('broadcast_video'):
                await bot.send_video(
                    user_id,
                    data['broadcast_video'],
                    caption=data.get('broadcast_caption'),
                    parse_mode="HTML"
                )
            elif data.get('broadcast_document'):
                await bot.send_document(
                    user_id,
                    data['broadcast_document'],
                    caption=data.get('broadcast_caption'),
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    user_id,
                    data.get('broadcast_content', ''),
                    parse_mode="HTML"
                )
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast failed for {user_id}: {e}")
        
        # Небольшая задержка чтобы не флудить
        await asyncio.sleep(0.05)
    
    await callback.message.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )
    
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    """Отменить рассылку"""
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: types.CallbackQuery, state: FSMContext):
    """Обработка админ-кнопок"""
    action = callback.data.replace("admin_", "")
    
    if action == "stats":
        await cmd_stats(callback.message)
    elif action == "users":
        await cmd_users(callback.message)
    elif action == "extend":
        await callback.message.answer(
            "Используйте команду:\n`/extend @username дни`\nили\n`/extend user_id дни`",
            parse_mode="Markdown"
        )
    elif action == "broadcast":
        await callback.message.answer(
            "📨 Используйте команду /broadcast для начала рассылки"
        )
    elif action == "activity":
        # Статистика активности
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('''
                SELECT action, COUNT(*) as count 
                FROM usage_stats 
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY action 
                ORDER BY count DESC
            ''')
            stats = await cursor.fetchall()
        
        text = "📈 **Активность за 7 дней:**\n\n"
        for action, count in stats:
            text += f"• {action}: {count}\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]
            ),
            parse_mode="Markdown"
        )
    elif action == "back":
        await callback.message.edit_text(
            "👨‍💻 **Панель администратора**",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )
    
    await callback.answer()

# ================== ОБЩИЕ ОБРАБОТЧИКИ ==================

@dp.message()
async def handle_unknown(message: types.Message):
    """Обработка неизвестных команд"""
    if message.text and message.text.startswith('/'):
        await message.answer(
            "❌ Неизвестная команда.\n"
            "Доступные команды: /start, /spam, /chats, /profile, /admin"
        )
    else:
        # Игнорируем обычные сообщения не в состоянии авторизации
        pass

# ================== ЗАПУСК БОТА ==================

async def on_startup():
    """Действия при запуске"""
    await init_db()
    logger.info("Bot started!")
    logger.info(f"Bot token: {BOT_TOKEN[:10]}...")

async def on_shutdown():
    """Действия при остановке"""
    logger.info("Bot stopped!")

async def main():
    """Главная функция"""
    # Регистрируем обработчики запуска/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Запускаем бота
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
