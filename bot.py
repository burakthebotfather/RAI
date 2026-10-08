import os
import re
import json
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    ContextTypes, ApplicationHandlerStop, filters
)
from telegram.error import TelegramError

import config
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== НОВЫЕ НАСТРОЙКИ ==================

TZ = ZoneInfo("Europe/Minsk")
# Пользователь с уникальным ID (администратор доставки). Можно вынести в config.py
UNIQUE_USER_ID = int(getattr(config, "UNIQUE_USER_ID", os.getenv("UNIQUE_USER_ID", 542345855)))
ERRORS_FILE = os.getenv("ERRORS_FILE", "phone_errors.json")
MIN_REQUEST_LEN = 50  # короче — не считаем заявкой


def get_display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name


def rating_keyboard(user_id: int, vote_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f"{i}⭐", callback_data=f"vote:{vote_id}:{user_id}:{i}")
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup([buttons])


# ================== СЧЁТЧИК ОШИБОК С НОМЕРОМ ==================
# user_id -> {"month": "YYYY-MM", "count": int}
# При наступлении нового месяца (1-го числа) значение автоматически начинается с нуля.

def _load_errors() -> dict:
    try:
        with open(ERRORS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


phone_errors: dict = _load_errors()


def register_phone_error(user_id: int) -> int:
    month = datetime.now(TZ).strftime("%Y-%m")
    rec = phone_errors.get(str(user_id))
    if not rec or rec.get("month") != month:
        rec = {"month": month, "count": 0}
    rec["count"] += 1
    phone_errors[str(user_id)] = rec
    try:
        with open(ERRORS_FILE, "w", encoding="utf-8") as f:
            json.dump(phone_errors, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Не удалось сохранить счётчик ошибок: %s", e)
    return rec["count"]


def user_mention(user) -> str:
    if user.username:
        return f"@{user.username}"
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name or "пользователь")}</a>'


# ================== ТЕЛЕФОН: 80XX... -> +375XX... ==================

_SEP = r"[\s\-\(\)]*"
LOCAL_80_RE = re.compile(
    rf"(?<!\d)8{_SEP}0{_SEP}(25|29|33|44)((?:{_SEP}\d){{7}})(?!\d)"
)


def normalize_by_phones(text: str) -> tuple[str, bool]:
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        changed = True
        tail = re.sub(r"\D", "", m.group(2))
        return f"+375{m.group(1)}{tail}"

    return LOCAL_80_RE.sub(repl, text), changed


# ================== ВРЕМЯ: интервал < 30 минут / точное время ==================

TIME_RANGE_RE = re.compile(
    r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:-|–|—|до)\s*([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)"
)
# «ровно в 15:00», «точно к 14:30», «строго к 12:00», «к 15:00»
EXACT_TIME_RE = re.compile(
    r"(?:(?:ровно|точно|строго)\s*(?:в|к)?|к)\s*([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)",
    re.IGNORECASE,
)


def has_time_violation(text: str) -> bool:
    for m in TIME_RANGE_RE.finditer(text):
        h1, m1, h2, m2 = map(int, m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end < start:
            end += 24 * 60
        if end - start < 30:
            return True
    # точное время считаем нарушением только если в заявке нет нормального интервала
    if EXACT_TIME_RE.search(text) and not TIME_RANGE_RE.search(text):
        return True
    return False


# ================== ОБРАБОТКА ЗАЯВОК (телефон + время) ==================

async def handle_order_checks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat.id
    chat_cfg = config.CHATS.get(chat_id)
    if not chat_cfg or message.message_thread_id != chat_cfg["thread_a"]:
        return

    user = message.from_user
    if user.id == UNIQUE_USER_ID or len(message.text) < MIN_REQUEST_LEN:
        return

    raw_text = message.text
    fixed_text, phone_fixed = normalize_by_phones(raw_text)

    link = ""
    try:
        link = f"\nСсылка на заявку в чате: {message.link}"
    except Exception:
        pass
    header = f"{chat_cfg['name']}{link}\n\n"

    # --- (2) Интервал < 30 минут / точное время: автоотклонение ---
    if has_time_violation(raw_text):
        await message.reply_text(
            "Заказ не принят в работу! Возможность приема заказов с интервалом доставки "
            "менее 30 минут заблокирована, если иное не было согласовано заранее. "
            "Пожалуйста, обратитесь к администратору."
        )
        try:
            await context.bot.send_message(
                UNIQUE_USER_ID,
                header + "Отклонена автоматически: нарушение п. 2.3 договора\n\n" + fixed_text,
                parse_mode=None,
            )
        except TelegramError as e:
            logger.error("Ошибка отправки отклонённой заявки: %s", e)
        # не даём сработать handle_plus (опрос рейтинга) для отклонённой заявки
        raise ApplicationHandlerStop

    # --- (1) Номер в формате 80XX...: исправляем ---
    if phone_fixed:
        count = register_phone_error(user.id)
        await message.reply_text(
            "Номер в заявке указан в неверном формате, уже исправлен системой и "
            "направлен администратору доставки в корректном виде.\n\n"
            f"{user_mention(user)}, Вы ошибались {count} раз(а) в этом месяце!",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                UNIQUE_USER_ID, header + fixed_text, parse_mode=None
            )
        except TelegramError as e:
            logger.error("Ошибка отправки заявки администратору: %s", e)


async def handle_plus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    if "+" not in message.text:
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    user = message.from_user
    user_id = user.id
    display_name = get_display_name(user)

    chat_cfg = config.CHATS.get(chat_id)
    if not chat_cfg:
        return

    if thread_id != chat_cfg["thread_a"]:
        return

    now = datetime.utcnow()
    last = db.get_last_plus(user_id, chat_id)
    if last and (now - last) < timedelta(hours=config.COOLDOWN_HOURS):
        return

    db.log_plus(user_id, chat_id)
    current_rating = db.get_rating(user_id)

    thread_b = chat_cfg["thread_b"]
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_b,
            text=(
                f"✅ Можно оценить качество работы {display_name} (id: {user_id}).\n"
                f"Текущий рейтинг: {current_rating:.2f}⭐\n"
                f"Опрос направлен в личные сообщения администраторам."
            )
        )
    except TelegramError as e:
        logger.error("Ошибка отправки в thread_b: %s", e)

    delete_at = now + timedelta(hours=config.DELETE_AFTER_HOURS)

    for admin_id in (chat_cfg["admin1"], chat_cfg["admin2"]):
        try:
            sent = await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📋 Оцените работу водителя {display_name} ({user_id})\n"
                    f"Организация: {chat_cfg['name']}\n"
                    f"Текущий рейтинг водителя: {current_rating:.2f}⭐"
                ),
                reply_markup=rating_keyboard(user_id, 0)
            )
            vote_id = db.save_pending_vote(user_id, chat_id, admin_id, sent.message_id, display_name)
            await context.bot.edit_message_reply_markup(
                chat_id=admin_id,
                message_id=sent.message_id,
                reply_markup=rating_keyboard(user_id, vote_id)
            )
            db.schedule_delete(admin_id, sent.message_id, delete_at)
        except TelegramError as e:
            logger.error("Ошибка отправки опроса администратору %s: %s", admin_id, e)


async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, vote_id_str, user_id_str, score_str = query.data.split(":")
        vote_id = int(vote_id_str)
        user_id = int(user_id_str)
        score = int(score_str)
    except (ValueError, AttributeError):
        return

    vote = db.get_pending_vote(vote_id)
    if not vote:
        await query.message.reply_text("⚠️ Голос не найден.")
        return

    if vote["voted"]:
        await query.message.reply_text("ℹ️ Вы уже проголосовали по этому опросу.")
        return

    new_rating = db.add_rating(user_id, score)
    db.mark_voted(vote_id)
    display_name = vote["display_name"]

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    try:
        confirm = await query.message.reply_text(
            f"✅ Спасибо, Ваша оценка качества работы водителя {display_name} ({user_id}) "
            f"принята и отразится на рейтинге водителя!\n"
            f"Новый рейтинг: {new_rating:.2f}⭐"
        )
        delete_at = datetime.utcnow() + timedelta(hours=config.DELETE_AFTER_HOURS)
        db.schedule_delete(query.message.chat_id, confirm.message_id, delete_at)
    except TelegramError as e:
        logger.error("Ошибка отправки подтверждения: %s", e)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.utcnow()
    rows = db.get_due_deletes(now)
    for row in rows:
        try:
            await context.bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
        except TelegramError as e:
            logger.warning("Не удалось удалить сообщение %s в чате %s: %s", row["message_id"], row["chat_id"], e)
        finally:
            db.remove_scheduled_delete(row["id"])


def main() -> None:
    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    db.init_db()

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    # group=-1: проверки заявки выполняются ДО handle_plus и могут его остановить
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order_checks), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plus))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote:"))

    app.job_queue.run_repeating(cleanup_job, interval=60, first=10)

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
