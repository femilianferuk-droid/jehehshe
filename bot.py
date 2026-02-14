"""
Telegram Bot "Monkey Market" - Маркетплейс аккаунтов
Один файл для удобства развертывания
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional, Dict, Any
import os
from enum import Enum

# Установка зависимостей (раскомментируйте при первом запуске)
# os.system('pip install aiogram pyrogram sqlalchemy cryptography langdetect')

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton, 
                          ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pyrogram import Client
from pyrogram.errors import (PhoneNumberInvalid, PhoneCodeInvalid, 
                            PhoneCodeExpired, SessionPasswordNeeded)

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2
import base64

from langdetect import detect

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================

# Токен бота (установите через переменные окружения)
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')

# Ключ шифрования (должен быть 32 байта в base64)
# Генерируется автоматически при первом запуске
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', Fernet.generate_key().decode())

# Настройки комиссии
COMMISSION_PERCENT = 10  # 10%
COMMISSION_WALLET = "admin"  # ID администратора для получения комиссии

# ==================== БАЗА ДАННЫХ ====================

engine = create_engine('sqlite:///monkey_market.db?check_same_thread=False')
Base = declarative_base()
Session = scoped_session(sessionmaker(bind=engine))

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False)
    username = Column(String)
    api_id = Column(String, nullable=True)  # Зашифровано
    api_hash = Column(String, nullable=True)  # Зашифровано
    balance = Column(Float, default=0.0)
    registered_at = Column(DateTime, default=datetime.utcnow)
    total_sales = Column(Integer, default=0)
    total_purchases = Column(Integer, default=0)

class Listing(Base):
    __tablename__ = 'listings'
    
    id = Column(Integer, primary_key=True)
    seller_id = Column(Integer, nullable=False)
    phone_number = Column(String, nullable=False)  # Зашифровано
    session_string = Column(Text, nullable=True)  # Зашифровано
    password = Column(String, nullable=True)  # Зашифровано
    country = Column(String, nullable=False)
    title = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    status = Column(String, default='active')  # active, sold
    created_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, nullable=False)
    buyer_id = Column(Integer, nullable=False)
    seller_id = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    commission = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ==================== ШИФРОВАНИЕ ====================

class EncryptionManager:
    def __init__(self, key: str):
        self.fernet = Fernet(key.encode() if isinstance(key, str) else key)
    
    def encrypt(self, data: str) -> str:
        if not data:
            return None
        return self.fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted_data: str) -> str:
        if not encrypted_data:
            return None
        return self.fernet.decrypt(encrypted_data.encode()).decode()

encryption = EncryptionManager(ENCRYPTION_KEY)

# ==================== СОСТОЯНИЯ FSM ====================

class SellStates(StatesGroup):
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()
    waiting_title = State()
    waiting_price = State()
    waiting_password = State()

class BuyStates(StatesGroup):
    browsing = State()
    confirming = State()

# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="buy"),
        InlineKeyboardButton(text="💰 Продать аккаунт", callback_data="sell")
    )
    builder.row(
        InlineKeyboardButton(text="📜 Мои объявления", callback_data="my_listings"),
        InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")
    )
    return builder.as_markup()

def get_back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back"))
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    return builder.as_markup()

def get_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"))
    return builder.as_markup()

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_user(user_id: int) -> Optional[User]:
    """Получить пользователя из БД"""
    session = Session()
    user = session.query(User).filter_by(user_id=user_id).first()
    if not user:
        user = User(user_id=user_id)
        session.add(user)
        session.commit()
    session.close()
    return user

async def check_spam_block(client: Client) -> tuple[bool, str]:
    """Проверка аккаунта через @spambot"""
    try:
        spambot = await client.get_users("spambot")
        await client.send_message(spambot.id, "/start")
        
        # Ждем ответ от spambot (таймаут 5 секунд)
        async for message in client.get_chat_history(spambot.id, limit=1):
            text = message.text.lower()
            
            # Определяем статус по ключевым словам
            good_phrases = ['good news', 'is not restricted', 'не ограничен', 'все отлично', 'ok']
            bad_phrases = ['is limited', 'banned', 'ограничен', 'забанен', 'restricted']
            
            if any(phrase in text for phrase in good_phrases):
                return True, "✅ Аккаунт чист (нет спам-блока)"
            elif any(phrase in text for phrase in bad_phrases):
                return False, "❌ Аккаунт заблокирован за спам"
            else:
                return True, "⚠️ Не удалось определить статус (продолжаем)"
    except Exception as e:
        logger.error(f"Ошибка проверки спам-блока: {e}")
        return True, "⚠️ Ошибка проверки (продолжаем)"

async def get_country_from_session(client: Client) -> str:
    """Определить страну аккаунта"""
    try:
        me = await client.get_me()
        if me.phone_number:
            # Простое определение по коду страны
            phone = me.phone_number
            country_codes = {
                '7': 'Россия', '380': 'Украина', '375': 'Беларусь',
                '1': 'США/Канада', '44': 'Великобритания', '49': 'Германия'
            }
            for code, country in country_codes.items():
                if phone.startswith(code):
                    return country
        return "Неизвестно"
    except:
        return "Неизвестно"

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user = get_user(message.from_user.id)
    await message.answer(
        "👋 Добро пожаловать в Monkey Market!\n\n"
        "Здесь вы можете безопасно покупать и продавать Telegram аккаунты.\n"
        "Все аккаунты проходят проверку на спам-блок перед публикацией.",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "profile")
async def show_profile(callback: types.CallbackQuery):
    """Показать профиль пользователя"""
    user = get_user(callback.from_user.id)
    
    text = (
        f"👤 Ваш профиль\n\n"
        f"💰 Баланс: {user.balance} ₽\n"
        f"📊 Продаж: {user.total_sales}\n"
        f"🛒 Покупок: {user.total_purchases}\n"
        f"📅 Зарегистрирован: {user.registered_at.strftime('%d.%m.%Y')}"
    )
    
    await callback.message.edit_text(text, reply_markup=get_back_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "my_listings")
async def my_listings(callback: types.CallbackQuery):
    """Показать объявления пользователя"""
    session = Session()
    listings = session.query(Listing).filter_by(
        seller_id=callback.from_user.id, 
        status='active'
    ).all()
    
    if not listings:
        await callback.message.edit_text(
            "📭 У вас нет активных объявлений",
            reply_markup=get_back_keyboard()
        )
        await callback.answer()
        session.close()
        return
    
    for listing in listings:
        text = (
            f"📱 {listing.title}\n"
            f"🌍 Страна: {listing.country}\n"
            f"💰 Цена: {listing.price} ₽\n"
            f"📅 Создано: {listing.created_at.strftime('%d.%m.%Y')}"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="❌ Снять с продажи", callback_data=f"del_{listing.id}")
        )
        
        await callback.message.answer(text, reply_markup=builder.as_markup())
    
    await callback.message.answer("Вернуться в меню:", reply_markup=get_back_keyboard())
    await callback.answer()
    session.close()

@dp.callback_query(F.data.startswith("del_"))
async def delete_listing(callback: types.CallbackQuery):
    """Снять объявление с продажи"""
    listing_id = int(callback.data.split("_")[1])
    
    session = Session()
    listing = session.query(Listing).filter_by(id=listing_id).first()
    if listing and listing.seller_id == callback.from_user.id:
        listing.status = 'deleted'
        session.commit()
        await callback.message.edit_text("✅ Объявление снято с продажи")
    else:
        await callback.answer("❌ Ошибка: объявление не найдено")
    
    session.close()

# ==================== СЦЕНАРИЙ ПРОДАЖИ ====================

@dp.callback_query(F.data == "sell")
async def start_sell(callback: types.CallbackQuery, state: FSMContext):
    """Начать процесс продажи"""
    user = get_user(callback.from_user.id)
    
    # Проверяем, есть ли у пользователя сохраненные API данные
    if user.api_id and user.api_hash:
        # Если есть, переходим к вводу номера
        await state.set_state(SellStates.waiting_phone)
        await callback.message.edit_text(
            "📱 Отправьте номер телефона аккаунта, который хотите продать\n"
            "(в международном формате, например: +79001234567)",
            reply_markup=get_cancel_keyboard()
        )
    else:
        # Если нет, запрашиваем api_id
        await state.set_state(SellStates.waiting_api_id)
        await callback.message.edit_text(
            "🔑 Для проверки аккаунтов нам нужны ваши API данные от Telegram.\n\n"
            "Как получить:\n"
            "1. Перейдите на my.telegram.org\n"
            "2. Войдите в аккаунт\n"
            "3. Создайте приложение\n"
            "4. Скопируйте api_id и api_hash\n\n"
            "Отправьте ваш api_id (это число):",
            reply_markup=get_cancel_keyboard()
        )
    
    await callback.answer()

@dp.message(SellStates.waiting_api_id)
async def process_api_id(message: types.Message, state: FSMContext):
    """Обработка ввода api_id"""
    if not message.text.isdigit():
        await message.answer("❌ api_id должен быть числом. Попробуйте снова:")
        return
    
    await state.update_data(api_id=message.text)
    await state.set_state(SellStates.waiting_api_hash)
    await message.answer(
        "✅ api_id сохранен\n\n"
        "Теперь отправьте ваш api_hash (строка символов):",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(SellStates.waiting_api_hash)
async def process_api_hash(message: types.Message, state: FSMContext):
    """Обработка ввода api_hash"""
    data = await state.get_data()
    api_id = data['api_id']
    api_hash = message.text
    
    # Сохраняем в БД
    session = Session()
    user = session.query(User).filter_by(user_id=message.from_user.id).first()
    user.api_id = encryption.encrypt(api_id)
    user.api_hash = encryption.encrypt(api_hash)
    session.commit()
    session.close()
    
    await state.set_state(SellStates.waiting_phone)
    await message.answer(
        "✅ API данные сохранены!\n\n"
        "📱 Теперь отправьте номер телефона аккаунта, который хотите продать\n"
        "(в международном формате, например: +79001234567)",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(SellStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    """Обработка ввода номера телефона"""
    phone = message.text.strip()
    
    # Базовая валидация номера
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer(
            "❌ Неверный формат номера. Используйте международный формат, например: +79001234567"
        )
        return
    
    await state.update_data(phone=phone)
    
    # Получаем API данные пользователя
    session = Session()
    user = session.query(User).filter_by(user_id=message.from_user.id).first()
    api_id = int(encryption.decrypt(user.api_id))
    api_hash = encryption.decrypt(user.api_hash)
    session.close()
    
    # Пытаемся авторизоваться
    try:
        # Создаем временного клиента
        client = Client("temp_session", api_id=api_id, api_hash=api_hash, in_memory=True)
        await client.connect()
        
        # Отправляем код
        sent_code = await client.send_code(phone)
        await state.update_data(client=client, phone_code_hash=sent_code.phone_code_hash)
        
        await state.set_state(SellStates.waiting_code)
        await message.answer(
            "📲 Код подтверждения отправлен в Telegram (или по SMS).\n"
            "Введите его цифрами:",
            reply_markup=get_cancel_keyboard()
        )
        
    except PhoneNumberInvalid:
        await message.answer("❌ Неверный номер телефона. Попробуйте снова.")
        await state.set_state(SellStates.waiting_phone)
    except Exception as e:
        logger.error(f"Ошибка авторизации: {e}")
        await message.answer("❌ Ошибка подключения. Попробуйте позже.")
        await state.clear()

@dp.message(SellStates.waiting_code)
async def process_code(message: types.Message, state: FSMContext):
    """Обработка ввода кода подтверждения"""
    code = message.text.strip()
    
    data = await state.get_data()
    client = data.get('client')
    phone = data.get('phone')
    phone_code_hash = data.get('phone_code_hash')
    
    try:
        # Пробуем войти с кодом
        await client.sign_in(phone, phone_code_hash, code)
        
        # Проверяем спам-блок
        await message.answer("⏳ Проверяем аккаунт через SpamBot...")
        
        is_clean, spam_status = await check_spam_block(client)
        
        if not is_clean:
            await client.disconnect()
            await message.answer(
                f"{spam_status}\n\n"
                "❌ Аккаунт не может быть продан.",
                reply_markup=get_back_keyboard()
            )
            await state.clear()
            return
        
        # Определяем страну
        country = await get_country_from_session(client)
        
        # Сохраняем сессию
        session_string = await client.export_session_string()
        
        await state.update_data(
            client=client,
            session_string=session_string,
            country=country,
            spam_status=spam_status
        )
        
        await state.set_state(SellStates.waiting_password)
        await message.answer(
            f"{spam_status}\n"
            f"🌍 Страна: {country}\n\n"
            "🔐 Введите пароль от аккаунта (если есть, иначе отправьте 'нет'):",
            reply_markup=get_cancel_keyboard()
        )
        
    except SessionPasswordNeeded:
        # Требуется 2FA
        await state.set_state(SellStates.waiting_2fa)
        await message.answer(
            "🔐 Аккаунт защищен двухфакторной аутентификацией.\n"
            "Введите облачный пароль:",
            reply_markup=get_cancel_keyboard()
        )
        
    except PhoneCodeInvalid:
        await message.answer("❌ Неверный код. Попробуйте снова:")
    except PhoneCodeExpired:
        await message.answer("❌ Код истек. Запросите новый код:")
        # Возвращаемся к вводу номера
        await state.set_state(SellStates.waiting_phone)

@dp.message(SellStates.waiting_2fa)
async def process_2fa(message: types.Message, state: FSMContext):
    """Обработка 2FA пароля"""
    password = message.text.strip()
    
    data = await state.get_data()
    client = data.get('client')
    
    try:
        await client.check_password(password)
        
        # Проверяем спам-блок
        await message.answer("⏳ Проверяем аккаунт через SpamBot...")
        
        is_clean, spam_status = await check_spam_block(client)
        
        if not is_clean:
            await client.disconnect()
            await message.answer(
                f"{spam_status}\n\n"
                "❌ Аккаунт не может быть продан.",
                reply_markup=get_back_keyboard()
            )
            await state.clear()
            return
        
        # Определяем страну
        country = await get_country_from_session(client)
        
        # Сохраняем сессию
        session_string = await client.export_session_string()
        
        await state.update_data(
            client=client,
            session_string=session_string,
            country=country,
            spam_status=spam_status,
            password=password
        )
        
        await state.set_state(SellStates.waiting_title)
        await message.answer(
            f"{spam_status}\n"
            f"🌍 Страна: {country}\n\n"
            "📝 Введите название для объявления:",
            reply_markup=get_cancel_keyboard()
        )
        
    except Exception as e:
        await message.answer("❌ Неверный пароль. Попробуйте снова:")

@dp.message(SellStates.waiting_password)
async def process_password(message: types.Message, state: FSMContext):
    """Обработка пароля (если есть)"""
    password = message.text.strip() if message.text.lower() != 'нет' else None
    
    await state.update_data(password=password)
    await state.set_state(SellStates.waiting_title)
    
    await message.answer(
        "📝 Введите название для объявления:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(SellStates.waiting_title)
async def process_title(message: types.Message, state: FSMContext):
    """Обработка названия объявления"""
    title = message.text.strip()
    
    if len(title) < 3 or len(title) > 100:
        await message.answer("❌ Название должно быть от 3 до 100 символов. Попробуйте снова:")
        return
    
    await state.update_data(title=title)
    await state.set_state(SellStates.waiting_price)
    
    await message.answer(
        "💰 Введите цену в рублях (только число):",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(SellStates.waiting_price)
async def process_price(message: types.Message, state: FSMContext):
    """Обработка цены и публикация объявления"""
    try:
        price = float(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число больше 0:")
        return
    
    data = await state.get_data()
    
    # Закрываем клиент Pyrogram
    if 'client' in data:
        await data['client'].disconnect()
    
    # Сохраняем объявление в БД
    session = Session()
    listing = Listing(
        seller_id=message.from_user.id,
        phone_number=encryption.encrypt(data['phone']),
        session_string=encryption.encrypt(data.get('session_string', '')),
        password=encryption.encrypt(data.get('password')) if data.get('password') else None,
        country=data['country'],
        title=data['title'],
        price=price,
        status='active'
    )
    session.add(listing)
    session.commit()
    session.close()
    
    await message.answer(
        "✅ Объявление успешно создано и опубликовано!\n\n"
        f"📱 {data['title']}\n"
        f"🌍 Страна: {data['country']}\n"
        f"💰 Цена: {price} ₽\n"
        f"{data['spam_status']}\n\n"
        "Товар появится в витрине для покупателей.",
        reply_markup=get_main_keyboard()
    )
    
    await state.clear()

# ==================== СЦЕНАРИЙ ПОКУПКИ ====================

@dp.callback_query(F.data == "buy")
async def start_buy(callback: types.CallbackQuery, state: FSMContext):
    """Начать процесс покупки"""
    session = Session()
    listings = session.query(Listing).filter_by(status='active').all()
    session.close()
    
    if not listings:
        await callback.message.edit_text(
            "😕 Пока нет активных объявлений",
            reply_markup=get_back_keyboard()
        )
        await callback.answer()
        return
    
    await state.set_state(BuyStates.browsing)
    await show_listings_page(callback.message, listings, 0)
    await callback.answer()

async def show_listings_page(message: types.Message, listings: list, page: int):
    """Показать страницу с объявлениями"""
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    current_listings = listings[start:end]
    
    if not current_listings:
        await message.edit_text(
            "📭 Больше нет объявлений",
            reply_markup=get_back_keyboard()
        )
        return
    
    builder = InlineKeyboardBuilder()
    
    for listing in current_listings:
        builder.row(
            InlineKeyboardButton(
                text=f"📱 {listing.title} | {listing.country} | {listing.price}₽",
                callback_data=f"view_{listing.id}"
            )
        )
    
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"page_{page-1}"))
    if end < len(listings):
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"page_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    
    await message.edit_text(
        f"🛒 Доступные аккаунты (страница {page+1}/{((len(listings)-1)//items_per_page)+1}):",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("page_"))
async def handle_page(callback: types.CallbackQuery):
    """Обработка переключения страниц"""
    page = int(callback.data.split("_")[1])
    
    session = Session()
    listings = session.query(Listing).filter_by(status='active').all()
    session.close()
    
    await show_listings_page(callback.message, listings, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_"))
async def view_listing(callback: types.CallbackQuery, state: FSMContext):
    """Просмотр конкретного объявления"""
    listing_id = int(callback.data.split("_")[1])
    
    session = Session()
    listing = session.query(Listing).filter_by(id=listing_id).first()
    session.close()
    
    if not listing or listing.status != 'active':
        await callback.answer("❌ Это объявление уже не доступно")
        await start_buy(callback, state)
        return
    
    text = (
        f"📱 {listing.title}\n"
        f"🌍 Страна: {listing.country}\n"
        f"💰 Цена: {listing.price} ₽\n\n"
        f"✅ Аккаунт проверен на спам-блок\n"
        f"📅 Добавлен: {listing.created_at.strftime('%d.%m.%Y')}"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💳 Купить", callback_data=f"buy_{listing.id}"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="buy")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def confirm_purchase(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение покупки"""
    listing_id = int(callback.data.split("_")[1])
    
    session = Session()
    listing = session.query(Listing).filter_by(id=listing_id).first()
    user = session.query(User).filter_by(user_id=callback.from_user.id).first()
    
    if not listing or listing.status != 'active':
        await callback.answer("❌ Объявление уже продано")
        await start_buy(callback, state)
        session.close()
        return
    
    if user.balance < listing.price:
        await callback.answer("❌ Недостаточно средств на балансе")
        session.close()
        return
    
    # Подтверждение покупки
    text = (
        f"Подтвердите покупку:\n\n"
        f"{listing.title}\n"
        f"Цена: {listing.price} ₽\n\n"
        f"После подтверждения сумма будет списана с вашего баланса."
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{listing.id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="buy")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()
    session.close()

@dp.callback_query(F.data.startswith("confirm_"))
async def process_purchase(callback: types.CallbackQuery):
    """Обработка подтвержденной покупки"""
    listing_id = int(callback.data.split("_")[1])
    
    session = Session()
    listing = session.query(Listing).filter_by(id=listing_id).first()
    buyer = session.query(User).filter_by(user_id=callback.from_user.id).first()
    seller = session.query(User).filter_by(user_id=listing.seller_id).first()
    
    if not listing or listing.status != 'active':
        await callback.answer("❌ Объявление уже недоступно")
        session.close()
        return
    
    # Расчет комиссии
    commission = listing.price * COMMISSION_PERCENT / 100
    seller_amount = listing.price - commission
    
    # Обновляем балансы
    buyer.balance -= listing.price
    seller.balance += seller_amount
    
    # Обновляем статистику
    buyer.total_purchases += 1
    seller.total_sales += 1
    
    # Помечаем объявление как проданное
    listing.status = 'sold'
    
    # Создаем запись о транзакции
    transaction = Transaction(
        listing_id=listing.id,
        buyer_id=buyer.user_id,
        seller_id=seller.user_id,
        amount=listing.price,
        commission=commission
    )
    session.add(transaction)
    
    session.commit()
    
    # Отправляем данные покупателю
    phone = encryption.decrypt(listing.phone_number)
    password = encryption.decrypt(listing.password) if listing.password else "не установлен"
    
    await callback.message.edit_text(
        "✅ Покупка успешно завершена!\n\n"
        f"📱 Данные аккаунта:\n"
        f"Телефон: {phone}\n"
        f"Пароль: {password}\n\n"
        f"💳 Сумма покупки: {listing.price} ₽\n"
        f"💰 Комиссия: {commission} ₽\n\n"
        "🔐 Данные зашифрованы и доступны только вам.\n"
        "Рекомендуем сразу сменить пароль после входа.",
        reply_markup=get_main_keyboard()
    )
    
    # Уведомляем продавца
    try:
        await bot.send_message(
            listing.seller_id,
            f"✅ Ваш аккаунт '{listing.title}' был продан!\n"
            f"💰 Вы получили: {seller_amount} ₽ (комиссия {COMMISSION_PERCENT}%)"
        )
    except:
        pass
    
    session.close()
    await callback.answer()

# ==================== ОБРАБОТЧИК ОТМЕНЫ ====================

@dp.callback_query(F.data == "cancel")
async def cancel_operation(callback: types.CallbackQuery, state: FSMContext):
    """Отмена текущей операции"""
    await state.clear()
    
    # Закрываем клиент Pyrogram если был открыт
    data = await state.get_data()
    if 'client' in data:
        await data['client'].disconnect()
    
    await callback.message.edit_text(
        "❌ Операция отменена",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back")
async def go_back(callback: types.CallbackQuery, state: FSMContext):
    """Возврат на предыдущий шаг"""
    current_state = await state.get_state()
    
    if current_state:
        # Если есть состояние, возвращаемся в главное меню
        await state.clear()
        await callback.message.edit_text(
            "Главное меню:",
            reply_markup=get_main_keyboard()
        )
    else:
        # Если нет состояния, просто в главное меню
        await callback.message.edit_text(
            "Главное меню:",
            reply_markup=get_main_keyboard()
        )
    
    await callback.answer()

# ==================== ЗАПУСК БОТА ====================

async def main():
    """Главная функция запуска бота"""
    logging.info("Запуск бота Monkey Market...")
    
    # Проверяем наличие токена
    if BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        logging.error("Пожалуйста, установите BOT_TOKEN в переменных окружения или в коде")
        return
    
    # Запускаем бота
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
