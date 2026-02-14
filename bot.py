"""
Telegram Bot для управления аккаунтами через Pyrogram
С функциями: рассылка, автоответчик, мультиаккаунты
ИСПРАВЛЕННАЯ ВЕРСИЯ - БД работает корректно
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

# Тестовые API данные (можно использовать для всех аккаунтов)
DEFAULT_API_ID = 32480523
DEFAULT_API_HASH = "147839735c9fa4e83451209e9b55cfc5"

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
MAX_ACCOUNTS = 50  # Максимум аккаунтов на пользователя

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
        # Таблица пользователей бота
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                subscription_until TIMESTAMP,
                language TEXT DEFAULT 'ru',
                auto_clean_spam INTEGER DEFAULT 0,
                notify_expiration INTEGER DEFAULT 1,
                auto_respond INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица для аккаунтов Telegram
        await db.execute('''
            CREATE TABLE IF NOT EXISTS telegram_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                account_name TEXT,
                api_id INTEGER,
                api_hash TEXT,
                session_string TEXT,
                phone_number TEXT,
                account_username TEXT,
                account_first_name TEXT,
                account_last_name TEXT,
                account_id INTEGER,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для временных данных авторизации
        await db.execute('''
            CREATE TABLE IF NOT EXISTS auth_temp (
                user_id INTEGER PRIMARY KEY,
                account_name TEXT,
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
                account_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                chat_type TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, account_id, chat_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES telegram_accounts(id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для автоответов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS auto_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                account_id INTEGER,
                trigger_text TEXT,
                response_text TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES telegram_accounts(id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица для запланированных рассылок
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                account_id INTEGER,
                message_text TEXT,
                selected_chats TEXT,
                status TEXT DEFAULT 'pending',
                sent_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES telegram_accounts(id) ON DELETE CASCADE
            )
        ''')
        
        await db.commit()
        logger.info("Database initialized")

# ================== FSM СОСТОЯНИЯ ==================
class AuthStates(StatesGroup):
    waiting_account_name = State()
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa_password = State()

class BroadcastStates(StatesGroup):
    waiting_message = State()
    waiting_chat_selection = State()
    waiting_confirm = State()

class AutoResponseStates(StatesGroup):
    waiting_account = State()
    waiting_trigger = State()
    waiting_response = State()

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Получить данные пользователя бота из БД"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

async def create_user(user_id: int, username: str = None):
    """Создать пользователя бота"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        subscription_until = (datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).isoformat()
        await db.execute('''
            INSERT OR IGNORE INTO users (user_id, username, subscription_until)
            VALUES (?, ?, ?)
        ''', (user_id, username, subscription_until))
        await db.commit()

async def get_user_accounts(user_id: int) -> List[Dict[str, Any]]:
    """Получить все аккаунты пользователя"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM telegram_accounts 
            WHERE user_id = ? 
            ORDER BY created_at DESC
        ''', (user_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_account(user_id: int, account_id: int) -> Optional[Dict[str, Any]]:
    """Получить конкретный аккаунт"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM telegram_accounts 
            WHERE user_id = ? AND id = ?
        ''', (user_id, account_id))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def save_telegram_account(user_id: int, account_name: str, session_string: str, 
                               account_info: PyroUser, phone_number: str):
    """Сохранить аккаунт Telegram"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT INTO telegram_accounts 
            (user_id, account_name, api_id, api_hash, session_string, phone_number, 
             account_username, account_first_name, account_last_name, account_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            account_name,
            DEFAULT_API_ID,
            DEFAULT_API_HASH,
            session_string,
            phone_number,
            account_info.username,
            account_info.first_name,
            account_info.last_name or "",
            account_info.id
        ))
        await db.commit()
        logger.info(f"User {user_id} saved Telegram account @{account_info.username}")

async def delete_telegram_account(user_id: int, account_id: int):
    """Удалить аккаунт Telegram"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM telegram_accounts WHERE user_id = ? AND id = ?",
            (user_id, account_id)
        )
        await db.commit()

async def check_subscription(user_id: int) -> Tuple[bool, Optional[str]]:
    """Проверить подписку пользователя бота"""
    user = await get_user(user_id)
    if not user:
        await create_user(user_id)
        user = await get_user(user_id)
    
    if not user.get('subscription_until'):
        return True, None
    
    try:
        subscription_until = datetime.fromisoformat(user['subscription_until'])
        if datetime.now() > subscription_until:
            return False, f"Срок подписки истек {subscription_until.strftime('%d.%m.%Y')}"
    except:
        pass
    
    return True, None

async def get_pyro_client_from_account(account: Dict) -> Optional[Client]:
    """Получить Pyrogram клиент из данных аккаунта"""
    if not account or not account.get('session_string'):
        return None
    
    try:
        client = Client(
            name=f"acc_{account['id']}",
            session_string=account['session_string'],
            api_id=account['api_id'] or DEFAULT_API_ID,
            api_hash=account['api_hash'] or DEFAULT_API_HASH,
            in_memory=True
        )
        await client.start()
        
        me = await client.get_me()
        if me:
            logger.info(f"Client started for account {account['id']} (@{me.username or me.first_name})")
            return client
        else:
            await client.stop()
            return None
            
    except AuthKeyUnregistered:
        logger.error(f"Auth key unregistered for account {account['id']}")
        return None
    except Exception as e:
        logger.error(f"Error starting pyro client for account {account['id']}: {e}")
        return None

async def log_usage(user_id: int, action: str, details: str = ""):
    """Логировать использование функций"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO usage_stats (user_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, details)
        )
        await db.commit()

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

# ================== КЛАВИАТУРЫ ==================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚫 Проверить спам-блок")],
            [KeyboardButton(text="💬 Мои чаты"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="📨 Рассылка"), KeyboardButton(text="🤖 Автоответы")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_accounts_keyboard(accounts: List[Dict]) -> InlineKeyboardMarkup:
    """Клавиатура выбора аккаунта"""
    builder = InlineKeyboardBuilder()
    
    for acc in accounts:
        name = acc['account_name']
        username = acc['account_username'] or "без username"
        builder.row(InlineKeyboardButton(
            text=f"📱 {name} (@{username})",
            callback_data=f"select_acc_{acc['id']}"
        ))
    
    builder.row(InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    return builder.as_markup()

def get_profile_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура профиля"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📱 Мои аккаунты", callback_data="my_accounts"))
    builder.row(InlineKeyboardButton(text="💳 ПРОДЛИТЬ ПОДПИСКУ", callback_data="extend_subscription"))
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

def get_chats_selection_keyboard(dialogs: List[Dict], selected: List[int], page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура выбора чатов для рассылки"""
    builder = InlineKeyboardBuilder()
    
    start = page * 10
    end = start + 10
    page_dialogs = dialogs[start:end]
    
    for dialog in page_dialogs:
        chat_id = dialog['id']
        title = dialog['title'][:20] + "..." if len(dialog['title']) > 20 else dialog['title']
        
        if dialog['type'] == ChatType.PRIVATE:
            icon = "👤"
        elif dialog['type'] in [ChatType.GROUP, ChatType.SUPERGROUP]:
            icon = "👥"
        elif dialog['type'] == ChatType.CHANNEL:
            icon = "📢"
        else:
            icon = "💬"
        
        selected_mark = "✅ " if chat_id in selected else ""
        builder.row(InlineKeyboardButton(
            text=f"{selected_mark}{icon} {title}",
            callback_data=f"toggle_chat_{chat_id}"
        ))
    
    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"chat_page_{page-1}"))
    if end < len(dialogs):
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"chat_page_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.row(
        InlineKeyboardButton(text=f"✅ Выбрано: {len(selected)}", callback_data="show_selected"),
        InlineKeyboardButton(text="📊 Всего", callback_data=f"total_chats_{len(dialogs)}")
    )
    
    builder.row(
        InlineKeyboardButton(text="▶️ Запустить рассылку", callback_data="start_broadcast"),
        InlineKeyboardButton(text="❌ Очистить", callback_data="clear_selected")
    )
    
    builder.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_broadcast"))
    
    return builder.as_markup()

def get_auto_responses_keyboard(responses: List[Dict]) -> InlineKeyboardMarkup:
    """Клавиатура автоответов"""
    builder = InlineKeyboardBuilder()
    
    for resp in responses:
        trigger = resp['trigger_text'][:15] + "..." if len(resp['trigger_text']) > 15 else resp['trigger_text']
        status = "✅" if resp['is_active'] else "❌"
        builder.row(InlineKeyboardButton(
            text=f"{status} {trigger}",
            callback_data=f"edit_response_{resp['id']}"
        ))
    
    builder.row(InlineKeyboardButton(text="➕ Создать автоответ", callback_data="create_response"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    return builder.as_markup()

# ================== ОБРАБОТЧИКИ КОМАНД ==================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    # Создаем пользователя если его нет
    await create_user(user_id, message.from_user.username)
    
    # Проверяем есть ли аккаунты
    accounts = await get_user_accounts(user_id)
    
    if accounts:
        await message.answer(
            f"👋 С возвращением!\n\n"
            f"У вас {len(accounts)} аккаунтов\n"
            f"Выберите действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        # Нет аккаунтов - предлагаем добавить
        await message.answer(
            "👋 Добро пожаловать в UserBox Manager!\n\n"
            "У вас пока нет добавленных аккаунтов Telegram.\n"
            "Нажмите кнопку ниже, чтобы добавить аккаунт:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")]
                ]
            )
        )

# ================== УПРАВЛЕНИЕ АККАУНТАМИ ==================

@dp.callback_query(F.data == "my_accounts")
async def my_accounts(callback: types.CallbackQuery):
    """Список аккаунтов пользователя"""
    user_id = callback.from_user.id
    accounts = await get_user_accounts(user_id)
    
    if not accounts:
        await callback.message.edit_text(
            "📱 У вас нет добавленных аккаунтов.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
                ]
            )
        )
        await callback.answer()
        return
    
    text = "📱 **Ваши аккаунты:**\n\n"
    
    for acc in accounts:
        status = "✅ Активен" if acc['is_active'] else "❌ Неактивен"
        text += f"**{acc['account_name']}**\n"
        text += f"└ @{acc['account_username'] or 'без username'}\n"
        text += f"└ {acc['account_first_name']} {acc['account_last_name'] or ''}\n"
        text += f"└ Статус: {status}\n"
        text += f"└ ID: `{acc['id']}`\n\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "add_account")
async def add_account_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало добавления аккаунта"""
    user_id = callback.from_user.id
    accounts = await get_user_accounts(user_id)
    
    if len(accounts) >= MAX_ACCOUNTS:
        await callback.message.edit_text(
            f"❌ Вы достигли максимального количества аккаунтов ({MAX_ACCOUNTS}).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
            )
        )
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "📱 **Добавление нового аккаунта**\n\n"
        "Шаг 1: Введите название для аккаунта (например: 'Рабочий', 'Личный'):"
    )
    await state.set_state(AuthStates.waiting_account_name)
    await callback.answer()

@dp.message(AuthStates.waiting_account_name)
async def auth_get_account_name(message: types.Message, state: FSMContext):
    """Получение названия аккаунта"""
    account_name = message.text.strip()
    
    if len(account_name) > 50:
        await message.answer("❌ Название слишком длинное. Максимум 50 символов:")
        return
    
    await state.update_data(account_name=account_name)
    await state.set_state(AuthStates.waiting_phone)
    
    await message.answer(
        "📱 **ШАГ 2 ИЗ 4:** Введите номер телефона\n\n"
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
    
    data = await state.get_data()
    account_name = data['account_name']
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO auth_temp 
            (user_id, account_name, api_id, api_hash, phone_number, code_attempts, password_attempts)
            VALUES (?, ?, ?, ?, ?, 0, 0)
        ''', (user_id, account_name, DEFAULT_API_ID, DEFAULT_API_HASH, phone))
        await db.commit()
    
    try:
        client = Client(
            name=f"auth_{user_id}_{int(datetime.now().timestamp())}",
            api_id=DEFAULT_API_ID,
            api_hash=DEFAULT_API_HASH,
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
            "📱 **ШАГ 3 ИЗ 4:** Введите код из Telegram\n\n"
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
        await message.answer("❌ Ошибка сессии. Начните заново.")
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
            await message.answer(f"❌ Превышено количество попыток ({MAX_CODE_ATTEMPTS}). Начните заново.")
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
                "SELECT account_name FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            account_name = row[0] if row else "Аккаунт"
        
        session_string = await client.export_session_string()
        await save_telegram_account(user_id, account_name, session_string, user, phone)
        
        await client.disconnect()
        await state.clear()
        
        await message.answer(
            f"✅ Аккаунт @{user.username or user.first_name} успешно добавлен!\n\n"
            f"Теперь вы можете использовать его для всех функций бота.",
            reply_markup=get_main_keyboard()
        )
        
        logger.info(f"User {user_id} added Telegram account @{user.username}")
        
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
        await message.answer("❌ Ошибка сессии. Начните заново.")
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
            await message.answer(f"❌ Превышено количество попыток ({MAX_2FA_ATTEMPTS}). Начните заново.")
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
                "SELECT account_name FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            account_name = row[0] if row else "Аккаунт"
        
        session_string = await client.export_session_string()
        await save_telegram_account(user_id, account_name, session_string, user, phone)
        
        await client.disconnect()
        await state.clear()
        
        await message.answer(
            f"✅ Аккаунт @{user.username or user.first_name} успешно добавлен!\n\n"
            f"Теперь вы можете использовать его для всех функций бота.",
            reply_markup=get_main_keyboard()
        )
        
        logger.info(f"User {user_id} added Telegram account with 2FA @{user.username}")
        
    except PasswordHashInvalid:
        remaining = MAX_2FA_ATTEMPTS - (attempts + 1)
        await message.answer(f"❌ Неверный пароль. Осталось попыток: {remaining}")
    except Exception as e:
        logger.error(f"Error during 2FA: {e}")
        await message.answer("❌ Ошибка при проверке пароля. Попробуйте позже.")
        await client.disconnect()
        await state.clear()

# ================== ПРОВЕРКА СПАМ-БЛОКА ==================

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
    
    accounts = await get_user_accounts(user_id)
    
    if not accounts:
        await message.answer(
            "❌ У вас нет добавленных аккаунтов.\n"
            "Сначала добавьте аккаунт через профиль."
        )
        return
    
    if len(accounts) == 1:
        # Если один аккаунт - проверяем сразу
        await check_spam_for_account(message, accounts[0])
    else:
        # Если несколько - предлагаем выбрать
        await message.answer(
            "Выберите аккаунт для проверки:",
            reply_markup=get_accounts_keyboard(accounts)
        )

async def check_spam_for_account(message: types.Message, account: Dict):
    """Проверка спама для конкретного аккаунта"""
    status_msg = await message.answer(f"🔄 Проверяю аккаунт {account['account_name']}...")
    
    client = await get_pyro_client_from_account(account)
    if not client:
        await status_msg.edit_text("❌ Ошибка подключения к аккаунту")
        return
    
    try:
        me = await client.get_me()
        
        try:
            spambot = await client.get_users(SPAM_BOT_USERNAME)
        except UsernameNotOccupied:
            await status_msg.edit_text("❌ Бот @spambot не найден")
            await client.stop()
            return
        
        await client.send_message(spambot.id, "/start")
        await asyncio.sleep(3)
        
        messages = []
        async for msg in client.get_chat_history(spambot.id, limit=10):
            if msg.from_user and msg.from_user.id == spambot.id:
                messages.append(msg)
        
        if not messages:
            await status_msg.edit_text("❌ Нет ответа от @spambot")
            await client.stop()
            return
        
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
        
        status_text = "✅ Аккаунт НЕ в спам-блоке" if not is_restricted else "🚫 Аккаунт В СПАМ-БЛОКЕ!"
        
        result_text = f"🔍 **Результат для {account['account_name']}**\n\n"
        result_text += f"Аккаунт: @{me.username or me.first_name}\n"
        result_text += f"Статус: {status_text}\n"
        if unlock_date:
            result_text += f"Дата разблокировки: {unlock_date}"
        
        await status_msg.edit_text(result_text, parse_mode="Markdown")
        await log_usage(message.from_user.id, "spam_check", account['account_name'])
        
    except Exception as e:
        logger.error(f"Error checking spam: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    finally:
        await client.stop()

@dp.callback_query(F.data.startswith("select_acc_"))
async def select_account_for_spam(callback: types.CallbackQuery, state: FSMContext):
    """Выбор аккаунта для проверки спама"""
    account_id = int(callback.data.replace("select_acc_", ""))
    user_id = callback.from_user.id
    
    account = await get_account(user_id, account_id)
    if account:
        await check_spam_for_account(callback.message, account)
    
    await callback.answer()

# ================== ПРОСМОТР ЧАТОВ ==================

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
    
    accounts = await get_user_accounts(user_id)
    
    if not accounts:
        await message.answer(
            "❌ У вас нет добавленных аккаунтов.\n"
            "Сначала добавьте аккаунт через профиль."
        )
        return
    
    if len(accounts) == 1:
        await show_chats_for_account(message, accounts[0], state)
    else:
        await message.answer(
            "Выберите аккаунт для просмотра чатов:",
            reply_markup=get_accounts_keyboard(accounts)
        )

async def show_chats_for_account(message: types.Message, account: Dict, state: FSMContext):
    """Показать чаты для конкретного аккаунта"""
    status_msg = await message.answer(f"🔄 Загружаю чаты для {account['account_name']}...")
    
    client = await get_pyro_client_from_account(account)
    if not client:
        await status_msg.edit_text("❌ Ошибка подключения к аккаунту")
        return
    
    try:
        dialogs = await get_all_dialogs(client)
        
        if not dialogs:
            await status_msg.edit_text(
                f"📭 У аккаунта {account['account_name']} нет активных чатов."
            )
            await client.stop()
            return
        
        dialogs.sort(key=lambda x: (-x.get('pinned', 0), x.get('last_message_date') or datetime.min), reverse=True)
        
        await state.update_data(current_account=account['id'], all_dialogs=dialogs, current_page=0)
        
        await show_chats_page(message, dialogs, 0, status_msg, account)
        
        await log_usage(message.from_user.id, "view_chats", account['account_name'])
        
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка при загрузке чатов: {str(e)[:100]}"
        )
    finally:
        await client.stop()

async def show_chats_page(message: types.Message, dialogs: List[Dict], page: int, 
                         edit_msg: types.Message = None, account: Dict = None):
    """Показать страницу чатов"""
    start = page * CHATS_PER_PAGE
    end = start + CHATS_PER_PAGE
    page_dialogs = dialogs[start:end]
    
    if not page_dialogs:
        if edit_msg:
            await edit_msg.edit_text("Чатов больше нет")
        return
    
    total_pages = (len(dialogs) + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE
    account_name = f" для {account['account_name']}" if account else ""
    text = f"💬 **Мои чаты{account_name}** (стр {page + 1}/{total_pages}):\n\n"
    
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
    
    builder.row(InlineKeyboardButton(text="⭐ В избранное", callback_data="show_favorites"))
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
    account_id = data.get('current_account')
    
    if dialogs and account_id:
        user_id = callback.from_user.id
        account = await get_account(user_id, account_id)
        await show_chats_page(callback.message, dialogs, page, callback.message, account)
    
    await callback.answer()

# ================== РАССЫЛКА ==================

@dp.message(F.text == "📨 Рассылка")
async def cmd_broadcast(message: types.Message, state: FSMContext):
    """Начало рассылки"""
    user_id = message.from_user.id
    
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку.",
            reply_markup=get_main_keyboard()
        )
        return
    
    accounts = await get_user_accounts(user_id)
    
    if not accounts:
        await message.answer(
            "❌ У вас нет добавленных аккаунтов.\n"
            "Сначала добавьте аккаунт через профиль."
        )
        return
    
    if len(accounts) == 1:
        await state.update_data(broadcast_account=accounts[0]['id'])
        await start_broadcast_message_input(message, state)
    else:
        await message.answer(
            "Выберите аккаунт для рассылки:",
            reply_markup=get_accounts_keyboard(accounts)
        )

async def start_broadcast_message_input(message: types.Message, state: FSMContext):
    """Начало ввода сообщения для рассылки"""
    await message.answer(
        "📨 **Создание рассылки**\n\n"
        "Шаг 1: Отправьте текст сообщения для рассылки\n\n"
        "Поддерживается Markdown форматирование: *жирный*, _курсив_, `код`",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_message)

@dp.message(BroadcastStates.waiting_message)
async def broadcast_get_message(message: types.Message, state: FSMContext):
    """Получение сообщения для рассылки"""
    await state.update_data(broadcast_message=message.html_text)
    
    user_id = message.from_user.id
    data = await state.get_data()
    account_id = data.get('broadcast_account')
    account = await get_account(user_id, account_id)
    
    if not account:
        await message.answer("❌ Ошибка: аккаунт не найден")
        await state.clear()
        return
    
    # Загружаем чаты аккаунта
    status_msg = await message.answer("🔄 Загружаю список чатов...")
    
    client = await get_pyro_client_from_account(account)
    if not client:
        await status_msg.edit_text("❌ Ошибка подключения к аккаунту")
        await state.clear()
        return
    
    try:
        dialogs = await get_all_dialogs(client)
        
        if not dialogs:
            await status_msg.edit_text("📭 У аккаунта нет чатов для рассылки")
            await client.stop()
            await state.clear()
            return
        
        # Сохраняем диалоги в состояние
        await state.update_data(
            broadcast_dialogs=dialogs,
            selected_chats=[],
            current_page=0
        )
        
        await status_msg.edit_text(
            f"📨 **Шаг 2: Выберите чаты для рассылки**\n\n"
            f"Всего доступно чатов: {len(dialogs)}\n"
            f"Можно выбрать до 10 чатов.\n\n"
            f"Нажимайте на чаты, чтобы выбрать/отменить выбор:",
            reply_markup=get_chats_selection_keyboard(dialogs, [], 0),
            parse_mode="Markdown"
        )
        
        await state.set_state(BroadcastStates.waiting_chat_selection)
        
    except Exception as e:
        logger.error(f"Error loading chats for broadcast: {e}")
        await status_msg.edit_text(f"❌ Ошибка загрузки чатов: {str(e)[:100]}")
        await state.clear()
    finally:
        await client.stop()

@dp.callback_query(BroadcastStates.waiting_chat_selection, F.data.startswith("toggle_chat_"))
async def toggle_chat_selection(callback: types.CallbackQuery, state: FSMContext):
    """Выбор/отмена выбора чата"""
    chat_id = int(callback.data.replace("toggle_chat_", ""))
    
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    dialogs = data.get('broadcast_dialogs', [])
    page = data.get('current_page', 0)
    
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        if len(selected) >= 10:
            await callback.answer("❌ Можно выбрать не более 10 чатов", show_alert=True)
            return
        selected.append(chat_id)
    
    await state.update_data(selected_chats=selected)
    
    await callback.message.edit_reply_markup(
        reply_markup=get_chats_selection_keyboard(dialogs, selected, page)
    )
    await callback.answer()

@dp.callback_query(BroadcastStates.waiting_chat_selection, F.data.startswith("chat_page_"))
async def chat_page_navigation(callback: types.CallbackQuery, state: FSMContext):
    """Навигация по страницам чатов"""
    page = int(callback.data.replace("chat_page_", ""))
    
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    dialogs = data.get('broadcast_dialogs', [])
    
    await state.update_data(current_page=page)
    
    await callback.message.edit_reply_markup(
        reply_markup=get_chats_selection_keyboard(dialogs, selected, page)
    )
    await callback.answer()

@dp.callback_query(BroadcastStates.waiting_chat_selection, F.data == "show_selected")
async def show_selected_chats(callback: types.CallbackQuery, state: FSMContext):
    """Показать выбранные чаты"""
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    dialogs = data.get('broadcast_dialogs', [])
    
    if not selected:
        await callback.answer("❌ Нет выбранных чатов", show_alert=True)
        return
    
    text = "✅ **Выбранные чаты:**\n\n"
    
    for i, chat_id in enumerate(selected, 1):
        for dialog in dialogs:
            if dialog['id'] == chat_id:
                text += f"{i}. {dialog['title']}\n"
                break
    
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(BroadcastStates.waiting_chat_selection, F.data == "clear_selected")
async def clear_selected_chats(callback: types.CallbackQuery, state: FSMContext):
    """Очистить выбранные чаты"""
    data = await state.get_data()
    dialogs = data.get('broadcast_dialogs', [])
    page = data.get('current_page', 0)
    
    await state.update_data(selected_chats=[])
    
    await callback.message.edit_reply_markup(
        reply_markup=get_chats_selection_keyboard(dialogs, [], page)
    )
    await callback.answer("✅ Выбор очищен")

@dp.callback_query(BroadcastStates.waiting_chat_selection, F.data == "start_broadcast")
async def start_broadcast_confirm(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение запуска рассылки"""
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    message_text = data.get('broadcast_message')
    
    if not selected:
        await callback.answer("❌ Выберите хотя бы один чат", show_alert=True)
        return
    
    if len(selected) > 10:
        await callback.answer("❌ Можно выбрать не более 10 чатов", show_alert=True)
        return
    
    # Показываем предпросмотр
    preview = f"📨 **Предпросмотр рассылки**\n\n"
    preview += f"**Сообщение:**\n{message_text[:200]}"
    if len(message_text) > 200:
        preview += "..."
    preview += f"\n\n**Количество чатов:** {len(selected)}"
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_broadcast"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")
    )
    
    await callback.message.edit_text(
        preview,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BroadcastStates.waiting_confirm)
    await callback.answer()

@dp.callback_query(BroadcastStates.waiting_confirm, F.data == "confirm_broadcast")
async def execute_broadcast(callback: types.CallbackQuery, state: FSMContext):
    """Выполнение рассылки"""
    data = await state.get_data()
    user_id = callback.from_user.id
    account_id = data.get('broadcast_account')
    selected = data.get('selected_chats', [])
    message_text = data.get('broadcast_message')
    dialogs = data.get('broadcast_dialogs', [])
    
    account = await get_account(user_id, account_id)
    if not account:
        await callback.message.edit_text("❌ Аккаунт не найден")
        await state.clear()
        return
    
    status_msg = await callback.message.edit_text("🔄 Запускаю рассылку...")
    
    client = await get_pyro_client_from_account(account)
    if not client:
        await status_msg.edit_text("❌ Ошибка подключения к аккаунту")
        await state.clear()
        return
    
    sent = 0
    failed = 0
    
    for chat_id in selected:
        try:
            # Находим название чата
            chat_title = "Неизвестный чат"
            for dialog in dialogs:
                if dialog['id'] == chat_id:
                    chat_title = dialog['title']
                    break
            
            await client.send_message(chat_id, message_text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(1)  # Задержка между сообщениями
            
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast error to {chat_id}: {e}")
    
    await client.stop()
    
    # Сохраняем в историю
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT INTO scheduled_broadcasts 
            (user_id, account_id, message_text, selected_chats, status, sent_count, total_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, account_id, message_text[:500], 
            json.dumps(selected), 'completed', sent, len(selected)
        ))
        await db.commit()
    
    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Всего чатов: {len(selected)}"
    )
    
    await log_usage(user_id, "broadcast", f"Sent: {sent}, Failed: {failed}")
    await state.clear()

@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: types.CallbackQuery, state: FSMContext):
    """Отмена рассылки"""
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена")
    await callback.answer()

# ================== АВТООТВЕТЫ ==================

@dp.message(F.text == "🤖 Автоответы")
async def cmd_auto_responses(message: types.Message, state: FSMContext):
    """Управление автоответами"""
    user_id = message.from_user.id
    
    valid, error_msg = await check_subscription(user_id)
    if not valid:
        await message.answer(
            f"⚠️ {error_msg}\n\n"
            f"Для продолжения использования продлите подписку.",
            reply_markup=get_main_keyboard()
        )
        return
    
    accounts = await get_user_accounts(user_id)
    
    if not accounts:
        await message.answer(
            "❌ У вас нет добавленных аккаунтов.\n"
            "Сначала добавьте аккаунт через профиль."
        )
        return
    
    if len(accounts) == 1:
        await show_auto_responses(message, accounts[0])
    else:
        await message.answer(
            "Выберите аккаунт для управления автоответами:",
            reply_markup=get_accounts_keyboard(accounts)
        )

async def show_auto_responses(message: types.Message, account: Dict):
    """Показать автоответы аккаунта"""
    user_id = message.from_user.id if isinstance(message, types.Message) else message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM auto_responses 
            WHERE user_id = ? AND account_id = ? 
            ORDER BY created_at DESC
        ''', (user_id, account['id']))
        responses = await cursor.fetchall()
        responses = [dict(r) for r in responses]
    
    if not responses:
        text = f"🤖 **Автоответы для {account['account_name']}**\n\n"
        text += "У вас пока нет созданных автоответов."
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Создать автоответ", callback_data=f"create_response_{account['id']}"))
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
        
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        await message.answer(
            f"🤖 **Автоответы для {account['account_name']}**",
            reply_markup=get_auto_responses_keyboard(responses),
            parse_mode="Markdown"
        )

@dp.callback_query(F.data.startswith("create_response_"))
async def create_response_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало создания автоответа"""
    account_id = int(callback.data.replace("create_response_", ""))
    await state.update_data(response_account=account_id)
    
    await callback.message.edit_text(
        "🤖 **Создание автоответа**\n\n"
        "Шаг 1: Введите ключевое слово или фразу (триггер)\n"
        "Например: 'привет', 'здравствуйте', 'help'"
    )
    await state.set_state(AutoResponseStates.waiting_trigger)
    await callback.answer()

@dp.message(AutoResponseStates.waiting_trigger)
async def response_get_trigger(message: types.Message, state: FSMContext):
    """Получение триггера автоответа"""
    trigger = message.text.lower().strip()
    
    if len(trigger) > 100:
        await message.answer("❌ Слишком длинный триггер. Максимум 100 символов:")
        return
    
    await state.update_data(response_trigger=trigger)
    await message.answer(
        "📝 Шаг 2: Введите текст ответа\n\n"
        "Поддерживается Markdown форматирование: *жирный*, _курсив_, `код`"
    )
    await state.set_state(AutoResponseStates.waiting_response)

@dp.message(AutoResponseStates.waiting_response)
async def response_get_text(message: types.Message, state: FSMContext):
    """Получение текста ответа"""
    response_text = message.html_text
    
    data = await state.get_data()
    user_id = message.from_user.id
    account_id = data.get('response_account')
    trigger = data.get('response_trigger')
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT INTO auto_responses (user_id, account_id, trigger_text, response_text)
            VALUES (?, ?, ?, ?)
        ''', (user_id, account_id, trigger, response_text))
        await db.commit()
    
    await message.answer(
        f"✅ Автоответ создан!\n\n"
        f"**Триггер:** {trigger}\n"
        f"**Ответ:** {response_text[:100]}..."
    )
    
    await state.clear()

# ================== ПРОФИЛЬ И ПОДПИСКА ==================

@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: types.Message):
    """Просмотр профиля"""
    user_id = message.from_user.id
    user = await get_user(user_id)
    accounts = await get_user_accounts(user_id)
    
    if not user:
        await create_user(user_id, message.from_user.username)
        user = await get_user(user_id)
    
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
        f"**ID:** `{user_id}`\n"
        f"**Username:** @{message.from_user.username or 'нет'}\n"
        f"**Аккаунтов Telegram:** {len(accounts)}/{MAX_ACCOUNTS}\n\n"
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
            SELECT action, COUNT(*) 
            FROM usage_stats 
            WHERE user_id = ? 
            GROUP BY action 
            ORDER BY COUNT(*) DESC
        ''', (user_id,))
        by_action = await cursor.fetchall()
    
    text = f"📊 **Ваша статистика**\n\n"
    text += f"Всего действий: {total}\n\n"
    text += "**По функциям:**\n"
    
    for action, count in by_action:
        action_name = {
            "spam_check": "Проверка спама",
            "view_chats": "Просмотр чатов",
            "broadcast": "Рассылка",
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
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="1 месяц - 100₽", callback_data="sub_1_month")],
                [InlineKeyboardButton(text="3 месяца - 250₽", callback_data="sub_3_months")],
                [InlineKeyboardButton(text="Навсегда - 500₽", callback_data="sub_forever")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
            ]
        ),
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

# ================== НАСТРОЙКИ ==================

@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    """Настройки пользователя"""
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user:
        await create_user(user_id, message.from_user.username)
        user = await get_user(user_id)
    
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

# ================== ИЗБРАННОЕ ==================

@dp.message(F.text == "⭐ Избранное")
async def cmd_favorites(message: types.Message):
    """Просмотр избранных чатов"""
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT f.*, a.account_name 
            FROM favorite_chats f
            JOIN telegram_accounts a ON f.account_id = a.id
            WHERE f.user_id = ? 
            ORDER BY f.added_at DESC
        ''', (user_id,))
        favorites = await cursor.fetchall()
    
    if not favorites:
        await message.answer(
            "⭐ **Избранное**\n\n"
            "У вас пока нет избранных чатов.\n"
            "Добавляйте чаты в избранное из списка чатов.",
            parse_mode="Markdown"
        )
        return
    
    builder = InlineKeyboardBuilder()
    
    for fav in favorites:
        title = fav['chat_title'][:25] + "..." if len(fav['chat_title']) > 25 else fav['chat_title']
        builder.row(InlineKeyboardButton(
            text=f"⭐ [{fav['account_name']}] {title}",
            callback_data=f"fav_open_{fav['chat_id']}_{fav['account_id']}"
        ))
    
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await message.answer(
        "⭐ **Избранные чаты:**",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("fav_open_"))
async def open_favorite(callback: types.CallbackQuery):
    """Открыть избранный чат"""
    parts = callback.data.replace("fav_open_", "").split("_")
    chat_id = int(parts[0])
    account_id = int(parts[1])
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await callback.message.answer(
        f"Чат открыт. ID: `{chat_id}`",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

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

# ================== АДМИН-ПАНЕЛЬ ==================

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
                "SELECT user_id FROM users WHERE username = ?",
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
        
        cursor = await db.execute("SELECT COUNT(*) FROM telegram_accounts")
        total_accounts = (await cursor.fetchone())[0]
        
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE subscription_until > ?",
            (now,)
        )
        active_subs = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT COUNT(*) FROM usage_stats")
        total_actions = (await cursor.fetchone())[0]
    
    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"📱 Аккаунтов TG: {total_accounts}\n"
        f"✅ Активных подписок: {active_subs}\n"
        f"🔄 Всего действий: {total_actions}"
    )
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("users"))
async def cmd_users(message: types.Message):
    """Список пользователей"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT u.user_id, u.username, u.subscription_until, 
                   COUNT(a.id) as accounts_count
            FROM users u
            LEFT JOIN telegram_accounts a ON u.user_id = a.user_id
            GROUP BY u.user_id
            ORDER BY u.created_at DESC 
            LIMIT 20
        ''')
        rows = await cursor.fetchall()
    
    if not rows:
        await message.answer("Пользователей пока нет")
        return
    
    text = "📋 **Последние 20 пользователей:**\n\n"
    
    for row in rows:
        user_id = row['user_id']
        username = row['username'] or 'нет'
        accounts = row['accounts_count']
        sub_until = row['subscription_until']
        
        if sub_until:
            try:
                if datetime.fromisoformat(sub_until) > datetime.now():
                    status = "✅"
                else:
                    status = "❌"
            except:
                status = "❌"
        else:
            status = "❌"
        
        text += f"`{user_id}` | @{username} | 📱{accounts} | {status}\n"
    
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
            "Доступные команды: /start, /admin"
        )

# ================== ФОНОВЫЕ ЗАДАЧИ ==================

async def check_subscription_expirations():
    """Проверка истекающих подписок"""
    while True:
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                warning_date = (datetime.now() + timedelta(days=3)).isoformat()
                cursor = await db.execute('''
                    SELECT user_id, username, subscription_until 
                    FROM users 
                    WHERE subscription_until <= ? AND notify_expiration = 1
                ''', (warning_date,))
                expiring = await cursor.fetchall()
                
                for user_id, username, sub_until in expiring:
                    try:
                        sub_date = datetime.fromisoformat(sub_until).strftime('%d.%m.%Y')
                        await bot.send_message(
                            user_id,
                            f"⚠️ **Уведомление о подписке**\n\n"
                            f"Уважаемый пользователь, ваша подписка истекает {sub_date}.\n"
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
