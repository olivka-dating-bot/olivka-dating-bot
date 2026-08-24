import os
import logging
from html import escape

import asyncpg
from aiohttp import web

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
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


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# SETTINGS
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

MAX_MEDIA = 6

(
    CREATE_NAME,
    CREATE_AGE,
    CREATE_CITY,
    CREATE_GENDER,
    CREATE_LOOKING,
    CREATE_ABOUT,
    CREATE_PHOTO,
    EDIT_VALUE,
    ADD_MEDIA,
) = range(9)

db_pool = None


# ============================================================
# DATABASE
# ============================================================

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
            CREATE TABLE IF NOT EXISTS profile_media (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        # Если база была создана старой версией бота,
        # переносим главное фото в галерею.
        await conn.execute("""
            INSERT INTO profile_media (
                user_id,
                media_type,
                file_id,
                sort_order
            )
            SELECT
                p.user_id,
                'photo',
                p.photo,
                0
            FROM profiles p
            WHERE p.photo IS NOT NULL
              AND p.photo <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM profile_media pm
                  WHERE pm.user_id = p.user_id
              )
        """)

    logger.info("Database initialized")


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

    return dict(row) if row else None


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
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)

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


async def update_profile_field(user_id, field, value):
    allowed = {
        "name",
        "age",
        "city",
        "gender",
        "looking_for",
        "about",
    }

    if field not in allowed:
        return

    query = f"""
        UPDATE profiles
        SET {field} = $1,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = $2
    """

    async with db_pool.acquire() as conn:
        await conn.execute(
            query,
            value,
            user_id,
        )


async def get_media(user_id):
    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                id,
                media_type,
                file_id,
                sort_order
            FROM profile_media
            WHERE user_id = $1
            ORDER BY sort_order ASC, id ASC
            """,
            user_id,
        )

    return [dict(row) for row in rows]


async def add_media(user_id, media_type, file_id):
    media = await get_media(user_id)

    if len(media) >= MAX_MEDIA:
        return False

    sort_order = len(media)

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO profile_media (
                user_id,
                media_type,
                file_id,
                sort_order
            )
            VALUES ($1,$2,$3,$4)
            """,
            user_id,
            media_type,
            file_id,
            sort_order,
        )

    return True


async def replace_media_with_first_photo(user_id, file_id):
    """
    Используется только при создании новой анкеты.

    Здесь НЕ устанавливаем profiles.photo = NULL,
    поэтому старая схема PostgreSQL с NOT NULL не ломается.
    """

    async with db_pool.acquire() as conn:

        async with conn.transaction():

            await conn.execute(
                """
                DELETE FROM profile_media
                WHERE user_id = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                INSERT INTO profile_media (
                    user_id,
                    media_type,
                    file_id,
                    sort_order
                )
                VALUES ($1, 'photo', $2, 0)
                """,
                user_id,
                file_id,
            )

            await conn.execute(
                """
                UPDATE profiles
                SET photo = $1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $2
                """,
                file_id,
                user_id,
            )


async def delete_last_media(user_id):
    media = await get_media(user_id)

    # Главное фото обязательно оставляем.
    if len(media) <= 1:
        return False

    last = media[-1]

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM profile_media
            WHERE id = $1
            """,
            last["id"],
        )

    return True


async def delete_profile(user_id):
    async with db_pool.acquire() as conn:

        async with conn.transaction():

            await conn.execute(
                """
                DELETE FROM profile_media
                WHERE user_id = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM likes
                WHERE from_user = $1
                   OR to_user = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM skips
                WHERE from_user = $1
                   OR to_user = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM profiles
                WHERE user_id = $1
                """,
                user_id,
            )


async def add_like(from_user, to_user):
    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO likes (
                from_user,
                to_user
            )
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
            """,
            from_user,
            to_user,
        )


async def add_skip(from_user, to_user):
    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO skips (
                from_user,
                to_user
            )
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
            """,
            from_user,
            to_user,
        )


async def is_match(user1, user2):
    async with db_pool.acquire() as conn:

        result = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM likes
                WHERE from_user = $1
                  AND to_user = $2
            )
            """,
            user2,
            user1,
        )

    return bool(result)


# ============================================================
# MATCH FILTER
# ============================================================

def gender_matches(looking_for, gender):

    if looking_for == "💞 Неважно":
        return True

    if looking_for == "👩 Девушку":
        return gender == "👩 Девушка"

    if looking_for == "👨 Мужчину":
        return gender == "👨 Мужчина"

    return True


def candidate_accepts(candidate_looking_for, my_gender):

    if candidate_looking_for == "💞 Неважно":
        return True

    if candidate_looking_for == "👩 Девушку":
        return my_gender == "👩 Девушка"

    if candidate_looking_for == "👨 Мужчину":
        return my_gender == "👨 Мужчина"

    return True


async def get_next_profile(user_id):
    me = await get_profile(user_id)

    if not me:
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
            LIMIT 100
            """,
            user_id,
        )

    for row in rows:

        profile = dict(row)

        media = await get_media(
            profile["user_id"]
        )

        if not media:
            continue

        if not gender_matches(
            me["looking_for"],
            profile["gender"],
        ):
            continue

        if not candidate_accepts(
            profile["looking_for"],
            me["gender"],
        ):
            continue

        return profile

    return None


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():

    return ReplyKeyboardMarkup(
        [
            ["🔥 Смотреть анкеты"],
            [
                "👤 Моя анкета",
                "✏️ Редактировать анкету",
            ],
        ],
        resize_keyboard=True,
    )


def create_menu():

    return ReplyKeyboardMarkup(
        [
            ["💘 Создать анкету"],
        ],
        resize_keyboard=True,
    )


def gender_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                "👩 Девушка",
                "👨 Мужчина",
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def looking_keyboard():

    return ReplyKeyboardMarkup(
        [
            ["👩 Девушку"],
            ["👨 Мужчину"],
            ["💞 Неважно"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def edit_menu():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👤 Имя",
                    callback_data="edit:name",
                ),
                InlineKeyboardButton(
                    "🎂 Возраст",
                    callback_data="edit:age",
                ),
            ],

            [
                InlineKeyboardButton(
                    "📍 Город",
                    callback_data="edit:city",
                ),
                InlineKeyboardButton(
                    "🚻 Пол",
                    callback_data="edit:gender",
                ),
            ],

            [
                InlineKeyboardButton(
                    "💘 Кого ищу",
                    callback_data="edit:looking_for",
                )
            ],

            [
                InlineKeyboardButton(
                    "✨ О себе",
                    callback_data="edit:about",
                )
            ],

            [
                InlineKeyboardButton(
                    "📸 Фото и видео",
                    callback_data="edit:media",
                )
            ],

            [
                InlineKeyboardButton(
                    "🗑 Удалить анкету",
                    callback_data="profile_delete",
                )
            ],

            [
                InlineKeyboardButton(
                    "✅ Готово",
                    callback_data="edit:done",
                )
            ],
        ]
    )


def media_menu():

    # ВАЖНО:
    # здесь больше НЕТ кнопки "Посмотреть галерею".
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Добавить фото/видео",
                    callback_data="media:add",
                )
            ],

            [
                InlineKeyboardButton(
                    "🗑 Удалить последнее",
                    callback_data="media:delete_last",
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="media:back",
                )
            ],
        ]
    )


# ============================================================
# PROFILE TEXT
# ============================================================

def profile_caption(profile):

    return (
        f"💗 <b>{escape(profile['name'])}, "
        f"{profile['age']}</b>\n"
        f"📍 {escape(profile['city'])}\n"
        f"{escape(profile['gender'])}\n"
        f"🔎 Ищу: {escape(profile['looking_for'])}\n\n"
        f"✨ {escape(profile['about'])}"
    )


# ============================================================
# ALBUM
# ============================================================

async def send_profile_album(message, profile):

    media = await get_media(
        profile["user_id"]
    )

    caption = profile_caption(profile)

    if not media:

        # Подстраховка для старых записей.
        if profile.get("photo"):

            await message.reply_photo(
                photo=profile["photo"],
                caption=caption,
                parse_mode="HTML",
            )

        else:

            await message.reply_text(
                caption,
                parse_mode="HTML",
            )

        return

    # ОДНО фото/видео
    if len(media) == 1:

        item = media[0]

        if item["media_type"] == "photo":

            await message.reply_photo(
                photo=item["file_id"],
                caption=caption,
                parse_mode="HTML",
            )

        else:

            await message.reply_video(
                video=item["file_id"],
                caption=caption,
                parse_mode="HTML",
            )

        return

    # НЕСКОЛЬКО = единый Telegram-альбом
    album = []

    for index, item in enumerate(
        media[:MAX_MEDIA]
    ):

        item_caption = (
            caption
            if index == 0
            else None
        )

        item_parse_mode = (
            "HTML"
            if index == 0
            else None
        )

        if item["media_type"] == "photo":

            album.append(
                InputMediaPhoto(
                    media=item["file_id"],
                    caption=item_caption,
                    parse_mode=item_parse_mode,
                )
            )

        elif item["media_type"] == "video":

            album.append(
                InputMediaVideo(
                    media=item["file_id"],
                    caption=item_caption,
                    parse_mode=item_parse_mode,
                )
            )

    try:

        await message.reply_media_group(
            media=album
        )

    except Exception as e:

        logger.exception(
            "Media group failed: %s",
            e,
        )

        # Если Telegram не принял смешанный альбом,
        # отправляем медиа по одному.
        for index, item in enumerate(
            media[:MAX_MEDIA]
        ):

            item_caption = (
                caption
                if index == 0
                else None
            )

            if item["media_type"] == "photo":

                await message.reply_photo(
                    photo=item["file_id"],
                    caption=item_caption,
                    parse_mode=(
                        "HTML"
                        if item_caption
                        else None
                    ),
                )

            elif item["media_type"] == "video":

                await message.reply_video(
                    video=item["file_id"],
                    caption=item_caption,
                    parse_mode=(
                        "HTML"
                        if item_caption
                        else None
                    ),
                )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    profile = await get_profile(
        update.effective_user.id
    )

    if profile:

        await update.message.reply_text(
            "💗 Добро пожаловать в OLIVKA MATCH!\n\n"
            "Твоя анкета на месте ✅",
            reply_markup=main_menu(),
        )

    else:

        await update.message.reply_text(
            "💗 Добро пожаловать в OLIVKA MATCH!\n\n"
            "Создай анкету и начинай знакомиться.",
            reply_markup=create_menu(),
        )


# ============================================================
# CREATE PROFILE
# ============================================================

async def create_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.clear()

    await update.message.reply_text(
        "Как тебя зовут? 😊",
        reply_markup=ReplyKeyboardRemove(),
    )

    return CREATE_NAME


async def create_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    name = update.message.text.strip()

    if len(name) < 2:

        await update.message.reply_text(
            "Напиши имя чуть подробнее 🙂"
        )

        return CREATE_NAME

    context.user_data["new_name"] = name

    await update.message.reply_text(
        "Сколько тебе лет?\n\n"
        "Только 18+ 🔞"
    )

    return CREATE_AGE


async def create_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text.strip()

    if not text.isdigit():

        await update.message.reply_text(
            "Напиши возраст цифрами 🙂"
        )

        return CREATE_AGE

    age = int(text)

    if age < 18:

        await update.message.reply_text(
            "OLIVKA MATCH доступен только 18+ 🔞",
            reply_markup=create_menu(),
        )

        return ConversationHandler.END

    if age > 100:

        await update.message.reply_text(
            "Проверь возраст 🙂"
        )

        return CREATE_AGE

    context.user_data["new_age"] = age

    await update.message.reply_text(
        "Из какого ты города? 📍"
    )

    return CREATE_CITY


async def create_city(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    city = update.message.text.strip()

    context.user_data["new_city"] = city

    await update.message.reply_text(
        "Кто ты?",
        reply_markup=gender_keyboard(),
    )

    return CREATE_GENDER


async def create_gender(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    gender = update.message.text.strip()

    if gender not in (
        "👩 Девушка",
        "👨 Мужчина",
    ):

        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )

        return CREATE_GENDER

    context.user_data["new_gender"] = gender

    await update.message.reply_text(
        "Кого хочешь найти? 💘",
        reply_markup=looking_keyboard(),
    )

    return CREATE_LOOKING


async def create_looking(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    looking = update.message.text.strip()

    if looking not in (
        "👩 Девушку",
        "👨 Мужчину",
        "💞 Неважно",
    ):

        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )

        return CREATE_LOOKING

    context.user_data[
        "new_looking"
    ] = looking

    await update.message.reply_text(
        "Расскажи немного о себе ✨",
        reply_markup=ReplyKeyboardRemove(),
    )

    return CREATE_ABOUT


async def create_about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    about = update.message.text.strip()

    if len(about) > 500:

        await update.message.reply_text(
            "Описание должно быть до 500 символов."
        )

        return CREATE_ABOUT

    context.user_data["new_about"] = about

    await update.message.reply_text(
        "Теперь отправь главное фото 📸\n\n"
        "После создания анкеты можно будет добавить "
        "ещё фотографии и видео."
    )

    return CREATE_PHOTO


async def create_first_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message.photo:

        await update.message.reply_text(
            "Нужно отправить именно фотографию 📸"
        )

        return CREATE_PHOTO

    user = update.effective_user

    photo_id = (
        update.message.photo[-1].file_id
    )

    profile = {
        "user_id": user.id,
        "username": user.username,
        "name": context.user_data[
            "new_name"
        ],
        "age": context.user_data[
            "new_age"
        ],
        "city": context.user_data[
            "new_city"
        ],
        "gender": context.user_data[
            "new_gender"
        ],
        "looking_for": context.user_data[
            "new_looking"
        ],
        "about": context.user_data[
            "new_about"
        ],
        "photo": photo_id,
    }

    try:

        # ВАЖНО:
        # сначала сохраняем profile с настоящим photo_id.
        await save_profile(profile)

        # Затем обновляем галерею.
        # profiles.photo никогда не становится NULL.
        await replace_media_with_first_photo(
            user.id,
            photo_id,
        )

    except Exception as e:

        logger.exception(
            "Create profile failed: %s",
            e,
        )

        await update.message.reply_text(
            "Произошла ошибка при сохранении анкеты 😔\n"
            "Попробуй отправить фото ещё раз."
        )

        return CREATE_PHOTO

    await update.message.reply_text(
        "✅ Анкета сохранена!",
        reply_markup=main_menu(),
    )

    saved = await get_profile(
        user.id
    )

    await send_profile_album(
        update.message,
        saved,
    )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# MY PROFILE
# ============================================================

async def my_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    profile = await get_profile(
        user_id
    )

    if not profile:

        await update.message.reply_text(
            "У тебя пока нет анкеты 💗",
            reply_markup=create_menu(),
        )

        return

    await send_profile_album(
        update.message,
        profile,
    )


# ============================================================
# EDIT PROFILE
# ============================================================

async def open_edit_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    profile = await get_profile(
        update.effective_user.id
    )

    if not profile:

        await update.message.reply_text(
            "Сначала создай анкету 💘",
            reply_markup=create_menu(),
        )

        return

    await update.message.reply_text(
        "✏️ Что хочешь изменить?",
        reply_markup=edit_menu(),
    )


async def edit_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    action = query.data.split(
        ":",
        1
    )[1]

    if action == "done":

        await query.message.reply_text(
            "✅ Всё сохранено.",
            reply_markup=main_menu(),
        )

        return ConversationHandler.END

    if action == "media":

        media = await get_media(
            query.from_user.id
        )

        await query.message.reply_text(
            f"📸 Фото и видео\n\n"
            f"Сейчас в анкете: "
            f"{len(media)}/{MAX_MEDIA}",
            reply_markup=media_menu(),
        )

        return ConversationHandler.END

    context.user_data[
        "edit_field"
    ] = action

    prompts = {
        "name": "Напиши новое имя:",
        "age": "Напиши новый возраст:",
        "city": "Напиши новый город:",
        "gender": "Выбери пол:",
        "looking_for": "Кого хочешь найти?",
        "about": "Напиши новое описание:",
    }

    if action == "gender":

        await query.message.reply_text(
            prompts[action],
            reply_markup=gender_keyboard(),
        )

    elif action == "looking_for":

        await query.message.reply_text(
            prompts[action],
            reply_markup=looking_keyboard(),
        )

    else:

        await query.message.reply_text(
            prompts[action],
            reply_markup=ReplyKeyboardRemove(),
        )

    return EDIT_VALUE


async def save_edit_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    field = context.user_data.get(
        "edit_field"
    )

    if not field:

        return ConversationHandler.END

    text = update.message.text.strip()

    if field == "age":

        if not text.isdigit():

            await update.message.reply_text(
                "Возраст напиши цифрами."
            )

            return EDIT_VALUE

        value = int(text)

        if value < 18 or value > 100:

            await update.message.reply_text(
                "Возраст должен быть от 18 до 100."
            )

            return EDIT_VALUE

    elif field == "gender":

        if text not in (
            "👩 Девушка",
            "👨 Мужчина",
        ):

            await update.message.reply_text(
                "Выбери вариант кнопкой."
            )

            return EDIT_VALUE

        value = text

    elif field == "looking_for":

        if text not in (
            "👩 Девушку",
            "👨 Мужчину",
            "💞 Неважно",
        ):

            await update.message.reply_text(
                "Выбери вариант кнопкой."
            )

            return EDIT_VALUE

        value = text

    elif field == "about":

        if len(text) > 500:

            await update.message.reply_text(
                "Описание должно быть до 500 символов."
            )

            return EDIT_VALUE

        value = text

    else:

        value = text

    await update_profile_field(
        user_id,
        field,
        value,
    )

    context.user_data.pop(
        "edit_field",
        None,
    )

    await update.message.reply_text(
        "✅ Сохранено.",
        reply_markup=main_menu(),
    )

    await update.message.reply_text(
        "Изменить что-нибудь ещё?",
        reply_markup=edit_menu(),
    )

    return ConversationHandler.END


# ============================================================
# MEDIA EDIT
# ============================================================

async def media_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    action = query.data.split(
        ":",
        1
    )[1]

    user_id = query.from_user.id

    if action == "add":

        media = await get_media(
            user_id
        )

        if len(media) >= MAX_MEDIA:

            await query.message.reply_text(
                f"Уже добавлено максимум "
                f"{MAX_MEDIA} фото/видео."
            )

            return ConversationHandler.END

        await query.message.reply_text(
            "Отправь новое фото или видео 📸🎬\n\n"
            f"Сейчас: {len(media)}/{MAX_MEDIA}"
        )

        return ADD_MEDIA

    if action == "delete_last":

        success = await delete_last_media(
            user_id
        )

        if not success:

            await query.message.reply_text(
                "Главное фото удалить нельзя.\n\n"
                "В анкете должно остаться хотя бы одно фото."
            )

            return ConversationHandler.END

        media = await get_media(
            user_id
        )

        await query.message.reply_text(
            f"🗑 Последнее фото/видео удалено.\n\n"
            f"Осталось: {len(media)}/{MAX_MEDIA}",
            reply_markup=media_menu(),
        )

        return ConversationHandler.END

    if action == "back":

        await query.message.reply_text(
            "✏️ Редактирование анкеты",
            reply_markup=edit_menu(),
        )

        return ConversationHandler.END


async def receive_extra_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    media = await get_media(
        user_id
    )

    if len(media) >= MAX_MEDIA:

        await update.message.reply_text(
            f"Максимум {MAX_MEDIA} фото/видео.",
            reply_markup=main_menu(),
        )

        return ConversationHandler.END

    if update.message.photo:

        media_type = "photo"

        file_id = (
            update.message.photo[-1].file_id
        )

    elif update.message.video:

        media_type = "video"

        file_id = (
            update.message.video.file_id
        )

    else:

        await update.message.reply_text(
            "Нужно отправить фото или видео."
        )

        return ADD_MEDIA

    try:

        success = await add_media(
            user_id,
            media_type,
            file_id,
        )

    except Exception as e:

        logger.exception(
            "Add media failed: %s",
            e,
        )

        await update.message.reply_text(
            "Не удалось сохранить файл 😔\n"
            "Попробуй ещё раз."
        )

        return ADD_MEDIA

    if not success:

        await update.message.reply_text(
            f"Максимум {MAX_MEDIA} фото/видео."
        )

        return ConversationHandler.END

    media = await get_media(
        user_id
    )

    await update.message.reply_text(
        "✅ Добавлено!\n\n"
        f"Сейчас в анкете: "
        f"{len(media)}/{MAX_MEDIA}",
        reply_markup=main_menu(),
    )

    await update.message.reply_text(
        "Можно добавить ещё или вернуться назад.",
        reply_markup=media_menu(),
    )

    return ConversationHandler.END


# ============================================================
# DELETE PROFILE
# ============================================================

async def delete_profile_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Да, удалить",
                    callback_data="delete_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Отмена",
                    callback_data="delete_cancel",
                )
            ],
        ]
    )

    await query.message.reply_text(
        "Точно удалить анкету?\n\n"
        "Фото, видео, лайки и история будут удалены.",
        reply_markup=keyboard,
    )


async def delete_confirm_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    await delete_profile(
        query.from_user.id
    )

    context.user_data.clear()

    await query.message.reply_text(
        "🗑 Анкета удалена.",
        reply_markup=create_menu(),
    )


async def delete_cancel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "Удаление отменено.",
        reply_markup=main_menu(),
    )


# ============================================================
# BROWSE
# ============================================================

async def browse_profiles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    my_data = await get_profile(
        user_id
    )

    if not my_data:

        await update.message.reply_text(
            "Сначала создай свою анкету 💘",
            reply_markup=create_menu(),
        )

        return

    candidate = await get_next_profile(
        user_id
    )

    if not candidate:

        await update.message.reply_text(
            "Пока подходящих новых анкет нет 😌\n\n"
            "Попробуй немного позже.",
            reply_markup=main_menu(),
        )

        return

    await show_candidate(
        update.message,
        candidate,
    )


async def show_candidate(
    message,
    profile,
):

    # Сразу показываем ВСЮ анкету-альбом.
    await send_profile_album(
        message,
        profile,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👎 Пропустить",
                    callback_data=(
                        f"skip:{profile['user_id']}"
                    ),
                ),
                InlineKeyboardButton(
                    "❤️ Нравится",
                    callback_data=(
                        f"like:{profile['user_id']}"
                    ),
                ),
            ]
        ]
    )

    await message.reply_text(
        "💘 Твой выбор:",
        reply_markup=keyboard,
    )


# ============================================================
# LIKE / SKIP
# ============================================================

async def profile_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    action, target_id = (
        query.data.split(":")
    )

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

        if await is_match(
            user_id,
            target_id,
        ):

            my_data = await get_profile(
                user_id
            )

            target_data = await get_profile(
                target_id
            )

            if my_data and target_data:

                if target_data.get(
                    "username"
                ):

                    target_link = (
                        "@"
                        + escape(
                            target_data["username"]
                        )
                    )

                else:

                    target_link = (
                        f'<a href="tg://user?id={target_id}">'
                        f'{escape(target_data["name"])}'
                        f'</a>'
                    )

                if my_data.get(
                    "username"
                ):

                    my_link = (
                        "@"
                        + escape(
                            my_data["username"]
                        )
                    )

                else:

                    my_link = (
                        f'<a href="tg://user?id={user_id}">'
                        f'{escape(my_data["name"])}'
                        f'</a>'
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

    next_profile = await get_next_profile(
        user_id
    )

    if next_profile:

        await show_candidate(
            query.message,
            next_profile,
        )

    else:

        await query.message.reply_text(
            "Подходящие анкеты закончились 😊",
            reply_markup=main_menu(),
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    profile = await get_profile(
        update.effective_user.id
    )

    context.user_data.clear()

    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=(
            main_menu()
            if profile
            else create_menu()
        ),
    )

    return ConversationHandler.END


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Telegram handler error",
        exc_info=context.error,
    )


# ============================================================
# TELEGRAM APP
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing"
    )


application = (
    Application.builder()
    .token(TOKEN)
    .concurrent_updates(False)
    .build()
)


create_conversation = ConversationHandler(

    entry_points=[
        MessageHandler(
            filters.Regex(
                r"^💘 Создать анкету$"
            ),
            create_profile,
        )
    ],

    states={

        CREATE_NAME: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                create_name,
            )
        ],

        CREATE_AGE: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                create_age,
            )
        ],

        CREATE_CITY: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                create_city,
            )
        ],

        CREATE_GENDER: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                create_gender,
            )
        ],

        CREATE_LOOKING: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                create_looking,
            )
        ],

        CREATE_ABOUT: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                create_about,
            )
        ],

        CREATE_PHOTO: [
            MessageHandler(
                filters.PHOTO,
                create_first_photo,
            ),
            MessageHandler(
                ~filters.PHOTO
                & ~filters.COMMAND,
                create_first_photo,
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


edit_conversation = ConversationHandler(

    entry_points=[
        CallbackQueryHandler(
            edit_callback,
            pattern=(
                r"^edit:"
                r"(name|age|city|gender|looking_for|about)$"
            ),
        )
    ],

    states={

        EDIT_VALUE: [
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND,
                save_edit_value,
            )
        ],
    },

    fallbacks=[
        CommandHandler(
            "cancel",
            cancel,
        )
    ],
)


media_conversation = ConversationHandler(

    entry_points=[
        CallbackQueryHandler(
            media_callback,
            pattern=r"^media:add$",
        )
    ],

    states={

        ADD_MEDIA: [
            MessageHandler(
                (
                    filters.PHOTO
                    | filters.VIDEO
                )
                & ~filters.COMMAND,
                receive_extra_media,
            )
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
    create_conversation
)

application.add_handler(
    edit_conversation
)

application.add_handler(
    media_conversation
)

application.add_handler(
    MessageHandler(
        filters.Regex(
            r"^👤 Моя анкета$"
        ),
        my_profile,
    )
)

application.add_handler(
    MessageHandler(
        filters.Regex(
            r"^🔥 Смотреть анкеты$"
        ),
        browse_profiles,
    )
)

application.add_handler(
    MessageHandler(
        filters.Regex(
            r"^✏️ Редактировать анкету$"
        ),
        open_edit_menu,
    )
)

application.add_handler(
    CallbackQueryHandler(
        edit_callback,
        pattern=r"^edit:(media|done)$",
    )
)

application.add_handler(
    CallbackQueryHandler(
        media_callback,
        pattern=r"^media:(delete_last|back)$",
    )
)

application.add_handler(
    CallbackQueryHandler(
        delete_profile_callback,
        pattern=r"^profile_delete$",
    )
)

application.add_handler(
    CallbackQueryHandler(
        delete_confirm_callback,
        pattern=r"^delete_confirm$",
    )
)

application.add_handler(
    CallbackQueryHandler(
        delete_cancel_callback,
        pattern=r"^delete_cancel$",
    )
)

application.add_handler(
    CallbackQueryHandler(
        profile_action,
        pattern=r"^(like|skip):",
    )
)

application.add_error_handler(
    error_handler
)


# ============================================================
# WEBHOOK / RENDER
# ============================================================

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

    await application.process_update(
        update
    )

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

    if not render_url:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing"
        )

    webhook_url = (
        f"{render_url.rstrip('/')}/telegram"
    )

    await application.bot.set_webhook(
        webhook_url
    )

    logger.info(
        "Webhook set: %s",
        webhook_url,
    )

    logger.info(
        "OLIVKA MATCH started"
    )


async def on_cleanup(web_app):

    global db_pool

    try:
        await application.stop()
    except Exception:
        pass

    try:
        await application.shutdown()
    except Exception:
        pass

    if db_pool:
        await db_pool.close()


# ============================================================
# SERVER
# ============================================================

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
