import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError

import config
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def rating_keyboard(user_id: int, vote_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f"{i}⭐", callback_data=f"vote:{vote_id}:{user_id}:{i}")
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup([buttons])


async def handle_plus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    logger.info(
        "Сообщение: chat_id=%s thread_id=%s user_id=%s text=%r",
        message.chat.id, message.message_thread_id, message.from_user.id if message.from_user else None, message.text
    )

    if message.text.strip() != "+":
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    user_id = message.from_user.id
    username = message.from_user.full_name

    chat_cfg = config.CHATS.get(chat_id)
    if not chat_cfg:
        return

    if thread_id != chat_cfg["thread_a"]:
        return

    now = datetime.utcnow()
    last = db.get_last_plus(user_id, chat_id)
    if last and (now - last) < timedelta(hours=config.COOLDOWN_HOURS):
        remaining = timedelta(hours=config.COOLDOWN_HOURS) - (now - last)
        hours_left = int(remaining.total_seconds() // 3600) + 1
        try:
            await message.reply_text(
                f"⏳ Отметка уже была засчитана. Следующая будет доступна через ~{hours_left} ч."
            )
        except TelegramError as e:
            logger.warning("Не удалось отправить cooldown-сообщение: %s", e)
        return

    db.log_plus(user_id, chat_id)
    current_rating = db.get_rating(user_id)

    thread_b = chat_cfg["thread_b"]
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_b,
            text=(
                f"✅ Можно оценить качество работы {username} (id: {user_id}).\n"
                f"Текущий рейтинг: {current_rating:.2f}⭐\n"
                f"Опрос направлен в личные сообщения администраторам."
            )
        )
    except TelegramError as e:
        logger.error("Ошибка отправки в thread_b: %s", e)

    delete_at = now + timedelta(hours=config.DELETE_AFTER_HOURS)

    for admin_id in (chat_cfg["admin1"], chat_cfg["admin2"]):
        try:
            vote_id_placeholder = 0
            keyboard = rating_keyboard(user_id, vote_id_placeholder)
            sent = await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📋 Оцените работу водителя {username} (id: {user_id})\n"
                    f"Организация: {chat_cfg['name']}\n"
                    f"Текущий рейтинг водителя: {current_rating:.2f}⭐"
                ),
                reply_markup=keyboard
            )
            vote_id = db.save_pending_vote(user_id, chat_id, admin_id, sent.message_id)

            keyboard = rating_keyboard(user_id, vote_id)
            await context.bot.edit_message_reply_markup(
                chat_id=admin_id,
                message_id=sent.message_id,
                reply_markup=keyboard
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

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    try:
        confirm = await query.message.reply_text(
            f"✅ Спасибо, Ваша оценка принята и отразится на рейтинге водителя!\n"
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

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plus))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote:"))

    app.job_queue.run_repeating(cleanup_job, interval=60, first=10)

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
