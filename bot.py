import os
import logging
import asyncio
from datetime import datetime, date
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from PIL import Image
import io
import asyncpg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# Conversation states
(SETUP_WEIGHT, SETUP_HEIGHT, SETUP_AGE, SETUP_GENDER,
 SETUP_ACTIVITY, SETUP_GOAL, MAIN_MENU) = range(7)

ACTIVITY_LEVELS = {
    "🛋 Минимальная": 1.2,
    "🚶 Лёгкая": 1.375,
    "🏃 Средняя": 1.55,
    "💪 Высокая": 1.725,
    "🏋️ Очень высокая": 1.9,
}

GOALS = {
    "📉 Похудеть": 0.85,
    "⚖️ Держать вес": 1.0,
    "📈 Набрать массу": 1.15,
}

ANALYZE_PROMPT = """Ты эксперт по питанию. Посмотри на фото еды и определи КБЖУ.

Ответь ТОЛЬКО в таком формате (без лишних слов):
БЛЮДО: [название]
ПОРЦИЯ: [число]
КАЛОРИИ: [число]
БЕЛКИ: [число]
ЖИРЫ: [число]
УГЛЕВОДЫ: [число]
КОММЕНТАРИЙ: [короткий совет]

Если на фото не еда — напиши: НЕ_ЕДА"""

TEXT_PROMPT = """Ты эксперт по питанию. Пользователь написал что съел: "{food}"

Ответь ТОЛЬКО в таком формате (без лишних слов):
БЛЮДО: [название]
ПОРЦИЯ: [число]
КАЛОРИИ: [число]
БЕЛКИ: [число]
ЖИРЫ: [число]
УГЛЕВОДЫ: [число]
КОММЕНТАРИЙ: [короткий совет]

Если это не еда — напиши: НЕ_ЕДА"""


async def get_db():
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    conn = await get_db()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            weight REAL,
            height REAL,
            age INTEGER,
            gender TEXT,
            activity REAL,
            goal REAL,
            calories_goal INTEGER,
            protein_goal INTEGER,
            fat_goal INTEGER,
            carb_goal INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS food_log (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            meal_name TEXT,
            portion INTEGER,
            calories INTEGER,
            protein REAL,
            fat REAL,
            carbs REAL,
            logged_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS water_log (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            amount INTEGER,
            logged_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.close()


def calculate_goals(weight, height, age, gender, activity, goal):
    if gender == "male":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161

    tdee = bmr * activity
    calories = int(tdee * goal)
    protein = int(weight * 2.0)
    fat = int(calories * 0.25 / 9)
    carbs = int((calories - protein * 4 - fat * 9) / 4)

    return calories, protein, fat, carbs


def parse_nutrition_response(text):
    result = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


async def get_today_totals(user_id):
    conn = await get_db()
    today = date.today()
    rows = await conn.fetch("""
        SELECT SUM(calories), SUM(protein), SUM(fat), SUM(carbs)
        FROM food_log
        WHERE user_id = $1 AND DATE(logged_at) = $2
    """, user_id, today)
    water = await conn.fetchval("""
        SELECT COALESCE(SUM(amount), 0) FROM water_log
        WHERE user_id = $1 AND DATE(logged_at) = $2
    """, user_id, today)
    await conn.close()
    row = rows[0]
    return {
        "calories": int(row[0] or 0),
        "protein": round(row[1] or 0, 1),
        "fat": round(row[2] or 0, 1),
        "carbs": round(row[3] or 0, 1),
        "water": water
    }


def progress_bar(current, total, length=10):
    if total == 0:
        return "░" * length
    filled = int(min(current / total, 1) * length)
    return "█" * filled + "░" * (length - filled)


# ─── SETUP FLOW ───────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = await get_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()

    if user:
        await show_main_menu(update, context)
        return MAIN_MENU

    await update.message.reply_text(
        "👋 Привет! Я твой персональный трекер питания.\n\n"
        "Давай настроим твой профиль — это займёт минуту.\n\n"
        "⚖️ Введи свой вес (в кг):",
        reply_markup=ReplyKeyboardRemove()
    )
    return SETUP_WEIGHT


async def setup_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.replace(",", "."))
        if not 30 <= weight <= 300:
            raise ValueError
        context.user_data["weight"] = weight
        await update.message.reply_text("📏 Введи свой рост (в см):")
        return SETUP_HEIGHT
    except ValueError:
        await update.message.reply_text("❌ Введи корректный вес, например: 70")
        return SETUP_WEIGHT


async def setup_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        height = float(update.message.text.replace(",", "."))
        if not 100 <= height <= 250:
            raise ValueError
        context.user_data["height"] = height
        await update.message.reply_text("🎂 Введи свой возраст:")
        return SETUP_AGE
    except ValueError:
        await update.message.reply_text("❌ Введи корректный рост, например: 175")
        return SETUP_HEIGHT


async def setup_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text)
        if not 10 <= age <= 120:
            raise ValueError
        context.user_data["age"] = age
        keyboard = [[KeyboardButton("👨 Мужской"), KeyboardButton("👩 Женский")]]
        await update.message.reply_text(
            "⚧ Укажи пол:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return SETUP_GENDER
    except ValueError:
        await update.message.reply_text("❌ Введи корректный возраст, например: 25")
        return SETUP_AGE


async def setup_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Мужской" in text:
        context.user_data["gender"] = "male"
    elif "Женский" in text:
        context.user_data["gender"] = "female"
    else:
        await update.message.reply_text("❌ Выбери из кнопок ниже")
        return SETUP_GENDER

    keyboard = [[KeyboardButton(k)] for k in ACTIVITY_LEVELS.keys()]
    await update.message.reply_text(
        "🏃 Выбери уровень активности:\n\n"
        "🛋 Минимальная — сидячий образ жизни\n"
        "🚶 Лёгкая — 1-3 тренировки в неделю\n"
        "🏃 Средняя — 3-5 тренировок в неделю\n"
        "💪 Высокая — 6-7 тренировок в неделю\n"
        "🏋️ Очень высокая — физический труд + тренировки",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return SETUP_ACTIVITY


async def setup_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ACTIVITY_LEVELS:
        await update.message.reply_text("❌ Выбери из кнопок ниже")
        return SETUP_ACTIVITY

    context.user_data["activity"] = ACTIVITY_LEVELS[text]
    keyboard = [[KeyboardButton(k)] for k in GOALS.keys()]
    await update.message.reply_text(
        "🎯 Какая твоя цель?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return SETUP_GOAL


async def setup_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in GOALS:
        await update.message.reply_text("❌ Выбери из кнопок ниже")
        return SETUP_GOAL

    context.user_data["goal"] = GOALS[text]
    data = context.user_data
    calories, protein, fat, carbs = calculate_goals(
        data["weight"], data["height"], data["age"],
        data["gender"], data["activity"], data["goal"]
    )

    user_id = update.effective_user.id
    username = update.effective_user.username

    conn = await get_db()
    await conn.execute("""
        INSERT INTO users (user_id, username, weight, height, age, gender, activity, goal,
                           calories_goal, protein_goal, fat_goal, carb_goal)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT (user_id) DO UPDATE SET
            weight=$3, height=$4, age=$5, gender=$6, activity=$7, goal=$8,
            calories_goal=$9, protein_goal=$10, fat_goal=$11, carb_goal=$12
    """, user_id, username, data["weight"], data["height"], data["age"],
        data["gender"], data["activity"], data["goal"], calories, protein, fat, carbs)
    await conn.close()

    await update.message.reply_text(
        f"✅ Профиль создан!\n\n"
        f"🎯 Твоя дневная норма:\n"
        f"🔥 Калории: {calories} ккал\n"
        f"🥩 Белки: {protein} г\n"
        f"🧈 Жиры: {fat} г\n"
        f"🍞 Углеводы: {carbs} г\n\n"
        f"Теперь отправляй фото еды или пиши что съел!"
    )

    await show_main_menu(update, context)
    return MAIN_MENU


# ─── MAIN MENU ────────────────────────────────────────────────────────────────

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📊 Статистика за сегодня")],
        [KeyboardButton("💧 Добавить воду"), KeyboardButton("📅 История")],
        [KeyboardButton("🎯 Моя норма"), KeyboardButton("⚙️ Изменить профиль")],
    ]
    await update.message.reply_text(
        "📸 Отправь фото еды или напиши что съел\n"
        "Например: *200г гречки и куриная грудка*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = await get_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()

    if not user:
        await update.message.reply_text("Сначала настрой профиль — напиши /start")
        return

    msg = await update.message.reply_text("🔍 Анализирую фото...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image = Image.open(io.BytesIO(file_bytes))

        response = model.generate_content([ANALYZE_PROMPT, image])
        text = response.text.strip()

        if "НЕ_ЕДА" in text:
            await msg.edit_text("🤔 Не вижу еду на фото. Попробуй другое фото!")
            return

        await save_and_reply(update, context, text, user, msg)

    except Exception as e:
        logger.error(f"Photo error: {e}")
        await msg.edit_text("😔 Не удалось проанализировать фото. Попробуй ещё раз!")


async def handle_text_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Handle menu buttons
    if text == "📊 Статистика за сегодня":
        await show_stats(update, context)
        return
    elif text == "💧 Добавить воду":
        await add_water_prompt(update, context)
        return
    elif text == "📅 История":
        await show_history(update, context)
        return
    elif text == "🎯 Моя норма":
        await show_goals(update, context)
        return
    elif text == "⚙️ Изменить профиль":
        await update.message.reply_text("Для изменения профиля напиши /start", reply_markup=ReplyKeyboardRemove())
        return
    elif text.startswith("💧"):
        await handle_water_input(update, context)
        return

    conn = await get_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()

    if not user:
        await update.message.reply_text("Сначала настрой профиль — напиши /start")
        return

    msg = await update.message.reply_text("🔍 Анализирую...")

    try:
        prompt = TEXT_PROMPT.format(food=text)
        response = model.generate_content(prompt)
        result = response.text.strip()

        if "НЕ_ЕДА" in result:
            await msg.edit_text("🤔 Не понял что за еда. Попробуй написать точнее, например: *200г гречки*", parse_mode="Markdown")
            return

        await save_and_reply(update, context, result, user, msg)

    except Exception as e:
        logger.error(f"Text error: {e}")
        await msg.edit_text("😔 Что-то пошло не так. Попробуй ещё раз!")


async def save_and_reply(update, context, text, user, msg):
    data = parse_nutrition_response(text)

    try:
        meal_name = data.get("БЛЮДО", "Неизвестное блюдо")
        portion = int(data.get("ПОРЦИЯ", 0))
        calories = int(data.get("КАЛОРИИ", 0))
        protein = float(data.get("БЕЛКИ", 0))
        fat = float(data.get("ЖИРЫ", 0))
        carbs = float(data.get("УГЛЕВОДЫ", 0))
        comment = data.get("КОММЕНТАРИЙ", "")
    except (ValueError, KeyError):
        await msg.edit_text("😔 Не удалось разобрать ответ. Попробуй ещё раз!")
        return

    user_id = update.effective_user.id
    conn = await get_db()
    await conn.execute("""
        INSERT INTO food_log (user_id, meal_name, portion, calories, protein, fat, carbs)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    """, user_id, meal_name, portion, calories, protein, fat, carbs)
    await conn.close()

    totals = await get_today_totals(user_id)
    cal_bar = progress_bar(totals["calories"], user["calories_goal"])
    prot_bar = progress_bar(totals["protein"], user["protein_goal"])

    remaining_cal = user["calories_goal"] - totals["calories"]
    remaining_sign = "осталось" if remaining_cal >= 0 else "превышение"

    reply = (
        f"✅ *{meal_name}* (~{portion}г)\n\n"
        f"🔥 {calories} ккал  |  🥩 {protein}г  |  🧈 {fat}г  |  🍞 {carbs}г\n\n"
        f"━━━ Сегодня ━━━\n"
        f"🔥 {cal_bar} {totals['calories']}/{user['calories_goal']} ккал\n"
        f"   ({abs(remaining_cal)} ккал {remaining_sign})\n"
        f"🥩 {prot_bar} {totals['protein']}/{user['protein_goal']}г белков\n\n"
    )

    if comment:
        reply += f"💡 {comment}"

    await msg.edit_text(reply, parse_mode="Markdown")


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = await get_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

    today = date.today()
    meals = await conn.fetch("""
        SELECT meal_name, calories, protein, fat, carbs, logged_at
        FROM food_log WHERE user_id = $1 AND DATE(logged_at) = $2
        ORDER BY logged_at
    """, user_id, today)
    await conn.close()

    totals = await get_today_totals(user_id)

    cal_pct = int(totals["calories"] / user["calories_goal"] * 100) if user["calories_goal"] else 0
    prot_pct = int(totals["protein"] / user["protein_goal"] * 100) if user["protein_goal"] else 0
    fat_pct = int(totals["fat"] / user["fat_goal"] * 100) if user["fat_goal"] else 0
    carb_pct = int(totals["carbs"] / user["carb_goal"] * 100) if user["carb_goal"] else 0

    text = f"📊 *Статистика за {today.strftime('%d.%m.%Y')}*\n\n"

    if meals:
        text += "🍽 *Приёмы пищи:*\n"
        for meal in meals:
            time_str = meal["logged_at"].strftime("%H:%M")
            text += f"  {time_str} — {meal['meal_name']} ({meal['calories']} ккал)\n"
        text += "\n"

    text += (
        f"📈 *Итого:*\n"
        f"🔥 Калории: {progress_bar(totals['calories'], user['calories_goal'])} {totals['calories']}/{user['calories_goal']} ({cal_pct}%)\n"
        f"🥩 Белки:   {progress_bar(totals['protein'], user['protein_goal'])} {totals['protein']}/{user['protein_goal']}г ({prot_pct}%)\n"
        f"🧈 Жиры:    {progress_bar(totals['fat'], user['fat_goal'])} {totals['fat']}/{user['fat_goal']}г ({fat_pct}%)\n"
        f"🍞 Углев.:  {progress_bar(totals['carbs'], user['carb_goal'])} {totals['carbs']}/{user['carb_goal']}г ({carb_pct}%)\n"
        f"💧 Вода:    {totals['water']} мл\n\n"
    )

    remaining = user["calories_goal"] - totals["calories"]
    if remaining > 0:
        text += f"✅ Осталось съесть: *{remaining} ккал*"
    else:
        text += f"⚠️ Превышение нормы на: *{abs(remaining)} ккал*"

    await update.message.reply_text(text, parse_mode="Markdown")


async def show_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = await get_db()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()

    gender_str = "Мужской" if user["gender"] == "male" else "Женский"
    goal_names = {0.85: "Похудение", 1.0: "Поддержание веса", 1.15: "Набор массы"}
    goal_str = goal_names.get(user["goal"], "—")

    text = (
        f"🎯 *Твой профиль и нормы*\n\n"
        f"⚖️ Вес: {user['weight']} кг\n"
        f"📏 Рост: {user['height']} см\n"
        f"🎂 Возраст: {user['age']} лет\n"
        f"⚧ Пол: {gender_str}\n"
        f"🏃 Цель: {goal_str}\n\n"
        f"*Дневная норма:*\n"
        f"🔥 Калории: {user['calories_goal']} ккал\n"
        f"🥩 Белки: {user['protein_goal']} г\n"
        f"🧈 Жиры: {user['fat_goal']} г\n"
        f"🍞 Углеводы: {user['carb_goal']} г\n\n"
        f"Для изменения профиля — /start"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = await get_db()
    rows = await conn.fetch("""
        SELECT DATE(logged_at) as day, SUM(calories), SUM(protein), SUM(fat), SUM(carbs)
        FROM food_log WHERE user_id = $1
        GROUP BY day ORDER BY day DESC LIMIT 7
    """, user_id)
    user = await conn.fetchrow("SELECT calories_goal FROM users WHERE user_id = $1", user_id)
    await conn.close()

    if not rows:
        await update.message.reply_text("📅 История пуста. Начни добавлять еду!")
        return

    text = "📅 *История за 7 дней*\n\n"
    for row in rows:
        day_str = row["day"].strftime("%d.%m")
        cal = int(row[1] or 0)
        bar = progress_bar(cal, user["calories_goal"], 8)
        text += f"`{day_str}` {bar} {cal} ккал\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def add_water_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("💧 150 мл"), KeyboardButton("💧 200 мл"), KeyboardButton("💧 250 мл")],
        [KeyboardButton("💧 300 мл"), KeyboardButton("💧 500 мл"), KeyboardButton("💧 1000 мл")],
    ]
    await update.message.reply_text(
        "💧 Сколько воды выпил?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def handle_water_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        amount = int("".join(filter(str.isdigit, text)))
        user_id = update.effective_user.id
        conn = await get_db()
        await conn.execute(
            "INSERT INTO water_log (user_id, amount) VALUES ($1, $2)",
            user_id, amount
        )
        await conn.close()

        totals = await get_today_totals(user_id)
        water_bar = progress_bar(totals["water"], 2000)
        await update.message.reply_text(
            f"💧 +{amount} мл добавлено!\n\n"
            f"Сегодня: {water_bar} {totals['water']}/2000 мл",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("📊 Статистика за сегодня")],
                [KeyboardButton("💧 Добавить воду"), KeyboardButton("📅 История")],
                [KeyboardButton("🎯 Моя норма"), KeyboardButton("⚙️ Изменить профиль")],
            ], resize_keyboard=True)
        )
    except Exception as e:
        logger.error(f"Water error: {e}")


def main():
    asyncio.get_event_loop().run_until_complete(init_db())

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SETUP_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_weight)],
            SETUP_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_height)],
            SETUP_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_age)],
            SETUP_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_gender)],
            SETUP_ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_activity)],
            SETUP_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_goal)],
            MAIN_MENU: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_food),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_food))

    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
