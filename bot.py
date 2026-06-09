import os
import logging
from datetime import date
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from PIL import Image
import io
import dataset
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

SETUP_WEIGHT, SETUP_HEIGHT, SETUP_AGE, SETUP_GENDER, SETUP_ACTIVITY, SETUP_GOAL, MAIN_MENU = range(7)

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


def get_db():
    return dataset.connect(DATABASE_URL)


def init_db():
    db = get_db()
    db.execute(text("""
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
            carb_goal INTEGER
        )
    """))
    db.execute(text("""
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
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS water_log (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            amount INTEGER,
            logged_at TIMESTAMP DEFAULT NOW()
        )
    """))
    logger.info("DB initialized")


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


def parse_nutrition_response(text_resp):
    result = {}
    for line in text_resp.strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def get_user(user_id):
    db = get_db()
    return db["users"].find_one(user_id=user_id)


def get_today_totals(user_id):
    db = get_db()
    today = date.today()
    rows = list(db.execute(text(
        "SELECT COALESCE(SUM(calories),0) as cal, COALESCE(SUM(protein),0) as prot, "
        "COALESCE(SUM(fat),0) as fat, COALESCE(SUM(carbs),0) as carbs "
        "FROM food_log WHERE user_id=:uid AND DATE(logged_at)=:d"
    ), {"uid": user_id, "d": today}))
    water = list(db.execute(text(
        "SELECT COALESCE(SUM(amount),0) as w FROM water_log WHERE user_id=:uid AND DATE(logged_at)=:d"
    ), {"uid": user_id, "d": today}))
    r = rows[0]
    return {
        "calories": int(r["cal"] or 0),
        "protein": round(float(r["prot"] or 0), 1),
        "fat": round(float(r["fat"] or 0), 1),
        "carbs": round(float(r["carbs"] or 0), 1),
        "water": int(water[0]["w"] or 0)
    }


def progress_bar(current, total, length=10):
    if total == 0:
        return "░" * length
    filled = int(min(current / total, 1) * length)
    return "█" * filled + "░" * (length - filled)


MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📊 Статистика за сегодня")],
    [KeyboardButton("💧 Добавить воду"), KeyboardButton("📅 История")],
    [KeyboardButton("🎯 Моя норма"), KeyboardButton("⚙️ Изменить профиль")],
], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user:
        await update.message.reply_text("👋 С возвращением! Отправь фото еды или напиши что съел.", reply_markup=MAIN_KB)
        return MAIN_MENU
    await update.message.reply_text(
        "👋 Привет! Я твой персональный трекер питания.\n\nДавай настроим профиль!\n\n⚖️ Введи свой вес (в кг):",
        reply_markup=ReplyKeyboardRemove()
    )
    return SETUP_WEIGHT


async def setup_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.replace(",", "."))
        if not 30 <= weight <= 300: raise ValueError
        context.user_data["weight"] = weight
        await update.message.reply_text("📏 Введи свой рост (в см):")
        return SETUP_HEIGHT
    except ValueError:
        await update.message.reply_text("❌ Введи корректный вес, например: 70")
        return SETUP_WEIGHT


async def setup_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        height = float(update.message.text.replace(",", "."))
        if not 100 <= height <= 250: raise ValueError
        context.user_data["height"] = height
        await update.message.reply_text("🎂 Введи свой возраст:")
        return SETUP_AGE
    except ValueError:
        await update.message.reply_text("❌ Введи корректный рост, например: 175")
        return SETUP_HEIGHT


async def setup_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text)
        if not 10 <= age <= 120: raise ValueError
        context.user_data["age"] = age
        kb = [[KeyboardButton("👨 Мужской"), KeyboardButton("👩 Женский")]]
        await update.message.reply_text("⚧ Укажи пол:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return SETUP_GENDER
    except ValueError:
        await update.message.reply_text("❌ Введи корректный возраст, например: 25")
        return SETUP_AGE


async def setup_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Мужской" in text: context.user_data["gender"] = "male"
    elif "Женский" in text: context.user_data["gender"] = "female"
    else:
        await update.message.reply_text("❌ Выбери из кнопок")
        return SETUP_GENDER
    kb = [[KeyboardButton(k)] for k in ACTIVITY_LEVELS.keys()]
    await update.message.reply_text(
        "🏃 Уровень активности:\n\n🛋 Минимальная — сидячий образ жизни\n🚶 Лёгкая — 1-3 тренировки\n🏃 Средняя — 3-5 тренировок\n💪 Высокая — 6-7 тренировок\n🏋️ Очень высокая — физический труд",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return SETUP_ACTIVITY


async def setup_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ACTIVITY_LEVELS:
        await update.message.reply_text("❌ Выбери из кнопок")
        return SETUP_ACTIVITY
    context.user_data["activity"] = ACTIVITY_LEVELS[text]
    kb = [[KeyboardButton(k)] for k in GOALS.keys()]
    await update.message.reply_text("🎯 Какая цель?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return SETUP_GOAL


async def setup_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in GOALS:
        await update.message.reply_text("❌ Выбери из кнопок")
        return SETUP_GOAL
    context.user_data["goal"] = GOALS[text]
    data = context.user_data
    calories, protein, fat, carbs = calculate_goals(
        data["weight"], data["height"], data["age"],
        data["gender"], data["activity"], data["goal"]
    )
    user_id = update.effective_user.id
    db = get_db()
    db["users"].upsert({
        "user_id": user_id,
        "username": update.effective_user.username,
        "weight": data["weight"], "height": data["height"], "age": data["age"],
        "gender": data["gender"], "activity": data["activity"], "goal": data["goal"],
        "calories_goal": calories, "protein_goal": protein, "fat_goal": fat, "carb_goal": carbs
    }, ["user_id"])
    await update.message.reply_text(
        f"✅ Профиль создан!\n\n🎯 Твоя дневная норма:\n"
        f"🔥 Калории: {calories} ккал\n🥩 Белки: {protein} г\n"
        f"🧈 Жиры: {fat} г\n🍞 Углеводы: {carbs} г\n\n"
        f"Отправляй фото еды или пиши что съел!",
        reply_markup=MAIN_KB
    )
    return MAIN_MENU


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала настрой профиль — напиши /start")
        return MAIN_MENU
    msg = await update.message.reply_text("🔍 Анализирую фото...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image = Image.open(io.BytesIO(file_bytes))
        response = model.generate_content([ANALYZE_PROMPT, image])
        resp_text = response.text.strip()
        if "НЕ_ЕДА" in resp_text:
            await msg.edit_text("🤔 Не вижу еду на фото. Попробуй другое!")
            return MAIN_MENU
        await save_and_reply(update, resp_text, user, msg)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await msg.edit_text("😔 Не удалось проанализировать. Попробуй ещё раз!")
    return MAIN_MENU


async def handle_text_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if text == "📊 Статистика за сегодня":
        await show_stats(update, context); return MAIN_MENU
    elif text == "💧 Добавить воду":
        await add_water_prompt(update, context); return MAIN_MENU
    elif text == "📅 История":
        await show_history(update, context); return MAIN_MENU
    elif text == "🎯 Моя норма":
        await show_goals(update, context); return MAIN_MENU
    elif text == "⚙️ Изменить профиль":
        return await start(update, context)
    elif text.startswith("💧") and "мл" in text:
        await handle_water_input(update, context); return MAIN_MENU

    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала настрой профиль — напиши /start")
        return MAIN_MENU

    msg = await update.message.reply_text("🔍 Анализирую...")
    try:
        prompt = TEXT_PROMPT.format(food=text)
        response = model.generate_content(prompt)
        result = response.text.strip()
        if "НЕ_ЕДА" in result:
            await msg.edit_text("🤔 Напиши точнее, например: *200г гречки и курица*", parse_mode="Markdown")
            return MAIN_MENU
        await save_and_reply(update, result, user, msg)
    except Exception as e:
        logger.error(f"Text error: {e}")
        await msg.edit_text("😔 Что-то пошло не так. Попробуй ещё раз!")
    return MAIN_MENU


async def save_and_reply(update, text, user, msg):
    data = parse_nutrition_response(text)
    try:
        meal_name = data.get("БЛЮДО", "Блюдо")
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
    db = get_db()
    db["food_log"].insert({
        "user_id": user_id, "meal_name": meal_name, "portion": portion,
        "calories": calories, "protein": protein, "fat": fat, "carbs": carbs
    })

    totals = get_today_totals(user_id)
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
        f"🥩 {prot_bar} {totals['protein']}/{user['protein_goal']}г белков\n"
    )
    if comment:
        reply += f"\n💡 {comment}"
    await msg.edit_text(reply, parse_mode="Markdown")


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    today = date.today()
    db = get_db()
    meals = list(db.execute(text(
        "SELECT meal_name, calories, logged_at FROM food_log "
        "WHERE user_id=:uid AND DATE(logged_at)=:d ORDER BY logged_at"
    ), {"uid": user_id, "d": today}))

    totals = get_today_totals(user_id)
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
        f"🔥 {progress_bar(totals['calories'], user['calories_goal'])} {totals['calories']}/{user['calories_goal']} ({cal_pct}%)\n"
        f"🥩 {progress_bar(totals['protein'], user['protein_goal'])} {totals['protein']}/{user['protein_goal']}г ({prot_pct}%)\n"
        f"🧈 {progress_bar(totals['fat'], user['fat_goal'])} {totals['fat']}/{user['fat_goal']}г ({fat_pct}%)\n"
        f"🍞 {progress_bar(totals['carbs'], user['carb_goal'])} {totals['carbs']}/{user['carb_goal']}г ({carb_pct}%)\n"
        f"💧 {totals['water']} мл воды\n\n"
    )
    remaining = user["calories_goal"] - totals["calories"]
    text += f"✅ Осталось: *{remaining} ккал*" if remaining > 0 else f"⚠️ Превышение: *{abs(remaining)} ккал*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)


async def show_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    gender_str = "Мужской" if user["gender"] == "male" else "Женский"
    goal_names = {0.85: "Похудение", 1.0: "Поддержание веса", 1.15: "Набор массы"}
    goal_str = goal_names.get(float(user["goal"]), "—")
    text = (
        f"🎯 *Профиль и нормы*\n\n"
        f"⚖️ Вес: {user['weight']} кг\n📏 Рост: {user['height']} см\n"
        f"🎂 Возраст: {user['age']} лет\n⚧ Пол: {gender_str}\n🏃 Цель: {goal_str}\n\n"
        f"*Дневная норма:*\n🔥 {user['calories_goal']} ккал\n"
        f"🥩 {user['protein_goal']} г белков\n🧈 {user['fat_goal']} г жиров\n🍞 {user['carb_goal']} г углеводов"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    db = get_db()
    rows = list(db.execute(text(
        "SELECT DATE(logged_at) as day, SUM(calories) as cal FROM food_log "
        "WHERE user_id=:uid GROUP BY day ORDER BY day DESC LIMIT 7"
    ), {"uid": user_id}))

    if not rows:
        await update.message.reply_text("📅 История пуста. Начни добавлять еду!", reply_markup=MAIN_KB)
        return
    text = "📅 *История за 7 дней*\n\n"
    for row in rows:
        day_str = row["day"].strftime("%d.%m")
        cal = int(row["cal"] or 0)
        bar = progress_bar(cal, user["calories_goal"], 8)
        text += f"`{day_str}` {bar} {cal} ккал\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)


async def add_water_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [KeyboardButton("💧 150 мл"), KeyboardButton("💧 200 мл"), KeyboardButton("💧 250 мл")],
        [KeyboardButton("💧 300 мл"), KeyboardButton("💧 500 мл"), KeyboardButton("💧 1000 мл")],
    ]
    await update.message.reply_text("💧 Сколько воды выпил?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))


async def handle_water_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int("".join(filter(str.isdigit, update.message.text)))
        user_id = update.effective_user.id
        db = get_db()
        db["water_log"].insert({"user_id": user_id, "amount": amount})
        totals = get_today_totals(user_id)
        water_bar = progress_bar(totals["water"], 2000)
        await update.message.reply_text(
            f"💧 +{amount} мл добавлено!\nСегодня: {water_bar} {totals['water']}/2000 мл",
            reply_markup=MAIN_KB
        )
    except Exception as e:
        logger.error(f"Water error: {e}")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
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
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_food))
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
