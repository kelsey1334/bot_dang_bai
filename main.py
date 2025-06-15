import json
import logging
import pytz
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

# Logging
logging.basicConfig(level=logging.INFO)

DATA_FILE = "data.json"
CATEGORY_IN = ['Lương', 'Bán hàng', 'Thu nợ', 'Được cho']
CATEGORY_OUT = ['Tiền đi lại', 'Ăn uống', 'Mua sắm', 'Y tế', 'Việc riêng', 'Đi chơi']

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Xin chào! Dùng /in hoặc /out + số tiền để ghi nhận chi tiêu/thu nhập.")

async def handle_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Vui lòng nhập đúng định dạng: /in [số tiền]")
    amount = int(context.args[0])
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"in|{cat}|{amount}")] for cat in CATEGORY_IN]
    await update.message.reply_text("Nguồn thu là gì?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Vui lòng nhập đúng định dạng: /out [số tiền]")
    amount = int(context.args[0])
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"out|{cat}|{amount}")] for cat in CATEGORY_OUT]
    await update.message.reply_text("Khoản chi là gì?", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind, category, amount = query.data.split("|")
    user_id = str(query.from_user.id)
    now = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")
    data = load_data()

    data.setdefault(user_id, {}).setdefault(now, {}).setdefault(kind, {}).setdefault(category, 0)
    data[user_id][now][kind][category] += int(amount)

    save_data(data)
    await query.edit_message_text(f"Đã ghi nhận {kind.upper()} {amount} VND vào mục *{category}*", parse_mode="Markdown")

async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    now = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh"))
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    data = load_data()

    for user_id, records in data.items():
        total_in_yesterday = total_out_yesterday = 0
        total_in_month = total_out_month = 0
        detail_in = detail_out = {}

        for date, types in records.items():
            if date.startswith(month):
                for k, v in types.get("in", {}).items():
                    total_in_month += v
                for k, v in types.get("out", {}).items():
                    total_out_month += v

            if date == yesterday:
                for k, v in types.get("in", {}).items():
                    total_in_yesterday += v
                    detail_in[k] = detail_in.get(k, 0) + v
                for k, v in types.get("out", {}).items():
                    total_out_yesterday += v
                    detail_out[k] = detail_out.get(k, 0) + v

        message = f"📊 *Báo cáo chi tiêu hôm qua ({yesterday}):*\n"
        message += f"\n💰 Thu nhập: {total_in_yesterday:,} VND"
        for cat, val in detail_in.items():
            message += f"\n  - {cat}: {val:,} VND"

        message += f"\n\n💸 Chi tiêu: {total_out_yesterday:,} VND"
        for cat, val in detail_out.items():
            message += f"\n  - {cat}: {val:,} VND"

        message += f"\n\n📅 Tổng tháng ({month}):\n  + Thu: {total_in_month:,} VND\n  + Chi: {total_out_month:,} VND"

        try:
            await bot.send_message(chat_id=int(user_id), text=message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Không gửi được báo cáo cho {user_id}: {e}")

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    TOKEN = os.getenv("BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("in", handle_in))
    app.add_handler(CommandHandler("out", handle_out))
    app.add_handler(CallbackQueryHandler(button_handler))

    scheduler = BackgroundScheduler(timezone='Asia/Ho_Chi_Minh')
    scheduler.add_job(send_daily_summary, 'cron', hour=8, minute=0, args=[app.job_queue])
    scheduler.start()

    app.run_polling()
