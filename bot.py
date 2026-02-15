import os
import asyncio
import logging
import re
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiocryptopay import AioCryptoPay, Networks
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Токен бота из переменных окружения (указывается в .env файле)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Токен Crypto Pay API прямо в коде (как ты просил)
CRYPTO_PAY_TOKEN = "452163:AAGTBJKe7YvufexfRN78tFhnTdGywQyUMSX"

# Сеть (можно изменить на Networks.TEST_NET для тестирования)
NETWORK = Networks.MAIN_NET

# Проверка наличия токена бота
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения! Создай файл .env с BOT_TOKEN=твой_токен")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# Инициализация Crypto Pay API с токеном из кода
crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN, network=NETWORK)

# Константы
MIN_AMOUNT = Decimal("0.01")
MAX_AMOUNT = Decimal("1000")
SUPPORTED_CURRENCIES = ["USDT", "TON"]

# Классы состояний
class DonateStates(StatesGroup):
    waiting_for_amount = State()

# Клавиатуры
def get_start_keyboard():
    """Главная клавиатура"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Сделать донат", callback_data="donate")]
    ])
    return keyboard

def get_back_keyboard():
    """Клавиатура с кнопкой назад"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
    ])
    return keyboard

def get_payment_keyboard(invoice_id: int, pay_link: str):
    """Клавиатура для оплаты"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить через Crypto Bot", url=pay_link)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_{invoice_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="cancel_payment")]
    ])
    return keyboard

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 *Добро пожаловать в Donate Bot\\!*\n\n"
        "Здесь ты можешь поддержать проект донатом в криптовалюте\\.\n"
        "Принимаем: *USDT* \\(через TON\\) и *TON*\n\n"
        "📊 *Лимиты:* от 0\\.01 до 1000 \\(в обеих валютах\\)\n\n"
        "Нажми кнопку ниже, чтобы сделать донат:"
    )
    
    await message.answer(
        welcome_text,
        parse_mode="MarkdownV2",
        reply_markup=get_start_keyboard()
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Обработчик команды /help"""
    help_text = (
        "📚 *Помощь по боту*\n\n"
        "*Команды:*\n"
        "• /start \\- начать работу\n"
        "• /help \\- эта справка\n\n"
        "*Как сделать донат:*\n"
        "1\\. Нажми кнопку \"💰 Сделать донат\"\n"
        "2\\. Введи сумму и валюту \\(например: `50 USDT` или `5\\.5 TON`\\)\n"
        "3\\. Перейди по ссылке и оплати через Crypto Bot\n"
        "4\\. Нажми \"✅ Я оплатил\" для проверки статуса\n\n"
        "*Лимиты:* от 0\\.01 до 1000 \\(в обеих валютах\\)"
    )
    
    await message.answer(
        help_text,
        parse_mode="MarkdownV2"
    )

# Обработчики callback'ов
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.message.edit_text(
        "👋 *Главное меню*\n\n"
        "Нажми кнопку ниже, чтобы сделать донат:",
        parse_mode="MarkdownV2",
        reply_markup=get_start_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "cancel_payment")
async def cancel_payment(callback: types.CallbackQuery, state: FSMContext):
    """Отмена платежа"""
    await state.clear()
    await callback.message.edit_text(
        "❌ *Платёж отменён*\n\n"
        "Можешь попробовать снова:",
        parse_mode="MarkdownV2",
        reply_markup=get_start_keyboard()
    )
    await callback.answer("Платёж отменён")

@dp.callback_query(F.data == "donate")
async def donate_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало процесса доната"""
    await state.set_state(DonateStates.waiting_for_amount)
    
    await callback.message.edit_text(
        "💸 *Введите сумму и валюту*\n\n"
        "Напиши сумму от 0\\.01 до 1000 и укажи валюту через пробел\\.\n"
        "Примеры:\n"
        "• `50 USDT`\n"
        "• `5\\.5 TON`\n"
        "• `0\\.1 ton` \\(регистр не важен\\)",
        parse_mode="MarkdownV2",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith('check_'))
async def check_payment(callback: types.CallbackQuery, state: FSMContext):
    """Проверка статуса платежа"""
    invoice_id = int(callback.data.split('_')[1])
    
    try:
        # Получаем информацию о счете из Crypto Pay API
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)
        
        if not invoices:
            await callback.answer("❌ Счёт не найден!", show_alert=True)
            return
        
        invoice = invoices[0]
        
        if invoice.status == "paid":
            # Платёж успешен
            await state.clear()
            
            # Получаем данные из состояния
            data = await state.get_data()
            amount = data.get('amount', 'неизвестно')
            currency = data.get('currency', 'неизвестно')
            
            success_text = (
                f"✅ *Платёж успешно получен\\!*\n\n"
                f"Спасибо за твою поддержку\\! 🙏\n\n"
                f"*Детали платежа:*\n"
                f"• Сумма: `{amount}` {currency}\n"
                f"• ID платежа: `{invoice.invoice_id}`\n"
                f"• Время: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )
            
            await callback.message.edit_text(
                success_text,
                parse_mode="MarkdownV2",
                reply_markup=get_start_keyboard()
            )
            
            # Логируем успешный донат
            logger.info(f"Получен донат: {amount} {currency} от пользователя {callback.from_user.id}")
            
        elif invoice.status == "active":
            await callback.answer("⏳ Платёж ещё не выполнен. Оплати счёт и попробуй снова.", show_alert=True)
        else:
            await callback.answer(f"❌ Статус платежа: {invoice.status}", show_alert=True)
            
    except Exception as e:
        logger.error(f"Ошибка при проверке платежа: {e}")
        await callback.answer("❌ Ошибка при проверке платежа", show_alert=True)

# Обработчик сообщений
@dp.message(DonateStates.waiting_for_amount)
async def process_donate_amount(message: types.Message, state: FSMContext):
    """Обработка введенной суммы и создание платежа через Crypto Pay API"""
    
    # Очищаем текст от лишних пробелов и приводим к верхнему регистру
    text = message.text.strip().upper()
    
    # Регулярка для парсинга суммы и валюты (поддерживает точки и запятые)
    pattern = r'^(\d+(?:[.,]\d{1,8})?)\s+(USDT|TON)$'
    match = re.match(pattern, text)
    
    if not match:
        await message.reply(
            "❌ *Неверный формат\\!*\n\n"
            "Пожалуйста, введи сумму и валюту через пробел\\.\n"
            "Например: `50 USDT` или `5\\.5 TON`",
            parse_mode="MarkdownV2"
        )
        return
    
    # Парсим сумму (заменяем запятую на точку)
    amount_str = match.group(1).replace(',', '.')
    currency = match.group(2)
    
    try:
        amount = Decimal(amount_str)
    except:
        await message.reply(
            "❌ *Неверный формат суммы\\!*\n\n"
            "Используй только цифры и точку/запятую\\. Например: `10\\.5`",
            parse_mode="MarkdownV2"
        )
        return
    
    # Проверяем лимиты
    if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
        await message.reply(
            f"❌ *Сумма вне допустимого диапазона\\!*\n\n"
            f"Минимум: `{MIN_AMOUNT}` {currency}\n"
            f"Максимум: `{MAX_AMOUNT}` {currency}",
            parse_mode="MarkdownV2"
        )
        return
    
    # Отправляем сообщение о создании платежа
    processing_msg = await message.reply(
        "🔄 *Создаю платёж\\.\\.\\.*",
        parse_mode="MarkdownV2"
    )
    
    try:
        # Создаем счет в Crypto Pay API
        invoice = await crypto.create_invoice(
            asset=currency,
            amount=float(amount),  # Crypto Pay API принимает float
            description=f"Донат от {message.from_user.full_name}",
            paid_btn_name="callback",
            paid_btn_url="https://t.me/your_bot",  # Замени на ссылку на твоего бота
            payload=f"donate_{message.from_user.id}_{datetime.now().timestamp()}"
        )
        
        # Сохраняем данные в состоянии
        await state.update_data(
            amount=str(amount),
            currency=currency,
            invoice_id=invoice.invoice_id
        )
        
        # Форматируем сумму для вывода (убираем лишние нули)
        amount_display = f"{amount:.8f}".rstrip('0').rstrip('.') if '.' in f"{amount:.8f}" else f"{amount:.8f}"
        
        # Отправляем информацию об оплате
        payment_text = (
            f"🧾 *Счёт создан\\!*\n\n"
            f"*Детали:*\n"
            f"• Сумма: `{amount_display}` {currency}\n"
            f"• Получатель: Crypto Bot\n\n"
            f"Нажми кнопку ниже, чтобы перейти к оплате через @CryptoBot\n\n"
            f"_После оплаты нажми \"✅ Я оплатил\" для проверки_"
        )
        
        await processing_msg.delete()
        await message.answer(
            payment_text,
            parse_mode="MarkdownV2",
            reply_markup=get_payment_keyboard(invoice.invoice_id, invoice.pay_url)
        )
        
    except Exception as e:
        logger.error(f"Ошибка при создании счета: {e}")
        await processing_msg.edit_text(
            "❌ *Ошибка при создании платежа*\n\n"
            "Попробуй позже или выбери другую валюту",
            parse_mode="MarkdownV2",
            reply_markup=get_back_keyboard()
        )

# Запуск бота
async def main():
    """Главная функция запуска бота"""
    logger.info("Бот запущен и готов к работе!")
    
    # Получаем информацию о боте
    me = await bot.get_me()
    logger.info(f"Bot: @{me.username} (ID: {me.id})")
    
    # Проверяем подключение к Crypto Pay API
    try:
        me_crypto = await crypto.get_me()
        logger.info(f"Crypto Pay: {me_crypto.app_name}")
    except Exception as e:
        logger.error(f"Ошибка подключения к Crypto Pay API: {e}")
    
    # Запускаем polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
