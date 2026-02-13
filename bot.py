import asyncio
import logging
import sqlite3
import re
from datetime import datetime
from typing import Optional, Dict
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from pyrogram import Client, enums
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
            
            # Таблица выданных аккаунтов
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
    
    def get_random_account(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM accounts 
                WHERE is_active = 1 
                AND id NOT IN (
                    SELECT account_id FROM user_accounts WHERE is_active = 1
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

# Инициализация базы данных
db = Database()

# Словарь для хранения временных данных авторизации
temp_auth_data: Dict[str, dict] = {}

# Улучшенные клавиатуры с эмодзи и стилями
def get_start_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="🔥 ПОЛУЧИТЬ АККАУНТ",
        callback_data="get_account"
    ))
    return builder.as_markup()

def get_code_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="📱 ПОЛУЧИТЬ КОД",
        callback_data="get_code"
    ))
    return builder.as_markup()

def get_back_to_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="🔙 ВЕРНУТЬСЯ В МЕНЮ",
        callback_data="back_to_menu"
    ))
    return builder.as_markup()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="admin_add"),
        InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="admin_stats"),
        InlineKeyboardButton(text="🔴 ДЕАКТИВИРОВАТЬ", callback_data="admin_deactivate"),
        InlineKeyboardButton(text="📋 СПИСОК АККАУНТОВ", callback_data="admin_list")
    )
    builder.adjust(1)
    return builder.as_markup()

# Обработчики пользовательских команд
@dp.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = """
🌟 <b>Добро пожаловать в Telegram Account Bot!</b> 🌟

Здесь вы можете получить временный аккаунт Telegram для своих нужд.

📌 <b>Как это работает:</b>
1️⃣ Нажмите кнопку "ПОЛУЧИТЬ АККАУНТ"
2️⃣ Вам будет выдан номер телефона
3️⃣ Нажмите "ПОЛУЧИТЬ КОД" для получения кода подтверждения
4️⃣ Используйте полученные данные для входа

⚠️ <b>Важно:</b> Аккаунт выдается одному пользователю один раз!
    """
    
    await message.answer(
        welcome_text,
        reply_markup=get_start_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "get_account")
async def process_get_account(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or "пользователь"
    
    # Проверяем, есть ли уже аккаунт у пользователя
    existing_account = db.get_user_account(user_id)
    if existing_account:
        await callback.message.edit_text(
            f"⚠️ <b>У вас уже есть активный аккаунт!</b>\n\n"
            f"📱 <b>Номер:</b> <code>{existing_account['phone']}</code>\n"
            f"📅 <b>Выдан:</b> {existing_account['last_used'] or 'только что'}\n\n"
            f"Нажмите кнопку ниже, чтобы получить код подтверждения:",
            reply_markup=get_code_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Получаем случайный свободный аккаунт
    account = db.get_random_account()
    if not account:
        await callback.message.edit_text(
            "😔 <b>К сожалению, свободных аккаунтов нет</b>\n\n"
            "Попробуйте позже или обратитесь к администратору.",
            reply_markup=get_back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Выдаем аккаунт пользователю
    db.assign_account_to_user(user_id, account['id'])
    
    success_text = f"""
✅ <b>Аккаунт успешно выдан!</b>

📱 <b>Номер телефона:</b>
<code>{account['phone']}</code>

🔑 <b>Дальнейшие действия:</b>
1. Нажмите кнопку "ПОЛУЧИТЬ КОД"
2. Дождитесь получения кода
3. Используйте номер + код для входа

⏱ <b>Важно:</b> Код необходимо запросить в течение 5 минут!
    """
    
    await callback.message.edit_text(
        success_text,
        reply_markup=get_code_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "get_code")
async def process_get_code(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Отправляем сообщение о начале поиска
    loading_msg = await callback.message.edit_text(
        "🔍 <b>Поиск кода подтверждения...</b>\n\n"
        "Пожалуйста, подождите несколько секунд.",
        parse_mode="HTML"
    )
    
    # Получаем аккаунт пользователя
    account = db.get_user_account(user_id)
    if not account:
        await loading_msg.edit_text(
            "❌ <b>У вас нет активного аккаунта</b>\n\n"
            "Сначала получите аккаунт через главное меню.",
            reply_markup=get_start_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    try:
        # Создаем клиент Pyrogram для аккаунта
        client = Client(
            f"session_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=account['session_string'],
            in_memory=True
        )
        
        await client.start()
        logger.info(f"Client started for account {account['phone']}")
        
        try:
            # Получаем все диалоги
            dialogs = []
            async for dialog in client.get_dialogs():
                dialogs.append(dialog)
                logger.info(f"Dialog found: {dialog.chat.id} - {dialog.chat.title or dialog.chat.first_name or 'No name'}")
            
            if not dialogs:
                await loading_msg.edit_text(
                    "❌ <b>Нет доступных чатов</b>\n\n"
                    "В аккаунте отсутствуют какие-либо диалоги.",
                    reply_markup=get_back_to_menu_keyboard(),
                    parse_mode="HTML"
                )
                return
            
            # Ищем закрепленный чат (первый в списке или помеченный как закрепленный)
            pinned_chat = None
            
            # Сначала ищем среди закрепленных
            for dialog in dialogs:
                if dialog.chat.is_pinned:  # Проверяем, закреплен ли чат
                    pinned_chat = dialog.chat
                    logger.info(f"Found pinned chat: {pinned_chat.id}")
                    break
            
            # Если нет закрепленных, берем первый чат (обычно это "Избранное" или последний активный)
            if not pinned_chat and dialogs:
                pinned_chat = dialogs[0].chat
                logger.info(f"No pinned chat, using first dialog: {pinned_chat.id}")
            
            if pinned_chat:
                logger.info(f"Selected chat: {pinned_chat.id}")
                
                # Получаем последние сообщения из чата
                messages = []
                async for msg in client.get_chat_history(pinned_chat.id, limit=10):
                    messages.append(msg)
                    if msg.text:
                        logger.info(f"Message text: {msg.text[:100]}...")
                
                if messages:
                    # Ищем 5-значный код в сообщениях
                    found_code = None
                    
                    for msg in messages:
                        if msg.text:
                            # Ищем ровно 5 цифр подряд
                            code_match = re.search(r'\b(\d{5})\b', msg.text)
                            if code_match:
                                found_code = code_match.group(1)
                                logger.info(f"Found 5-digit code: {found_code}")
                                break
                            
                            # Также ищем код после слова "код" или "code"
                            code_match = re.search(r'[КкKk][оОoO][дДdD][:\s]*(\d{5})', msg.text)
                            if code_match:
                                found_code = code_match.group(1)
                                logger.info(f"Found code after keyword: {found_code}")
                                break
                    
                    if found_code:
                        success_text = f"""
✅ <b>Код подтверждения найден!</b>

🔑 <b>Ваш код:</b> <code>{found_code}</code>

📝 <b>Инструкция:</b>
1. Скопируйте код
2. Введите его в приложении Telegram
3. Готово! Вы вошли в аккаунт

⚠️ <b>Важно:</b> Код действителен в течение нескольких минут!
                        """
                        
                        await loading_msg.edit_text(
                            success_text,
                            reply_markup=get_back_to_menu_keyboard(),
                            parse_mode="HTML"
                        )
                    else:
                        await loading_msg.edit_text(
                            "❌ <b>Не удалось найти код подтверждения</b>\n\n"
                            "В последних сообщениях нет 5-значного кода.\n\n"
                            "💡 <b>Совет:</b> Запросите код заново в приложении Telegram "
                            "и нажмите кнопку еще раз.",
                            reply_markup=get_code_keyboard(),
                            parse_mode="HTML"
                        )
                else:
                    await loading_msg.edit_text(
                        "❌ <b>Нет сообщений в чате</b>\n\n"
                        "В выбранном чате отсутствуют сообщения.",
                        reply_markup=get_back_to_menu_keyboard(),
                        parse_mode="HTML"
                    )
            else:
                await loading_msg.edit_text(
                    "❌ <b>Не найден подходящий чат</b>\n\n"
                    "Убедитесь, что в аккаунте есть активные диалоги.",
                    reply_markup=get_back_to_menu_keyboard(),
                    parse_mode="HTML"
                )
        
        finally:
            await client.stop()
            logger.info(f"Client stopped for account {account['phone']}")
    
    except Exception as e:
        logger.error(f"Error getting code: {e}")
        await loading_msg.edit_text(
            f"❌ <b>Произошла ошибка</b>\n\n"
            f"Детали: {str(e)[:100]}...\n\n"
            f"Попробуйте позже или обратитесь к администратору.",
            reply_markup=get_back_to_menu_keyboard(),
            parse_mode="HTML"
        )

# Обработчики админ-панели
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ <b>Доступ запрещен</b>\n\nУ вас нет прав администратора.", parse_mode="HTML")
        return
    
    admin_text = """
🔧 <b>Панель администратора</b>

Выберите действие:
• <b>➕ Добавить аккаунт</b> - добавить новый аккаунт
• <b>📊 Статистика</b> - просмотр статистики
• <b>🔴 Деактивировать</b> - деактивировать аккаунт
• <b>📋 Список аккаунтов</b> - просмотр всех аккаунтов
    """
    
    await message.answer(
        admin_text,
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    stats = db.get_statistics()
    
    stats_text = f"""
📊 <b>Статистика системы</b>

📱 <b>Всего аккаунтов:</b> {stats['total']}
✅ <b>Активных:</b> {stats['active']}
👥 <b>Выдано пользователям:</b> {stats['issued']}
🆓 <b>Свободно:</b> {stats['available']}

📈 <b>Процент использования:</b> 
{(stats['issued']/stats['active']*100) if stats['active'] > 0 else 0:.1f}%
    """
    
    await callback.message.edit_text(
        stats_text,
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
            SELECT a.*, COUNT(ua.user_id) as users_count 
            FROM accounts a
            LEFT JOIN user_accounts ua ON a.id = ua.account_id AND ua.is_active = 1
            GROUP BY a.id
            ORDER BY a.added_date DESC
            LIMIT 20
        ''')
        accounts = cursor.fetchall()
    
    if not accounts:
        await callback.message.edit_text(
            "📭 <b>Нет аккаунтов в базе</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    text = "📋 <b>Последние 20 аккаунтов:</b>\n\n"
    for acc in accounts:
        status = "✅" if acc['is_active'] else "❌"
        text += f"{status} <code>{acc['phone']}</code> | Использован: {acc['users_count']} раз | {acc['added_date'][:10]}\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
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
        "или отправьте /cancel для отмены",
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
    
    # Базовая валидация номера
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer(
            "❌ <b>Неверный формат номера</b>\n\n"
            "Используйте формат: <code>+79001234567</code>\n"
            "Попробуйте снова:",
            parse_mode="HTML"
        )
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
        
        # Сохраняем данные
        temp_auth_data[phone] = {
            'client': client,
            'phone_code_hash': sent_code.phone_code_hash
        }
        
        # Сохраняем телефон в состоянии
        await state.update_data(phone=phone)
        
        await message.answer(
            "✅ <b>Код подтверждения отправлен!</b>\n\n"
            "Введите код из Telegram (5 цифр):\n"
            "или отправьте /cancel для отмены",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_code)
        
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await message.answer(
            f"❌ <b>Ошибка:</b> {str(e)[:100]}",
            reply_markup=get_admin_keyboard()
        )
        await state.clear()

@dp.message(AdminStates.waiting_code)
async def process_admin_code(message: Message, state: FSMContext):
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
    
    code = message.text.strip()
    
    # Проверяем, что код состоит из 5 цифр
    if not re.match(r'^\d{5}$', code):
        await message.answer(
            "❌ <b>Неверный формат кода</b>\n\n"
            "Код должен состоять из 5 цифр.\n"
            "Попробуйте снова:",
            parse_mode="HTML"
        )
        return
    
    data = await state.get_data()
    phone = data.get('phone')
    
    # Получаем временные данные
    auth_data = temp_auth_data.get(phone)
    if not auth_data:
        await message.answer(
            "❌ <b>Сессия устарела</b>\n\n"
            "Начните процесс добавления заново.",
            reply_markup=get_admin_keyboard()
        )
        await state.clear()
        return
    
    client = auth_data['client']
    phone_code_hash = auth_data['phone_code_hash']
    
    try:
        # Подтверждаем код
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=code
        )
        
        # Получаем session string
        session_string = await client.export_session_string()
        
        # Сохраняем аккаунт в базу
        db.add_account(phone, session_string)
        
        # Очищаем временные данные
        await client.disconnect()
        del temp_auth_data[phone]
        
        await message.answer(
            f"✅ <b>Аккаунт успешно добавлен!</b>\n\n"
            f"📱 <b>Номер:</b> <code>{phone}</code>\n"
            f"🆔 <b>Аккаунт сохранен в базе</b>",
            parse_mode="HTML"
        )
        
        # Возвращаемся в админ-панель
        await message.answer(
            "🔧 <b>Панель администратора</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error signing in: {e}")
        await message.answer(
            f"❌ <b>Ошибка:</b> {str(e)[:100]}",
            reply_markup=get_admin_keyboard()
        )
    
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
            SELECT a.id, a.phone, ua.user_id, ua.issued_date 
            FROM accounts a
            JOIN user_accounts ua ON a.id = ua.account_id
            WHERE a.is_active = 1 AND ua.is_active = 1
            ORDER BY ua.issued_date DESC
        ''')
        accounts = cursor.fetchall()
    
    if not accounts:
        await callback.message.edit_text(
            "📭 <b>Нет выданных активных аккаунтов</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Создаем клавиатуру с аккаунтами
    builder = InlineKeyboardBuilder()
    for acc in accounts[:10]:  # Показываем только 10 последних
        short_phone = acc['phone'][-8:]  # Показываем последние 8 цифр
        builder.add(InlineKeyboardButton(
            text=f"📱 ...{short_phone} | 👤 {acc['user_id']}",
            callback_data=f"deactivate_{acc['id']}"
        ))
    builder.add(InlineKeyboardButton(text="🔙 НАЗАД", callback_data="admin_back"))
    builder.adjust(1)
    
    await callback.message.edit_text(
        "🔴 <b>Выберите аккаунт для деактивации:</b>\n\n"
        "(показаны последние 10)",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("deactivate_"))
async def process_deactivate(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен")
        return
    
    account_id = int(callback.data.split("_")[1])
    
    # Получаем информацию об аккаунте перед деактивацией
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT phone FROM accounts WHERE id = ?', (account_id,))
        account = cursor.fetchone()
    
    db.deactivate_account(account_id)
    
    await callback.message.edit_text(
        f"✅ <b>Аккаунт деактивирован</b>\n\n"
        f"📱 <b>Номер:</b> <code>{account['phone'] if account else 'Неизвестно'}</code>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

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
    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
