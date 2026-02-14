"""
Telegram Bot для управления аккаунтами через Pyrogram
Полностью рабочая версия с множеством функций
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
SPAM_BOT_TIMEOUT = 15  # Увеличил таймаут
CHATS_PER_PAGE = 15  # Увеличил количество
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
        
        # Таблица для автоответов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS auto_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                trigger_text TEXT,
                response_text TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для запланированных сообщений
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                message_text TEXT,
                send_time TIMESTAMP,
                is_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для контактов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS contacts (
                user_id INTEGER,
                contact_id INTEGER,
                contact_name TEXT,
                contact_username TEXT,
                contact_phone TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, contact_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для заметок
        await db.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для мониторинга чатов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS monitored_chats (
                user_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                keywords TEXT,
                last_check TIMESTAMP,
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

class BroadcastStates(StatesGroup):
    waiting_message = State()
    waiting_confirm = State()

class AutoResponseStates(StatesGroup):
    waiting_trigger = State()
    waiting_response = State()

class ScheduleStates(StatesGroup):
    waiting_chat = State()
    waiting_message = State()
    waiting_time = State()

class ContactStates(StatesGroup):
    waiting_contact = State()
    waiting_notes = State()

class NoteStates(StatesGroup):
    waiting_title = State()
    waiting_content = State()

class MonitorStates(StatesGroup):
    waiting_chat = State()
    waiting_keywords = State()

class ForwardStates(StatesGroup):
    waiting_from = State()
    waiting_to = State()
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
        
        # Очищаем временные данные
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
        await db.execute("DELETE FROM auto_responses WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM scheduled_messages WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM contacts WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM monitored_chats WHERE user_id = ?", (user_id,))
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
        
        # Проверяем, что клиент работает
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

# ================== ОСНОВНЫЕ ФУНКЦИИ ==================

async def check_spam_status(user_id: int) -> Tuple[bool, str, Optional[str]]:
    """
    Проверка спам-блока через @spambot
    Возвращает: (успех, статус, дата разблокировки)
    """
    client = None
    try:
        client = await get_pyro_client(user_id)
        if not client:
            return False, "Ошибка подключения к аккаунту", None
        
        # Получаем информацию о пользователе
        me = await client.get_me()
        
        # Ищем @spambot
        try:
            spambot = await client.get_users(SPAM_BOT_USERNAME)
        except UsernameNotOccupied:
            return False, "Бот @spambot не найден", None
        
        # Отправляем команду /start
        await client.send_message(spambot.id, "/start")
        
        # Ждем ответ
        await asyncio.sleep(3)
        
        # Получаем последние сообщения
        messages = []
        async for msg in client.get_chat_history(spambot.id, limit=10):
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
        
        # Проверка на русском
        if re.search(r'(ограничены|заблокированы|имеются ограничения)', text.lower()):
            is_restricted = True
        elif re.search(r'(добро пожаловать|вы не ограничены|нет ограничений)', text.lower()):
            is_restricted = False
        
        # Проверка на английском
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

async def get_chat_full_info(client: Client, chat_id: int) -> Optional[Dict]:
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
            'linked_chat_id': getattr(chat, 'linked_chat_id', None),
            'slow_mode': getattr(chat, 'slow_mode_delay', 0),
            'restrictions': getattr(chat, 'restrictions', [])
        }
        
        if chat.type == ChatType.PRIVATE:
            info['first_name'] = chat.first_name
            info['last_name'] = chat.last_name
            info['is_bot'] = chat.is_bot
            info['phone_number'] = getattr(chat, 'phone_number', None)
            info['dc_id'] = getattr(chat, 'dc_id', None)
        
        # Получаем фото
        if chat.photo:
            info['photo'] = True
        
        # Получаем права
        if hasattr(chat, 'permissions'):
            info['permissions'] = {
                'can_send_messages': chat.permissions.can_send_messages,
                'can_send_media': chat.permissions.can_send_media_messages,
                'can_send_polls': chat.permissions.can_send_polls,
                'can_add_web_page_previews': chat.permissions.can_add_web_page_previews
            }
        
        return info
    except Exception as e:
        logger.error(f"Error getting chat info: {e}")
        return None

# ================== КЛАВИАТУРЫ ==================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    builder = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚫 Проверить спам-блок")],
            [KeyboardButton(text="💬 Мои чаты"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="📊 Инфо о чате"), KeyboardButton(text="👥 Контакты")],
            [KeyboardButton(text="📝 Заметки"), KeyboardButton(text="🤖 Автоответы")],
            [KeyboardButton(text="📅 Планировщик"), KeyboardButton(text="🔄 Переслать")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )
    return builder

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
    auto_respond = user.get('auto_respond', 0)
    
    clean_status = "✅ Вкл" if auto_clean else "❌ Выкл"
    notify_status = "✅ Вкл" if notify else "❌ Выкл"
    respond_status = "✅ Вкл" if auto_respond else "❌ Выкл"
    
    builder.row(InlineKeyboardButton(text=f"🧹 Автоочистка спам-бота: {clean_status}", callback_data="toggle_auto_clean"))
    builder.row(InlineKeyboardButton(text=f"🔔 Уведомления: {notify_status}", callback_data="toggle_notify"))
    builder.row(InlineKeyboardButton(text=f"🤖 Автоответы: {respond_status}", callback_data="toggle_auto_respond"))
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

def get_chats_actions_keyboard(chat_id: int, chat_title: str) -> InlineKeyboardMarkup:
    """Клавиатура действий с чатом"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add_{chat_id}|{chat_title}"),
        InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"chat_info_{chat_id}")
    )
    builder.row(
        InlineKeyboardButton(text="📨 Написать", callback_data=f"send_msg_{chat_id}"),
        InlineKeyboardButton(text="📅 Отложить", callback_data=f"schedule_{chat_id}")
    )
    builder.row(
        InlineKeyboardButton(text="👥 Контакты", callback_data=f"chat_contacts_{chat_id}"),
        InlineKeyboardButton(text="🔄 Переслать", callback_data=f"forward_from_{chat_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_chats"))
    return builder.as_markup()

def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Админ-клавиатура"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="➕ Продлить подписку", callback_data="admin_extend"))
    builder.row(InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_users"))
    builder.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast"))
    builder.row(InlineKeyboardButton(text="📈 Активность", callback_data="admin_activity"))
    builder.row(InlineKeyboardButton(text="🗑 Очистить БД", callback_data="admin_cleanup"))
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
            "🔑 **ШАГ 1 ИЗ 4:** Введите API ID\n\n"
            "API ID - это целое число, которое можно получить на https://my.telegram.org/apps\n\n"
            "Отправьте API ID:",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )

# ================== ПРОВЕРКА СПАМ-БЛОКА ==================

@dp.message(Command("spam"))
@dp.message(F.text == "🚫 Проверить спам-блок")
async def cmd_spam(message: types.Message):
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
async def cmd_chats(message: types.Message):
    """Просмотр всех чатов"""
    user_id = message.from_user.id
    
    # Проверка подписки
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
        # Получаем ВСЕ диалоги
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
        
        # Сортируем: закрепленные сверху, потом по дате
        dialogs.sort(key=lambda x: (-x.get('pinned', 0), x.get('last_message_date') or datetime.min), reverse=True)
        
        # Сохраняем в сессию
        await state.update_data(all_dialogs=dialogs, current_page=0)
        
        # Показываем первую страницу
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
        await edit_msg.edit_text("Чатов больше нет")
        return
    
    text = f"💬 **Мои чаты** (страница {page + 1}/{(len(dialogs) + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE}):\n\n"
    
    for i, dialog in enumerate(page_dialogs, start + 1):
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
        if len(name) > 30:
            name = name[:27] + "..."
        
        # Дополнительная информация
        info = []
        if dialog.get('pinned'):
            info.append("📌")
        if dialog.get('unread_count', 0) > 0:
            info.append(f"💬{dialog['unread_count']}")
        if dialog.get('members_count', 0) > 0:
            info.append(f"👥{dialog['members_count']}")
        
        info_str = f" [{', '.join(info)}]" if info else ""
        
        text += f"{i}. {icon} {name}{info_str}\n"
    
    # Создаем клавиатуру пагинации
    builder = InlineKeyboardBuilder()
    
    if page > 0:
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"chats_page_{page-1}"))
    if end < len(dialogs):
        builder.row(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"chats_page_{page+1}"))
    
    # Кнопки действий
    builder.row(
        InlineKeyboardButton(text="🔍 Выбрать чат", callback_data="select_chat"),
        InlineKeyboardButton(text="⭐ Избранное", callback_data="show_favorites")
    )
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
        "Отправьте номер чата из списка или его название:",
        parse_mode="Markdown"
    )
    await state.set_state("waiting_chat_selection")
    await callback.answer()

@dp.message(F.text, lambda message: message.state == "waiting_chat_selection")
async def process_chat_selection(message: types.Message, state: FSMContext):
    """Обработка выбора чата"""
    try:
        # Пробуем получить номер
        if message.text.isdigit():
            chat_num = int(message.text) - 1
            data = await state.get_data()
            dialogs = data.get('all_dialogs', [])
            
            if 0 <= chat_num < len(dialogs):
                chat = dialogs[chat_num]
                await show_chat_actions(message, chat)
                await state.clear()
                return
        
        # Если не номер, ищем по названию
        data = await state.get_data()
        dialogs = data.get('all_dialogs', [])
        
        for chat in dialogs:
            if message.text.lower() in chat['title'].lower():
                await show_chat_actions(message, chat)
                await state.clear()
                return
        
        await message.answer("❌ Чат не найден. Попробуйте снова или отправьте /cancel")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

async def show_chat_actions(message: types.Message, chat: Dict):
    """Показать действия для чата"""
    text = f"**Чат:** {chat['title']}\n"
    text += f"**Тип:** {str(chat['type']).split('.')[-1]}\n"
    if chat.get('username'):
        text += f"**Username:** @{chat['username']}\n"
    if chat.get('members_count'):
        text += f"**Участников:** {chat['members_count']}\n"
    
    await message.answer(
        text,
        reply_markup=get_chats_actions_keyboard(chat['id'], chat['title']),
        parse_mode="Markdown"
    )

# ================== ИНФОРМАЦИЯ О ЧАТЕ ==================

@dp.message(F.text == "📊 Инфо о чате")
async def cmd_chat_info(message: types.Message, state: FSMContext):
    """Получить информацию о чате"""
    await message.answer(
        "📊 **Информация о чате**\n\n"
        "Отправьте ссылку, ID или username чата:\n"
        "Пример: @username или -100123456789"
    )
    await state.set_state("waiting_chat_for_info")

@dp.message(lambda message: message.state == "waiting_chat_for_info")
async def process_chat_info(message: types.Message, state: FSMContext):
    """Обработка запроса информации о чате"""
    user_id = message.from_user.id
    
    # Проверка подписки
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
        # Парсим ввод
        chat_input = message.text.strip()
        chat_id = None
        
        if chat_input.startswith('@'):
            # Это username
            chat = await client.get_chat(chat_input)
            chat_id = chat.id
        elif chat_input.lstrip('-').isdigit():
            # Это ID
            chat_id = int(chat_input)
            chat = await client.get_chat(chat_id)
        else:
            await status_msg.edit_text("❌ Неверный формат. Используйте @username или ID")
            await client.stop()
            await state.clear()
            return
        
        # Получаем полную информацию
        info = await get_chat_full_info(client, chat_id)
        
        if not info:
            await status_msg.edit_text("❌ Не удалось получить информацию о чате")
            await client.stop()
            await state.clear()
            return
        
        # Формируем текст
        text = f"📊 **Информация о чате**\n\n"
        text += f"**Название:** {info['title']}\n"
        text += f"**Тип:** {str(info['type']).split('.')[-1]}\n"
        
        if info.get('username'):
            text += f"**Username:** @{info['username']}\n"
        
        if info.get('description'):
            desc = info['description'][:100] + "..." if len(info['description']) > 100 else info['description']
            text += f"**Описание:** {desc}\n"
        
        if info.get('members_count'):
            text += f"**Участников:** {info['members_count']}\n"
        
        if info.get('slow_mode'):
            text += f"**Медленный режим:** {info['slow_mode']}с\n"
        
        if info.get('first_name'):
            text += f"**Имя:** {info['first_name']} {info.get('last_name', '')}\n"
        
        if info.get('is_bot'):
            text += f"**Это бот:** Да\n"
        
        if info.get('phone_number'):
            text += f"**Телефон:** {info['phone_number']}\n"
        
        if info.get('dc_id'):
            text += f"**DC ID:** {info['dc_id']}\n"
        
        if info.get('permissions'):
            text += f"\n**Права:**\n"
            perms = info['permissions']
            text += f"• Отправка сообщений: {'✅' if perms.get('can_send_messages') else '❌'}\n"
            text += f"• Отправка медиа: {'✅' if perms.get('can_send_media') else '❌'}\n"
        
        text += f"\n**ID:** `{info['id']}`"
        
        await status_msg.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add_{info['id']}|{info['title']}")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
                ]
            ),
            parse_mode="Markdown"
        )
        
        await log_usage(user_id, "chat_info", str(chat_id))
        
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
        # Если нет названия, пробуем получить
        chat_id = int(data)
        chat_title = f"Чат {chat_id}"
        
        # Пробуем получить название через клиент
        client = await get_pyro_client(user_id)
        if client:
            try:
                chat = await client.get_chat(chat_id)
                chat_title = chat.title or chat.first_name or f"Чат {chat_id}"
                await client.stop()
            except:
                pass
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO favorite_chats (user_id, chat_id, chat_title)
            VALUES (?, ?, ?)
        ''', (user_id, chat_id, chat_title))
        await db.commit()
    
    await callback.answer("✅ Добавлено в избранное")
    
    # Обновляем клавиатуру если есть
    if callback.message.reply_markup:
        await callback.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("fav_open_"))
async def open_favorite(callback: types.CallbackQuery):
    """Открыть избранный чат"""
    chat_id = int(callback.data.replace("fav_open_", ""))
    await callback.message.answer(
        f"Выберите действие для чата:",
        reply_markup=get_chats_actions_keyboard(chat_id, "Избранный чат")
    )
    await callback.answer()

# ================== КОНТАКТЫ ==================

@dp.message(F.text == "👥 Контакты")
async def cmd_contacts(message: types.Message, state: FSMContext):
    """Управление контактами"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Список", callback_data="contacts_list"),
        InlineKeyboardButton(text="➕ Добавить", callback_data="contacts_add")
    )
    builder.row(
        InlineKeyboardButton(text="🔍 Поиск", callback_data="contacts_search"),
        InlineKeyboardButton(text="📤 Экспорт", callback_data="contacts_export")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await message.answer(
        "👥 **Управление контактами**",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "contacts_list")
async def contacts_list(callback: types.CallbackQuery):
    """Список контактов"""
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM contacts WHERE user_id = ? ORDER BY contact_name LIMIT 20",
            (user_id,)
        )
        contacts = await cursor.fetchall()
    
    if not contacts:
        await callback.message.edit_text(
            "📭 У вас пока нет сохраненных контактов.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="contacts_back")]]
            )
        )
        await callback.answer()
        return
    
    text = "👥 **Ваши контакты:**\n\n"
    
    for i, contact in enumerate(contacts, 1):
        text += f"{i}. **{contact['contact_name']}**"
        if contact['contact_username']:
            text += f" (@{contact['contact_username']})"
        text += f"\n   ID: `{contact['contact_id']}`"
        if contact['notes']:
            text += f"\n   📝 {contact['notes'][:50]}"
        text += "\n\n"
    
    if len(text) > 4000:
        text = text[:4000] + "..."
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="contacts_back")]]
        ),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "contacts_add")
async def contacts_add(callback: types.CallbackQuery, state: FSMContext):
    """Добавить контакт"""
    await callback.message.edit_text(
        "➕ **Добавление контакта**\n\n"
        "Отправьте username, ID или номер телефона контакта:"
    )
    await state.set_state(ContactStates.waiting_contact)
    await callback.answer()

@dp.message(ContactStates.waiting_contact)
async def process_contact_input(message: types.Message, state: FSMContext):
    """Обработка ввода контакта"""
    user_id = message.from_user.id
    contact_input = message.text.strip()
    
    client = await get_pyro_client(user_id)
    if not client:
        await message.answer("❌ Ошибка подключения к аккаунту")
        await state.clear()
        return
    
    try:
        # Пытаемся найти пользователя
        if contact_input.startswith('@'):
            user = await client.get_users(contact_input)
        elif contact_input.replace('+', '').isdigit():
            # По номеру телефона
            user = await client.get_users(contact_input)
        elif contact_input.isdigit():
            # По ID
            user = await client.get_users(int(contact_input))
        else:
            # По username без @
            user = await client.get_users(f"@{contact_input}")
        
        await state.update_data(
            contact_id=user.id,
            contact_name=user.first_name or "Unknown",
            contact_username=user.username,
            contact_phone=getattr(user, 'phone_number', None)
        )
        
        text = f"✅ Найден пользователь:\n\n"
        text += f"**Имя:** {user.first_name} {user.last_name or ''}\n"
        if user.username:
            text += f"**Username:** @{user.username}\n"
        if hasattr(user, 'phone_number') and user.phone_number:
            text += f"**Телефон:** {user.phone_number}\n"
        text += f"**ID:** `{user.id}`\n\n"
        text += "Введите заметку для контакта (или отправьте /skip):"
        
        await message.answer(text, parse_mode="Markdown")
        await state.set_state(ContactStates.waiting_notes)
        
    except Exception as e:
        await message.answer(f"❌ Пользователь не найден: {str(e)[:100]}")
        await state.clear()
    finally:
        await client.stop()

@dp.message(ContactStates.waiting_notes)
async def process_contact_notes(message: types.Message, state: FSMContext):
    """Обработка заметок для контакта"""
    if message.text == "/skip":
        notes = ""
    else:
        notes = message.text
    
    data = await state.get_data()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO contacts 
            (user_id, contact_id, contact_name, contact_username, contact_phone, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            data['contact_id'],
            data['contact_name'],
            data.get('contact_username'),
            data.get('contact_phone'),
            notes
        ))
        await db.commit()
    
    await message.answer("✅ Контакт успешно сохранен!")
    await state.clear()

# ================== ЗАМЕТКИ ==================

@dp.message(F.text == "📝 Заметки")
async def cmd_notes(message: types.Message, state: FSMContext):
    """Управление заметками"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Список", callback_data="notes_list"),
        InlineKeyboardButton(text="➕ Создать", callback_data="notes_create")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await message.answer(
        "📝 **Мои заметки**",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "notes_create")
async def notes_create(callback: types.CallbackQuery, state: FSMContext):
    """Создать заметку"""
    await callback.message.edit_text(
        "📝 **Создание заметки**\n\n"
        "Введите название заметки:"
    )
    await state.set_state(NoteStates.waiting_title)
    await callback.answer()

@dp.message(NoteStates.waiting_title)
async def process_note_title(message: types.Message, state: FSMContext):
    """Обработка названия заметки"""
    await state.update_data(note_title=message.text)
    await message.answer(
        "📝 Введите содержание заметки (можно использовать Markdown):"
    )
    await state.set_state(NoteStates.waiting_content)

@dp.message(NoteStates.waiting_content)
async def process_note_content(message: types.Message, state: FSMContext):
    """Обработка содержания заметки"""
    data = await state.get_data()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute('''
            INSERT INTO notes (user_id, title, content)
            VALUES (?, ?, ?)
        ''', (user_id, data['note_title'], message.html_text))
        note_id = cursor.lastrowid
        await db.commit()
    
    await message.answer(
        f"✅ Заметка создана!\n\n"
        f"**ID:** {note_id}\n"
        f"**Название:** {data['note_title']}",
        parse_mode="Markdown"
    )
    await state.clear()

@dp.callback_query(F.data == "notes_list")
async def notes_list(callback: types.CallbackQuery):
    """Список заметок"""
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (user_id,)
        )
        notes = await cursor.fetchall()
    
    if not notes:
        await callback.message.edit_text(
            "📭 У вас пока нет заметок.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="notes_back")]]
            )
        )
        await callback.answer()
        return
    
    builder = InlineKeyboardBuilder()
    
    for note in notes:
        title = note['title'][:30] + "..." if len(note['title']) > 30 else note['title']
        builder.row(InlineKeyboardButton(
            text=f"📝 {title}",
            callback_data=f"note_view_{note['id']}"
        ))
    
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="notes_back"))
    
    await callback.message.edit_text(
        "📝 **Список заметок:**",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("note_view_"))
async def note_view(callback: types.CallbackQuery):
    """Просмотр заметки"""
    note_id = int(callback.data.replace("note_view_", ""))
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM notes WHERE id = ? AND user_id = ?",
            (note_id, user_id)
        )
        note = await cursor.fetchone()
    
    if not note:
        await callback.answer("❌ Заметка не найдена")
        return
    
    text = f"**{note['title']}**\n\n"
    text += note['content']
    text += f"\n\n📅 {datetime.fromisoformat(note['created_at']).strftime('%d.%m.%Y %H:%M')}"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Удалить", callback_data=f"note_delete_{note_id}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="notes_list")]
            ]
        ),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("note_delete_"))
async def note_delete(callback: types.CallbackQuery):
    """Удаление заметки"""
    note_id = int(callback.data.replace("note_delete_", ""))
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?",
            (note_id, user_id)
        )
        await db.commit()
    
    await callback.answer("✅ Заметка удалена")
    await notes_list(callback)

# ================== АВТООТВЕТЫ ==================

@dp.message(F.text == "🤖 Автоответы")
async def cmd_auto_responses(message: types.Message, state: FSMContext):
    """Управление автоответами"""
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user or not user.get('auto_respond'):
        await message.answer(
            "⚠️ Функция автоответов отключена.\n"
            "Включите её в настройках: ⚙️ Настройки"
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Список", callback_data="responses_list"),
        InlineKeyboardButton(text="➕ Создать", callback_data="response_create")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await message.answer(
        "🤖 **Управление автоответами**",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "response_create")
async def response_create(callback: types.CallbackQuery, state: FSMContext):
    """Создать автоответ"""
    await callback.message.edit_text(
        "🤖 **Создание автоответа**\n\n"
        "Введите ключевое слово или фразу (триггер):"
    )
    await state.set_state(AutoResponseStates.waiting_trigger)
    await callback.answer()

@dp.message(AutoResponseStates.waiting_trigger)
async def process_response_trigger(message: types.Message, state: FSMContext):
    """Обработка триггера автоответа"""
    await state.update_data(trigger=message.text.lower())
    await message.answer(
        "📝 Введите текст ответа (можно использовать Markdown):"
    )
    await state.set_state(AutoResponseStates.waiting_response)

@dp.message(AutoResponseStates.waiting_response)
async def process_response_text(message: types.Message, state: FSMContext):
    """Обработка текста ответа"""
    data = await state.get_data()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT INTO auto_responses (user_id, trigger_text, response_text)
            VALUES (?, ?, ?)
        ''', (user_id, data['trigger'], message.html_text))
        await db.commit()
    
    await message.answer(
        f"✅ Автоответ создан!\n\n"
        f"**Триггер:** {data['trigger']}\n"
        f"**Ответ:** {message.text[:100]}..."
    )
    await state.clear()

@dp.callback_query(F.data == "responses_list")
async def responses_list(callback: types.CallbackQuery):
    """Список автоответов"""
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM auto_responses WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC",
            (user_id,)
        )
        responses = await cursor.fetchall()
    
    if not responses:
        await callback.message.edit_text(
            "📭 У вас пока нет автоответов.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="responses_back")]]
            )
        )
        await callback.answer()
        return
    
    text = "🤖 **Ваши автоответы:**\n\n"
    
    for i, resp in enumerate(responses, 1):
        text += f"{i}. **Триггер:** {resp['trigger_text']}\n"
        text += f"   **Ответ:** {resp['response_text'][:50]}...\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="responses_back")]]
        ),
        parse_mode="Markdown"
    )
    await callback.answer()

# ================== ПЛАНИРОВЩИК ==================

@dp.message(F.text == "📅 Планировщик")
async def cmd_scheduler(message: types.Message, state: FSMContext):
    """Планировщик сообщений"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Запланированные", callback_data="scheduled_list"),
        InlineKeyboardButton(text="➕ Создать", callback_data="schedule_create")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await message.answer(
        "📅 **Планировщик сообщений**",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "schedule_create")
async def schedule_create(callback: types.CallbackQuery, state: FSMContext):
    """Создать запланированное сообщение"""
    await callback.message.edit_text(
        "📅 **Создание запланированного сообщения**\n\n"
        "Отправьте ID или username чата:"
    )
    await state.set_state(ScheduleStates.waiting_chat)
    await callback.answer()

@dp.message(ScheduleStates.waiting_chat)
async def schedule_get_chat(message: types.Message, state: FSMContext):
    """Получение чата для планировщика"""
    await state.update_data(chat_input=message.text.strip())
    await message.answer(
        "📝 Отправьте текст сообщения:"
    )
    await state.set_state(ScheduleStates.waiting_message)

@dp.message(ScheduleStates.waiting_message)
async def schedule_get_message(message: types.Message, state: FSMContext):
    """Получение сообщения для планировщика"""
    await state.update_data(message_text=message.html_text)
    await message.answer(
        "⏰ Отправьте время отправки в формате:\n"
        "`ДД.ММ.ГГГГ ЧЧ:ММ`\n\n"
        "Пример: `25.12.2024 15:30`"
    )
    await state.set_state(ScheduleStates.waiting_time)

@dp.message(ScheduleStates.waiting_time)
async def schedule_get_time(message: types.Message, state: FSMContext):
    """Получение времени для планировщика"""
    try:
        send_time = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        
        if send_time <= datetime.now():
            await message.answer("❌ Время должно быть в будущем")
            return
        
        data = await state.get_data()
        user_id = message.from_user.id
        
        # Получаем информацию о чате
        client = await get_pyro_client(user_id)
        chat_title = "Unknown"
        
        if client:
            try:
                chat_input = data['chat_input']
                if chat_input.startswith('@'):
                    chat = await client.get_users(chat_input)
                else:
                    chat = await client.get_chat(int(chat_input))
                chat_title = chat.title or chat.first_name or "Unknown"
                await client.stop()
            except:
                pass
        
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                INSERT INTO scheduled_messages 
                (user_id, chat_id, chat_title, message_text, send_time)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                user_id,
                data['chat_input'],
                chat_title,
                data['message_text'],
                send_time.isoformat()
            ))
            await db.commit()
        
        await message.answer(
            f"✅ Сообщение запланировано на {send_time.strftime('%d.%m.%Y %H:%M')}"
        )
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()

@dp.callback_query(F.data == "scheduled_list")
async def scheduled_list(callback: types.CallbackQuery):
    """Список запланированных сообщений"""
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM scheduled_messages 
            WHERE user_id = ? AND is_sent = 0 
            ORDER BY send_time
        ''', (user_id,))
        scheduled = await cursor.fetchall()
    
    if not scheduled:
        await callback.message.edit_text(
            "📭 Нет запланированных сообщений.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="scheduler_back")]]
            )
        )
        await callback.answer()
        return
    
    text = "📅 **Запланированные сообщения:**\n\n"
    
    for i, msg in enumerate(scheduled, 1):
        send_time = datetime.fromisoformat(msg['send_time'])
        time_str = send_time.strftime('%d.%m.%Y %H:%M')
        text += f"{i}. **Чат:** {msg['chat_title']}\n"
        text += f"   **Время:** {time_str}\n"
        text += f"   **Текст:** {msg['message_text'][:50]}...\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="scheduler_back")]]
        ),
        parse_mode="Markdown"
    )
    await callback.answer()

# ================== ПЕРЕСЫЛКА ==================

@dp.message(F.text == "🔄 Переслать")
async def cmd_forward(message: types.Message, state: FSMContext):
    """Пересылка сообщений между чатами"""
    await message.answer(
        "🔄 **Пересылка сообщений**\n\n"
        "Отправьте ID или username чата-источника:"
    )
    await state.set_state(ForwardStates.waiting_from)

@dp.message(ForwardStates.waiting_from)
async def forward_get_from(message: types.Message, state: FSMContext):
    """Получение чата-источника"""
    await state.update_data(from_chat=message.text.strip())
    await message.answer(
        "📤 Отправьте ID или username чата-получателя:"
    )
    await state.set_state(ForwardStates.waiting_to)

@dp.message(ForwardStates.waiting_to)
async def forward_get_to(message: types.Message, state: FSMContext):
    """Получение чата-получателя"""
    await state.update_data(to_chat=message.text.strip())
    await message.answer(
        "📨 Отправьте сообщение для пересылки:"
    )
    await state.set_state(ForwardStates.waiting_confirm)

@dp.message(ForwardStates.waiting_confirm)
async def forward_execute(message: types.Message, state: FSMContext):
    """Выполнение пересылки"""
    user_id = message.from_user.id
    data = await state.get_data()
    
    status_msg = await message.answer("🔄 Выполняю пересылку...")
    
    client = await get_pyro_client(user_id)
    if not client:
        await status_msg.edit_text("❌ Ошибка подключения к аккаунту")
        await state.clear()
        return
    
    try:
        # Получаем чаты
        from_input = data['from_chat']
        to_input = data['to_chat']
        
        # Парсим источники
        if from_input.startswith('@'):
            from_chat = await client.get_chat(from_input)
        else:
            from_chat = await client.get_chat(int(from_input))
        
        if to_input.startswith('@'):
            to_chat = await client.get_chat(to_input)
        else:
            to_chat = await client.get_chat(int(to_input))
        
        # Пересылаем сообщение
        await client.send_message(
            to_chat.id,
            f"**Переслано из {from_chat.title or from_chat.first_name}:**\n\n{message.html_text}",
            parse_mode="HTML"
        )
        
        await status_msg.edit_text(
            f"✅ Сообщение переслано!\n\n"
            f"**Из:** {from_chat.title or from_chat.first_name}\n"
            f"**В:** {to_chat.title or to_chat.first_name}"
        )
        
        await log_usage(user_id, "forward", f"{from_input} -> {to_input}")
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    finally:
        await client.stop()
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
    
    # Получаем статистику
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
        # Общее количество
        cursor = await db.execute(
            "SELECT COUNT(*) FROM usage_stats WHERE user_id = ?",
            (user_id,)
        )
        total = (await cursor.fetchone())[0]
        
        # По дням
        cursor = await db.execute('''
            SELECT date(timestamp), COUNT(*) 
            FROM usage_stats 
            WHERE user_id = ? 
            GROUP BY date(timestamp) 
            ORDER BY date(timestamp) DESC 
            LIMIT 7
        ''', (user_id,))
        daily = await cursor.fetchall()
        
        # По типам
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
            "forward": "Пересылка"
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
        f"**Номер:** {user['phone_number']}\n"
        f"**ID аккаунта:** `{user.get('account_id', 'неизвестно')}`\n\n"
        f"📅 **Подписка активна до:** {sub_until.strftime('%d.%m.%Y')}\n"
        f"**Статус:** {status}"
    )
    
    await callback.message.edit_text(
        profile_text,
        reply_markup=get_profile_keyboard(),
        parse_mode="Markdown"
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

@dp.callback_query(F.data == "toggle_auto_respond")
async def toggle_auto_respond(callback: types.CallbackQuery):
    """Переключить автоответы"""
    user_id = callback.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await callback.answer("Ошибка")
        return
    
    new_value = 0 if user.get('auto_respond', 0) else 1
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET auto_respond = ? WHERE user_id = ?",
            (new_value, user_id)
        )
        await db.commit()
    
    user['auto_respond'] = new_value
    await callback.message.edit_reply_markup(
        reply_markup=get_settings_keyboard(user)
    )
    await callback.answer("✅ Настройка сохранена")

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

@dp.callback_query(F.data == "back_to_chats")
async def back_to_chats(callback: types.CallbackQuery, state: FSMContext):
    """Вернуться к списку чатов"""
    data = await state.get_data()
    dialogs = data.get('all_dialogs', [])
    
    if dialogs:
        await show_chats_page(callback.message, dialogs, 0, callback.message)
    else:
        await cmd_chats(callback.message)
    
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
        
        # Активных сегодня
        today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM usage_stats WHERE timestamp > ?",
            (today_start,)
        )
        active_today = (await cursor.fetchone())[0]
    
    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Активных подписок: {active_subs}\n"
        f"📈 Новых за 24ч: {new_today}\n"
        f"🔄 Всего действий: {total_actions}\n"
        f"📊 Активных сегодня: {active_today}"
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
    content = {
        'text': message.html_text if message.text else None,
        'caption': message.caption if message.caption else None,
        'photo': message.photo[-1].file_id if message.photo else None,
        'video': message.video.file_id if message.video else None,
        'document': message.document.file_id if message.document else None,
        'animation': message.animation.file_id if message.animation else None,
        'voice': message.voice.file_id if message.voice else None,
        'audio': message.audio.file_id if message.audio else None,
        'sticker': message.sticker.file_id if message.sticker else None
    }
    
    await state.update_data(broadcast_content=content)
    
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
    content = data.get('broadcast_content', {})
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        users = await cursor.fetchall()
    
    await callback.message.edit_text(f"📨 Начинаю рассылку {len(users)} пользователям...")
    
    sent = 0
    failed = 0
    
    for (user_id,) in users:
        try:
            if content.get('photo'):
                await bot.send_photo(
                    user_id,
                    content['photo'],
                    caption=content.get('caption'),
                    parse_mode="HTML"
                )
            elif content.get('video'):
                await bot.send_video(
                    user_id,
                    content['video'],
                    caption=content.get('caption'),
                    parse_mode="HTML"
                )
            elif content.get('document'):
                await bot.send_document(
                    user_id,
                    content['document'],
                    caption=content.get('caption'),
                    parse_mode="HTML"
                )
            elif content.get('animation'):
                await bot.send_animation(
                    user_id,
                    content['animation'],
                    caption=content.get('caption'),
                    parse_mode="HTML"
                )
            elif content.get('voice'):
                await bot.send_voice(
                    user_id,
                    content['voice'],
                    caption=content.get('caption'),
                    parse_mode="HTML"
                )
            elif content.get('audio'):
                await bot.send_audio(
                    user_id,
                    content['audio'],
                    caption=content.get('caption'),
                    parse_mode="HTML"
                )
            elif content.get('sticker'):
                await bot.send_sticker(user_id, content['sticker'])
            else:
                await bot.send_message(
                    user_id,
                    content.get('text', ''),
                    parse_mode="HTML"
                )
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast failed for {user_id}: {e}")
        
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
    elif action == "cleanup":
        await callback.message.edit_text(
            "🗑 **Очистка базы данных**\n\n"
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🧹 Очистить старые логи", callback_data="cleanup_logs")],
                    [InlineKeyboardButton(text="❌ Удалить неактивных", callback_data="cleanup_inactive")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
                ]
            )
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
async def handle_unknown(message: types.Message, state: FSMContext):
    """Обработка неизвестных команд"""
    current_state = await state.get_state()
    
    if current_state:
        # Если мы в каком-то состоянии, игнорируем
        return
    
    if message.text and message.text.startswith('/'):
        await message.answer(
            "❌ Неизвестная команда.\n"
            "Доступные команды: /start, /spam, /chats, /profile, /admin"
        )

# ================== ФОНОВЫЕ ЗАДАЧИ ==================

async def check_scheduled_messages():
    """Проверка запланированных сообщений"""
    while True:
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                now = datetime.now().isoformat()
                cursor = await db.execute('''
                    SELECT * FROM scheduled_messages 
                    WHERE send_time <= ? AND is_sent = 0
                ''', (now,))
                messages = await cursor.fetchall()
                
                for msg in messages:
                    user_id = msg[1]
                    chat_id = msg[2]
                    text = msg[4]
                    
                    client = await get_pyro_client(user_id)
                    if client:
                        try:
                            await client.send_message(int(chat_id), text, parse_mode="HTML")
                            await db.execute(
                                "UPDATE scheduled_messages SET is_sent = 1 WHERE id = ?",
                                (msg[0],)
                            )
                            await db.commit()
                            logger.info(f"Scheduled message {msg[0]} sent")
                        except Exception as e:
                            logger.error(f"Error sending scheduled message: {e}")
                        finally:
                            await client.stop()
                    
                    await asyncio.sleep(1)
            
            await asyncio.sleep(60)  # Проверка каждую минуту
            
        except Exception as e:
            logger.error(f"Error in scheduled messages checker: {e}")
            await asyncio.sleep(60)

async def check_subscription_expirations():
    """Проверка истекающих подписок"""
    while True:
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                # За 3 дня до истечения
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
    
    # Запускаем фоновые задачи
    asyncio.create_task(check_scheduled_messages())
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
