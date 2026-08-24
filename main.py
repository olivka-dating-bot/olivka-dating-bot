import os
from html import escape

import asyncpg
from aiohttp import web

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)


# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

NAME, AGE, CITY, GENDER, LOOKING_FOR, ABOUT, PHOTO = range(7)

db_pool = None


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

async def init_database():
    global db_pool

    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=1,
        max_size=5,
    )

    async with db_pool.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                name TEXT NOT NULL,
                age INTEGER NOT NULL,
                city TEXT NOT NULL,
                gender TEXT NOT NULL,
                looking_for TEXT NOT NULL,
                about TEXT NOT NULL,
                photo TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                from_user BIGINT NOT NULL,
                to_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_user, to_user)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS skips (
                from_user BIGINT NOT NULL,
                to_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_user, to_user)
            )
        """)


async def save_profile(profile):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO profiles (
                user_id,
                username,
                name,
                age,
                city,
                gender,
                looking_for,
                about,
                photo
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9
            )

            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                name = EXCLUDED.name,
                age = EXCLUDED.age,
                city = EXCLUDED.city,
                gender = EXCLUDED.gender,
                looking_for = EXCLUDED.looking_for,
                about = EXCLUDED.about,
                photo = EXCLUDED.photo,
                updated_at = CURRENT_TIMESTAMP
            """,
            profile["user_id"],
            profile["username"],
            profile["name"],
            profile["age"],
            profile["city"],
            profile["gender"],
            profile["looking_for"],
            profile["about"],
            profile["photo"],
        )


async def get_profile(user_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM profiles
            WHERE user_id = $1
            """,
            user_id,
        )

    if not row:
        return None

    return dict(row)


async def add_like(from_user, to_user):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO likes (from_user, to_user)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            from_user,
            to_user,
        )


async def add_skip(from_user, to_user):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO skips (from_user, to_user)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            from_user,
            to_user,
        )


async def is_match(user1, user2):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM likes
            WHERE from_user = $1
              AND to_user = $2
            """,
            user2,
            user1,
        )

    return row is not None


def gender_matches(looking_for, candidate_gender):
    if looking_for == "💞 Неважно":
        return True

    if looking_for == "👩 Девушку":
        return candidate_gender == "👩 Девушка"

    if looking_for == "👨 Мужчину":
        return candidate_gender == "👨 Мужчина"

    return True


def candidate_accepts(candidate_looking_for, viewer_gender):
    if candidate_looking_for == "💞 Неважно":
        return True

    if candidate_looking_for == "👩 Девушку":
        return viewer_gender == "👩 Девушка"

    if candidate_looking_for == "👨 Мужчину":
        return viewer_gender == "👨 Мужчина"

    return True


async def get_next_profile(user_id):
    viewer = await get_profile(user_id)

    if not viewer:
        return None

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT p.*
            FROM profiles p

            WHERE p.user_id <> $1

            AND NOT EXISTS (
                SELECT 1
                FROM likes l
                WHERE l.from_user = $1
                  AND l.to_user = p.user_id
            )

            AND NOT EXISTS (
                SELECT 1
                FROM skips s
                WHERE s.from_user = $1
                  AND s.to_user = p.user_id
            )

            ORDER BY p.updated_at DESC

            LIMIT 50
            """,
            user_id,
        )

    for row in rows:
        profile = dict(row)

        if not gender_matches(
            viewer["looking_for"],
            profile["gender"],
        ):
            continue

        if not candidate_accepts(
            profile["looking_for"],
            viewer["gender"],
        ):
            continue

        return profile

    return None


# =========================================================
# МЕНЮ
# =========================================================

def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["💘 Создать анкету"],
            ["🔥 Смотреть анкеты"],
            ["👤 Моя анкета"],
        ],
        resize_keyboard=True,
    )


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "💗 Добро пожаловать в OLIVKA MATCH!\n\n"
        "Создавай анкету, смотри людей и находи взаимную симпатию 💞",
        reply_markup=main_menu(),
    )


# =========================================================
# СОЗДАНИЕ АНКЕТЫ
# =========================================================

async def create_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Как тебя зовут? 😊",
        reply_markup=ReplyKeyboardRemove(),
    )

    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):

    name = update.message.text.strip()

    if len(name) < 2:
        await update.message.reply_text(
            "Напиши имя чуть подробнее 🙂"
        )
        return NAME

    context.user_data["name"] = name

    await update.message.reply_text(
        "Сколько тебе лет?\n\n"
        "OLIVKA MATCH — только 18+ 🔞"
    )

    return AGE


async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    if not text.isdigit():

        await update.message.reply_text(
            "Напиши возраст цифрами 🙂"
        )

        return AGE

    age = int(text)

    if age < 18:

        await update.message.reply_text(
            "OLIVKA MATCH доступен только пользователям 18+ 🔞",
            reply_markup=main_menu(),
        )

        return ConversationHandler.END

    if age > 100:

        await update.message.reply_text(
            "Проверь возраст 🙂"
        )

        return AGE

    context.user_data["age"] = age

    await update.message.reply_text(
        "Из какого ты города? 📍"
    )

    return CITY


async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["city"] = update.message.text.strip()

    keyboard = ReplyKeyboardMarkup(
        [
            ["👩 Девушка", "👨 Мужчина"]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "Кто ты?",
        reply_markup=keyboard,
    )

    return GENDER


async def get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):

    gender = update.message.text.strip()

    if gender not in ["👩 Девушка", "👨 Мужчина"]:

        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )

        return GENDER

    context.user_data["gender"] = gender

    keyboard = ReplyKeyboardMarkup(
        [
            ["👩 Девушку"],
            ["👨 Мужчину"],
            ["💞 Неважно"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "Кого хочешь найти? 💘",
        reply_markup=keyboard,
    )

    return LOOKING_FOR


async def get_looking_for(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    looking_for = update.message.text.strip()

    allowed = [
        "👩 Девушку",
        "👨 Мужчину",
        "💞 Неважно",
    ]

    if looking_for not in allowed:

        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )

        return LOOKING_FOR

    context.user_data["looking_for"] = looking_for

    await update.message.reply_text(
        "Расскажи немного о себе ✨\n\n"
        "Что любишь, чем увлекаешься и кого хочешь встретить?",
        reply_markup=ReplyKeyboardRemove(),
    )

    return ABOUT


async def get_about(update: Update, context: ContextTypes.DEFAULT_TYPE):

    about = update.message.text.strip()

    if len(about) > 500:

        await update.message.reply_text(
            "Описание получилось слишком длинным 😄\n"
            "Сделай до 500 символов."
        )

        return ABOUT

    context.user_data["about"] = about

    await update.message.reply_text(
        "Теперь отправь фотографию 📸"
    )

    return PHOTO


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photo_id = update.message.photo[-1].file_id

    user = update.effective_user

    profile = {
        "user_id": user.id,
        "username": user.username,
        "name": context.user_data["name"],
        "age": context.user_data["age"],
        "city": context.user_data["city"],
        "gender": context.user_data["gender"],
        "looking_for": context.user_data["looking_for"],
        "about": context.user_data["about"],
        "photo": photo_id,
    }

    await save_profile(profile)

    caption = (
        "💗 <b>Твоя анкета готова!</b>\n\n"
        f"👤 {escape(profile['name'])}, {profile['age']}\n"
        f"📍 {escape(profile['city'])}\n"
        f"{escape(profile['gender'])}\n"
        f"🔎 Ищу: {escape(profile['looking_for'])}\n\n"
        f"✨ {escape(profile['about'])}"
    )

    await update.message.reply_photo(
        photo=photo_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )

    return ConversationHandler.END


async def photo_required(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "Нужно отправить именно фотографию 📸"
    )

    return PHOTO


# =========================================================
# МОЯ АНКЕТА
# =========================================================

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    profile = await get_profile(user_id)

    if not profile:

        await update.message.reply_text(
            "У тебя пока нет анкеты 💗\n\n"
            "Нажми «💘 Создать анкету».",
            reply_markup=main_menu(),
        )

        return

    caption = (
        f"👤 <b>{escape(profile['name'])}, {profile['age']}</b>\n"
        f"📍 {escape(profile['city'])}\n"
        f"{escape(profile['gender'])}\n"
        f"🔎 Ищу: {escape(profile['looking_for'])}\n\n"
        f"✨ {escape(profile['about'])}"
    )

    await update.message.reply_photo(
        photo=profile["photo"],
        caption=caption,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =========================================================
# ПРОСМОТР АНКЕТ
# =========================================================

async def browse_profiles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    my_data = await get_profile(user_id)

    if not my_data:

        await update.message.reply_text(
            "Сначала создай свою анкету 💘",
            reply_markup=main_menu(),
        )

        return

    profile = await get_next_profile(user_id)

    if not profile:

        await update.message.reply_text(
            "Пока подходящих новых анкет нет 😌\n\n"
            "Загляни сюда немного позже.",
            reply_markup=main_menu(),
        )

        return

    await show_profile(update, profile)


async def show_profile(update, profile):

    caption = (
        f"💗 <b>{escape(profile['name'])}, {profile['age']}</b>\n"
        f"📍 {escape(profile['city'])}\n\n"
        f"✨ {escape(profile['about'])}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👎",
                    callback_data=f"skip:{profile['user_id']}",
                ),
                InlineKeyboardButton(
                    "❤️",
                    callback_data=f"like:{profile['user_id']}",
                ),
            ]
        ]
    )

    if update.callback_query:

        await update.callback_query.message.reply_photo(
            photo=profile["photo"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    else:

        await update.message.reply_photo(
            photo=profile["photo"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# =========================================================
# ЛАЙКИ / ПРОПУСКИ / MATCH
# =========================================================

async def profile_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    action, target_id = query.data.split(":")

    target_id = int(target_id)
    user_id = query.from_user.id

    try:

        await query.edit_message_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    if action == "skip":

        await add_skip(
            user_id,
            target_id,
        )

    elif action == "like":

        await add_like(
            user_id,
            target_id,
        )

        match = await is_match(
            user_id,
            target_id,
        )

        if match:

            my_data = await get_profile(user_id)
            target_data = await get_profile(target_id)

            if my_data and target_data:

                if target_data["username"]:

                    target_link = (
                        f"@{escape(target_data['username'])}"
                    )

                else:

                    target_link = (
                        f'<a href="tg://user?id={target_id}">'
                        f'{escape(target_data["name"])}</a>'
                    )

                if my_data["username"]:

                    my_link = (
                        f"@{escape(my_data['username'])}"
                    )

                else:

                    my_link = (
                        f'<a href="tg://user?id={user_id}">'
                        f'{escape(my_data["name"])}</a>'
                    )

                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "💞 <b>У ВАС СОВПАДЕНИЕ!</b>\n\n"
                        "Симпатия взаимна 🔥\n\n"
                        f"Написать: {target_link}"
                    ),
                    parse_mode="HTML",
                )

                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "💞 <b>У ВАС СОВПАДЕНИЕ!</b>\n\n"
                        "Симпатия взаимна 🔥\n\n"
                        f"Написать: {my_link}"
                    ),
                    parse_mode="HTML",
                )

        else:

            await query.message.reply_text(
                "❤️ Лайк отправлен!"
            )

    next_profile = await get_next_profile(user_id)

    if next_profile:

        await show_profile(
            update,
            next_profile,
        )

    else:

        await query.message.reply_text(
            "Подходящие анкеты закончились 😊\n\n"
            "Возвращайся позже.",
            reply_markup=main_menu(),
        )


# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "Создание анкеты отменено.",
        reply_markup=main_menu(),
    )

    return ConversationHandler.END


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

application = (
    Application.builder()
    .token(TOKEN)
    .concurrent_updates(False)
    .build()
)


profile_conversation = ConversationHandler(

    entry_points=[
        MessageHandler(
            filters.Regex("^💘 Создать анкету$"),
            create_profile,
        )
    ],

    states={

        NAME: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                get_name,
            )
        ],

        AGE: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                get_age,
            )
        ],

        CITY: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                get_city,
            )
        ],

        GENDER: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                get_gender,
            )
        ],

        LOOKING_FOR: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                get_looking_for,
            )
        ],

        ABOUT: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                get_about,
            )
        ],

        PHOTO: [
            MessageHandler(
                filters.PHOTO,
                get_photo,
            ),

            MessageHandler(
                ~filters.PHOTO & ~filters.COMMAND,
                photo_required,
            ),
        ],
    },

    fallbacks=[
        CommandHandler(
            "cancel",
            cancel,
        )
    ],
)


application.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

application.add_handler(
    profile_conversation
)

application.add_handler(
    MessageHandler(
        filters.Regex("^👤 Моя анкета$"),
        my_profile,
    )
)

application.add_handler(
    MessageHandler(
        filters.Regex("^🔥 Смотреть анкеты$"),
        browse_profiles,
    )
)

application.add_handler(
    CallbackQueryHandler(
        profile_action,
        pattern=r"^(like|skip):",
    )
)


# =========================================================
# WEBHOOK / RENDER
# =========================================================

async def health(request):

    return web.Response(
        text="OLIVKA MATCH is running 💗"
    )


async def telegram_webhook(request):

    data = await request.json()

    update = Update.de_json(
        data,
        application.bot,
    )

    await application.process_update(update)

    return web.Response(
        text="OK"
    )


async def on_startup(web_app):

    await init_database()

    await application.initialize()

    await application.start()

    render_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if render_url:

        await application.bot.set_webhook(
            f"{render_url}/telegram"
        )

    print("OLIVKA MATCH started")


async def on_cleanup(web_app):

    global db_pool

    await application.stop()

    await application.shutdown()

    if db_pool:

        await db_pool.close()


# =========================================================
# WEB SERVER
# =========================================================

web_app = web.Application()

web_app.router.add_get(
    "/",
    health,
)

web_app.router.add_post(
    "/telegram",
    telegram_webhook,
)

web_app.on_startup.append(
    on_startup
)

web_app.on_cleanup.append(
    on_cleanup
)

web.run_app(
    web_app,
    host="0.0.0.0",
    port=PORT,
)
