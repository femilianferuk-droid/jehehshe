"""
Telegram Bot для управления аккаунтами через Pyrogram
Один файл с поддержкой переменных окружения
"""

import os
import asyncio
import logging
import re
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
    PasswordHashInvalid, AuthKeyUnregistered, FloodWait, PeerIdInvalid
)
from pyrogram.types import User as PyroUser
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
        logger.warning("Не удалось распарсить ADMIN_IDS, админ-панель будет доступна всем")

SUBSCRIPTION_PRICES = {
    "1_month": 100,
    "3_months": 250,
    "forever": 500
}
TEST_PERIOD_DAYS = 3
SPAM_BOT_USERNAME = "spambot"
SPAM_BOT_TIMEOUT = 5
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

async def save_user_session(user_id: int, session_string: str, account_info: PyroUser, phone_number: str):
    """Сохранить сессию пользователя"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        subscription_until = (datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).isoformat()
        
        await db.execute('''
            INSERT OR REPLACE INTO users 
            (user_id, username, session_string, phone_number, account_username, 
             account_first_name, account_last_name, subscription_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, 
            account_info.username or f"user_{user_id}",
            session_string,
            phone_number,
            account_info.username,
            account_info.first_name,
            account_info.last_name or "",
            subscription_until
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
        return client
    except Exception as e:
        logger.error(f"Error starting pyro client for {user_id}: {e}")
        return None

# ================== КЛАВИАТУРЫ ==================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚫 Проверить спам-блок")],
            [KeyboardButton(text="💬 Мои чаты")],
            [KeyboardButton(text="👤 Профиль")]
        ],
        resize_keyboard=True
    )

def get_profile_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура профиля"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 ПРОДЛИТЬ ПОДПИСКУ", callback_data="extend_subscription"))
    builder.row(InlineKeyboardButton(text="🔄 СБРОСИТЬ СЕССИЮ", callback_data="reset_session"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

def get_subscription_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура продления подписки"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="1 месяц - 100₽", callback_data="sub_1_month"))
    builder.row(InlineKeyboardButton(text="3 месяца - 250₽", callback_data="sub_3_months"))
    builder.row(InlineKeyboardButton(text="Навсегда - 500₽", callback_data="sub_forever"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile"))
    return builder.as_markup()

def get_chats_keyboard(has_more: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура для чатов"""
    builder = InlineKeyboardBuilder()
    if has_more:
        builder.row(InlineKeyboardButton(text="⬇️ Загрузить еще 10", callback_data="load_more_chats"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Админ-клавиатура"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="➕ Продлить подписку", callback_data="admin_extend"))
    builder.row(InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_users"))
    return builder.as_markup()

# ================== ОБРАБОТЧИКИ КОМАНД ==================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    # Проверяем, есть ли уже авторизованный пользователь
    user = await get_user(user_id)
    
    if user:
        # Если пользователь уже авторизован, показываем главное меню
        await message.answer(
            f"👋 С возвращением, {user['account_first_name']}!\n\n"
            f"Подключен аккаунт: @{user['account_username'] or 'нет'}\n"
            f"Выберите действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        # Новый пользователь - начинаем авторизацию
        await state.set_state(AuthStates.waiting_api_id)
        await message.answer(
            "👋 Добро пожаловать в UserBox Manager!\n\n"
            "🔑 ШАГ 1 ИЗ 4: Введите API ID\n\n"
            "API ID - это целое число, которое можно получить на https://my.telegram.org/apps\n\n"
            "Отправьте API ID:",
            reply_markup=ReplyKeyboardRemove()
        )

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
    
    # Отправляем сообщение о начале проверки
    status_msg = await message.answer("🔄 Проверяю статус аккаунта у @spambot...")
    
    try:
        # Получаем клиент Pyrogram
        client = await get_pyro_client(user_id)
        if not client:
            await status_msg.edit_text(
                "❌ Ошибка: не удалось подключиться к аккаунту.\n"
                "Попробуйте переавторизоваться в профиле."
            )
            return
        
        # Ищем или создаем чат с @spambot
        try:
            spambot = await client.get_users(SPAM_BOT_USERNAME)
        except Exception as e:
            logger.error(f"Error getting spambot user: {e}")
            await status_msg.edit_text(
                "❌ Не удалось найти @spambot. Возможно, бот временно недоступен."
            )
            await client.stop()
            return
        
        # Отправляем команду /start
        await client.send_message(spambot.id, "/start")
        
        # Ожидаем ответ
        start_time = time.time()
        response = None
        
        async for message_pyro in client.get_chat_history(spambot.id, limit=5):
            if message_pyro.from_user and message_pyro.from_user.id == spambot.id:
                if time.time() - start_time <= SPAM_BOT_TIMEOUT:
                    response = message_pyro
                    break
        
        await client.stop()
        
        if not response:
            await status_msg.edit_text(
                "❌ Не удалось получить ответ от @spambot. Попробуйте позже."
            )
            return
        
        # Анализируем ответ
        text = response.text or response.caption or ""
        
        # Определяем язык
        is_russian = bool(re.search('[а-яА-Я]', text))
        
        # Парсим статус
        if is_russian:
            if re.search(r'(добро пожаловать|вы не ограничены|нет ограничений)', text.lower()):
                status = "✅ Аккаунт НЕ в спам-блоке"
                status_detail = "Статус: активен без ограничений"
            elif re.search(r'(ограничены|заблокированы|имеются ограничения)', text.lower()):
                status = "🚫 Аккаунт В СПАМ-БЛОКЕ!"
                status_detail = "Статус: имеются ограничения"
            else:
                status = "❓ Не удалось определить статус"
                status_detail = "Получен нестандартный ответ"
        else:
            if re.search(r'(welcome|is not restricted|no restrictions)', text.lower()):
                status = "✅ Account is NOT restricted"
                status_detail = "Status: active, no restrictions"
            elif re.search(r'(restricted|limited|banned)', text.lower()):
                status = "🚫 Account IS RESTRICTED!"
                status_detail = "Status: restricted"
            else:
                status = "❓ Could not determine status"
                status_detail = "Received non-standard response"
        
        # Ищем дату разблокировки
        date_patterns = [
            r'до (\d{2}\.\d{2}\.\d{4})',
            r'until (\w+ \d{1,2},? \d{4})',
            r'(\d{4}-\d{2}-\d{2})'
        ]
        
        unlock_date = None
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                unlock_date = match.group(1)
                break
        
        # Формируем результат
        result_text = f"🔍 РЕЗУЛЬТАТ ПРОВЕРКИ:\n\n{status}\n{status_detail}"
        if unlock_date:
            result_text += f"\nПредполагаемая дата разблокировки: {unlock_date}"
        
        await status_msg.edit_text(result_text)
        
        # Очищаем чат (опционально)
        user = await get_user(user_id)
        if user and user.get('settings_clean_spam'):
            try:
                async with Client(
                    name=f"user_{user_id}_clean",
                    session_string=user['session_string'],
                    api_id=user['api_id'],
                    api_hash=user['api_hash'],
                    in_memory=True
                ) as clean_client:
                    async for msg in clean_client.get_chat_history(spambot.id, limit=10):
                        if msg.from_user and msg.from_user.is_self:
                            await msg.delete()
            except Exception as e:
                logger.error(f"Error cleaning spam chat: {e}")
        
        logger.info(f"User {user_id} checked spam status: {status}")
        
    except FloodWait as e:
        await status_msg.edit_text(
            f"⚠️ Слишком много запросов. Попробуйте через {e.value} секунд."
        )
    except Exception as e:
        logger.error(f"Error checking spam: {e}")
        await status_msg.edit_text(
            "❌ Произошла ошибка при проверке. Попробуйте позже."
        )

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
    
    # Получаем клиент
    client = await get_pyro_client(user_id)
    if not client:
        await message.answer(
            "❌ Ошибка: не удалось подключиться к аккаунту.\n"
            "Попробуйте переавторизоваться в профиле."
        )
        return
    
    try:
        # Получаем последний offset
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT last_offset_id, all_loaded FROM chat_pagination WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            
            offset_id = row[0] if row else 0
            all_loaded = row[1] if row and len(row) > 1 else False
        
        if all_loaded:
            await message.answer("Все чаты уже загружены. Начните сначала.")
            return
        
        # Получаем диалоги
        dialogs = []
        async for dialog in client.get_dialogs(offset_id=offset_id, limit=CHATS_PER_PAGE):
            dialogs.append(dialog)
            
            # Сохраняем в кэш
            chat = dialog.chat
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute('''
                    INSERT OR REPLACE INTO chats_cache 
                    (user_id, chat_id, chat_title, chat_type, participants_count)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    user_id, chat.id, 
                    chat.title or chat.first_name or "Unknown",
                    str(chat.type).split(".")[-1],
                    getattr(chat, 'members_count', 0)
                ))
            await db.commit()
        
        if not dialogs:
            await message.answer("У вас нет диалогов.")
            await client.stop()
            return
        
        # Обновляем offset
        last_dialog = dialogs[-1]
        has_more = len(dialogs) == CHATS_PER_PAGE
        
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                INSERT OR REPLACE INTO chat_pagination (user_id, last_offset_id, all_loaded)
                VALUES (?, ?, ?)
            ''', (user_id, last_dialog.id, not has_more))
            await db.commit()
        
        # Формируем сообщение
        chats_text = "💬 **Мои чаты:**\n\n"
        
        for i, dialog in enumerate(dialogs, 1):
            chat = dialog.chat
            
            # Определяем иконку
            if chat.type.name == "PRIVATE":
                if chat.is_bot:
                    icon = "🤖"
                else:
                    icon = "👤"
            elif chat.type.name in ["GROUP", "SUPERGROUP"]:
                icon = "👥"
            elif chat.type.name == "CHANNEL":
                icon = "📢"
            else:
                icon = "💬"
            
            # Название
            name = chat.title or chat.first_name or "Unknown"
            if chat.last_name:
                name += f" {chat.last_name}"
            
            # Тип и количество участников
            type_str = str(chat.type).split(".")[-1].lower()
            members = getattr(chat, 'members_count', None)
            members_str = f", {members} участников" if members else ""
            
            chats_text += f"{i}. {icon} {name} ({type_str}{members_str})\n"
        
        await message.answer(
            chats_text,
            reply_markup=get_chats_keyboard(has_more),
            parse_mode="Markdown"
        )
        
        await state.set_state(PaginationStates.browsing_chats)
        
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        await message.answer("❌ Ошибка при загрузке чатов.")
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
        dialogs = []
        async for dialog in client.get_dialogs(offset_id=offset_id, limit=CHATS_PER_PAGE):
            dialogs.append(dialog)
        
        if not dialogs:
            await callback.message.edit_text("Больше чатов нет.")
            return
        
        # Обновляем offset
        last_dialog = dialogs[-1]
        has_more = len(dialogs) == CHATS_PER_PAGE
        
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                INSERT OR REPLACE INTO chat_pagination (user_id, last_offset_id, all_loaded)
                VALUES (?, ?, ?)
            ''', (user_id, last_dialog.id, not has_more))
            await db.commit()
        
        # Добавляем к существующему списку
        current_text = callback.message.text or callback.message.caption or ""
        
        for i, dialog in enumerate(dialogs, current_text.count("\n") + 1):
            chat = dialog.chat
            
            if chat.type.name == "PRIVATE":
                icon = "🤖" if chat.is_bot else "👤"
            elif chat.type.name in ["GROUP", "SUPERGROUP"]:
                icon = "👥"
            elif chat.type.name == "CHANNEL":
                icon = "📢"
            else:
                icon = "💬"
            
            name = chat.title or chat.first_name or "Unknown"
            if chat.last_name:
                name += f" {chat.last_name}"
            
            type_str = str(chat.type).split(".")[-1].lower()
            members = getattr(chat, 'members_count', None)
            members_str = f", {members} участников" if members else ""
            
            current_text += f"\n{i}. {icon} {name} ({type_str}{members_str})"
        
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
        f"*Нажмите кнопку для продления или сброса сессии*"
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
        "Для оплаты свяжитесь с @admin (ссылка будет заменена)",
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
        f"3. После подтверждения подписка будет активирована\n\n"
        f"*Это демо-режим. В реальном боте будут настоящие реквизиты.*",
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
        f"**Статус:** {status}\n\n"
        f"*Нажмите кнопку для продления или сброса сессии*"
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

# ================== АДМИН-ПАНЕЛЬ ==================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Админ-панель - доступна всем пользователям"""
    await message.answer(
        "👨‍💻 **Панель администратора**\n\n"
        "Доступные команды:\n"
        "• /extend @user дни - продлить подписку\n"
        "• /stats - статистика бота\n"
        "• /users - список пользователей\n\n"
        "*Это демо-режим. В реальном боте здесь будет функционал для администраторов.*",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(Command("extend"))
async def cmd_extend(message: types.Message, state: FSMContext):
    """Продлить подписку пользователя (доступно всем)"""
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
        # Ищем пользователя по username
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
        # Получаем текущую дату
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
    """Статистика бота (доступно всем)"""
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
    
    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Активных подписок: {active_subs}\n"
        f"📈 Новых за 24ч: {new_today}\n\n"
        f"*Данные обновляются в реальном времени*"
    )
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("users"))
async def cmd_users(message: types.Message):
    """Список пользователей (доступно всем)"""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, account_username, account_first_name, subscription_until FROM users ORDER BY created_at DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
    
    if not rows:
        await message.answer("Пользователей пока нет")
        return
    
    text = "📋 **Последние 10 пользователей:**\n\n"
    
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
        
        text += f"`{user_id}` | @{username} | {name} | {status} до {sub_date}\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: types.CallbackQuery):
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
        
        # Успешный вход
        session_string = await client.export_session_string()
        await save_user_session(user_id, session_string, user, phone)
        
        await client.disconnect()
        await state.clear()
        
        await message.answer(
            f"✅ Аккаунт @{user.username or 'без username'} успешно подключен!\n\n"
            f"Вам активирован тестовый период на {TEST_PERIOD_DAYS} дня.\n"
            f"Дата окончания: {(datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).strftime('%d.%m.%Y')}\n\n"
            f"Доступные команды:\n"
            f"🚫 /spam - Проверить спам-блок\n"
            f"💬 /chats - Мои чаты\n"
            f"👤 /profile - Профиль и подписка",
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
        
        # Успешный вход
        session_string = await client.export_session_string()
        
        # Получаем данные из временной таблицы
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                "SELECT api_id, api_hash FROM auth_temp WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                api_id, api_hash = row
                # Обновляем пользователя с API данными
                async with aiosqlite.connect(DATABASE_PATH) as db2:
                    await db2.execute(
                        "UPDATE users SET api_id = ?, api_hash = ? WHERE user_id = ?",
                        (api_id, api_hash, user_id)
                    )
                    await db2.commit()
        
        await save_user_session(user_id, session_string, user, phone)
        
        await client.disconnect()
        await state.clear()
        
        await message.answer(
            f"✅ Аккаунт @{user.username or 'без username'} успешно подключен!\n\n"
            f"Вам активирован тестовый период на {TEST_PERIOD_DAYS} дня.\n"
            f"Дата окончания: {(datetime.now() + timedelta(days=TEST_PERIOD_DAYS)).strftime('%d.%m.%Y')}\n\n"
            f"Доступные команды:\n"
            f"🚫 /spam - Проверить спам-блок\n"
            f"💬 /chats - Мои чаты\n"
            f"👤 /profile - Профиль и подписка",
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
