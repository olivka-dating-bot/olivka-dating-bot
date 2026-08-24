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
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

MAX_MEDIA = 6

(
    CREATE_NAME,
    CREATE_AGE,
    CREATE_CITY,
    CREATE_GENDER,
    CREATE_LOOKING_FOR,
    CREATE_ABOUT,
    CREATE_MEDIA,
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
                photo TEXT,
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

        # Перенос старого главного фото в новую галерею.
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
            profile.get("photo"),
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
            SELECT id, media_type, file_id, sort_order
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

    next_order = len(media)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO profile_media (
                user_id,
                media_type,
                file_id,
                sort_order
            )
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            media_type,
            file_id,
            next_order,
        )

        if next_order == 0 and media_type == "photo":
            await conn.execute(
                """
                UPDATE profiles
                SET photo = $1
                WHERE user_id = $2
                """,
                file_id,
                user_id,
            )

    return True


async def delete_last_media(user_id):
    media = await get_media(user_id)

    if not media:
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

    media_after = await get_media(user_id)

    async with db_pool.acquire() as conn:
        first_photo = next(
            (
                item["file_id"]
                for item in media_after
                if item["media_type"] == "photo"
            ),
            None,
        )

        await conn.execute(
            """
            UPDATE profiles
            SET photo = $1
            WHERE user_id = $2
            """,
            first_photo,
            user_id,
        )

    return True


async def clear_media(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM profile_media
            WHERE user_id = $1
            """,
            user_id,
        )

        await conn.execute(
            """
            UPDATE profiles
            SET photo = NULL
            WHERE user_id = $1
            """,
            user_id,
        )


async def delete_profile(user_id):
    async with db_pool.acquire() as conn:
        async with conn.transaction():

            await conn.execute(
                "DELETE FROM profile_media WHERE user_id = $1",
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM likes
                WHERE from_user = $1 OR to_user = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM skips
                WHERE from_user = $1 OR to_user = $1
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
        value = await conn.fetchval(
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

    return bool(value)


# ============================================================
# FILTERING
# ============================================================

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
            LIMIT 100
            """,
            user_id,
        )

    for row in rows:
        profile = dict(row)

        media = await get_media(profile["user_id"])

        if not media:
            continue

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


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["🔥 Смотреть анкеты"],
            ["👤 Моя анкета", "✏️ Редактировать анкету"],
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
            ["👩 Девушка", "👨 Мужчина"],
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
                ),
            ],
            [
                InlineKeyboardButton(
                    "✨ О себе",
                    callback_data="edit:about",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📸 Фото и видео",
                    callback_data="edit:media",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑 Удалить анкету",
                    callback_data="profile_delete",
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Готово",
                    callback_data="edit:done",
                ),
            ],
        ]
    )


def media_menu():
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
                    "👀 Посмотреть галерею",
                    callback_data="media:view",
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
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    profile = await get_profile(user_id)

    if profile:
        await update.message.reply_text(
            "💗 Добро пожаловать в OLIVKA MATCH!\n\n"
            "Твоя анкета сохранена.",
            reply_markup=main_menu(),
        )
    else:
        await update.message.reply_text(
            "💗 Добро пожаловать в OLIVKA MATCH!\n\n"
            "Создай анкету и начинай знакомиться.",
            reply_markup=create_menu(),
        )


# ============================================================
# СОЗДАНИЕ АНКЕТЫ
# ============================================================

async def create_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "Как тебя зовут? 😊",
        reply_markup=ReplyKeyboardRemove(),
    )

    return CREATE_NAME


async def create_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text.strip()

    if len(text) < 2:
        await update.message.reply_text(
            "Напиши имя чуть подробнее 🙂"
        )
        return CREATE_NAME

    context.user_data["new_name"] = text

    await update.message.reply_text(
        "Сколько тебе лет?\n\n"
        "Только 18+ 🔞"
    )

    return CREATE_AGE


async def create_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
            "OLIVKA MATCH доступен только 18+ 🔞"
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
    context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_city"] = update.message.text.strip()

    await update.message.reply_text(
        "Кто ты?",
        reply_markup=gender_keyboard(),
    )

    return CREATE_GENDER


async def create_gender(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    gender = update.message.text.strip()

    if gender not in [
        "👩 Девушка",
        "👨 Мужчина",
    ]:
        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )
        return CREATE_GENDER

    context.user_data["new_gender"] = gender

    await update.message.reply_text(
        "Кого хочешь найти? 💘",
        reply_markup=looking_keyboard(),
    )

    return CREATE_LOOKING_FOR


async def create_looking_for(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    value = update.message.text.strip()

    if value not in [
        "👩 Девушку",
        "👨 Мужчину",
        "💞 Неважно",
    ]:
        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )
        return CREATE_LOOKING_FOR

    context.user_data["new_looking_for"] = value

    await update.message.reply_text(
        "Расскажи немного о себе ✨",
        reply_markup=ReplyKeyboardRemove(),
    )

    return CREATE_ABOUT


async def create_about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text.strip()

    if len(text) > 500:
        await update.message.reply_text(
            "Описание должно быть до 500 символов."
        )
        return CREATE_ABOUT

    context.user_data["new_about"] = text

    await update.message.reply_text(
        "Теперь отправь первое фото 📸\n\n"
        "После создания анкеты сможешь добавить ещё фото и видео."
    )

    return CREATE_MEDIA


async def create_first_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message.photo:
        await update.message.reply_text(
            "Первым медиа должна быть фотография 📸"
        )
        return CREATE_MEDIA

    user = update.effective_user
    photo_id = update.message.photo[-1].file_id

    profile = {
        "user_id": user.id,
        "username": user.username,
        "name": context.user_data["new_name"],
        "age": context.user_data["new_age"],
        "city": context.user_data["new_city"],
        "gender": context.user_data["new_gender"],
        "looking_for": context.user_data["new_looking_for"],
        "about": context.user_data["new_about"],
        "photo": photo_id,
    }

    await save_profile(profile)
    await clear_media(user.id)
    await add_media(
        user.id,
        "photo",
        photo_id,
    )

    await update.message.reply_text(
        "💗 Анкета создана!\n\n"
        "Теперь можно добавить дополнительные фото или видео через "
        "«✏️ Редактировать анкету».",
        reply_markup=main_menu(),
    )

    await send_own_profile(
        update.message,
        user.id,
    )

    return ConversationHandler.END


# ============================================================
# ПРОСМОТР СВОЕЙ АНКЕТЫ
# ============================================================

async def send_own_profile(message, user_id):
    profile = await get_profile(user_id)

    if not profile:
        await message.reply_text(
            "У тебя пока нет анкеты 💗"
        )
        return

    caption = (
        f"💗 <b>{escape(profile['name'])}, {profile['age']}</b>\n"
        f"📍 {escape(profile['city'])}\n"
        f"{escape(profile['gender'])}\n"
        f"🔎 Ищу: {escape(profile['looking_for'])}\n\n"
        f"✨ {escape(profile['about'])}"
    )

    media = await get_media(user_id)

    if not media:
        await message.reply_text(
            caption,
            parse_mode="HTML",
        )
        return

    first = media[0]

    if first["media_type"] == "photo":
        await message.reply_photo(
            photo=first["file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    else:
        await message.reply_video(
            video=first["file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


async def my_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await send_own_profile(
        update.message,
        update.effective_user.id,
    )


# ============================================================
# РЕДАКТИРОВАНИЕ
# ============================================================

async def open_edit_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    action = query.data.split(":", 1)[1]

    if action == "done":
        await query.message.reply_text(
            "✅ Изменения сохранены.",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    if action == "media":
        await query.message.reply_text(
            "📸 Фото и видео",
            reply_markup=media_menu(),
        )
        return ConversationHandler.END

    context.user_data["edit_field"] = action

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
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id
    field = context.user_data.get("edit_field")

    if not field:
        return ConversationHandler.END

    text = update.message.text.strip()

    if field == "age":
        if not text.isdigit():
            await update.message.reply_text(
                "Возраст нужно написать цифрами."
            )
            return EDIT_VALUE

        value = int(text)

        if value < 18 or value > 100:
            await update.message.reply_text(
                "Допустимый возраст: от 18 до 100."
            )
            return EDIT_VALUE

    elif field == "gender":
        if text not in [
            "👩 Девушка",
            "👨 Мужчина",
        ]:
            await update.message.reply_text(
                "Выбери вариант кнопкой."
            )
            return EDIT_VALUE

        value = text

    elif field == "looking_for":
        if text not in [
            "👩 Девушку",
            "👨 Мужчину",
            "💞 Неважно",
        ]:
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

    await update.message.reply_text(
        "✅ Сохранено.",
        reply_markup=main_menu(),
    )

    await update.message.reply_text(
        "Продолжить редактирование?",
        reply_markup=edit_menu(),
    )

    return ConversationHandler.END


# ============================================================
# MEDIA
# ============================================================

async def media_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    action = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    if action == "add":
        media = await get_media(user_id)

        if len(media) >= MAX_MEDIA:
            await query.message.reply_text(
                f"В анкете уже максимум {MAX_MEDIA} фото/видео."
            )
            return ConversationHandler.END

        await query.message.reply_text(
            f"Отправь фото или видео.\n\n"
            f"Можно хранить до {MAX_MEDIA} медиа."
        )

        return ADD_MEDIA

    if action == "view":
        await send_gallery(
            query.message,
            user_id,
        )
        return ConversationHandler.END

    if action == "delete_last":
        media = await get_media(user_id)

        if len(media) <= 1:
            await query.message.reply_text(
                "В анкете должно остаться хотя бы одно фото."
            )
            return ConversationHandler.END

        deleted = await delete_last_media(user_id)

        if deleted:
            await query.message.reply_text(
                "🗑 Последнее фото/видео удалено.",
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
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    media = await get_media(user_id)

    if len(media) >= MAX_MEDIA:
        await update.message.reply_text(
            f"Максимум {MAX_MEDIA} фото/видео.",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    if update.message.photo:
        media_type = "photo"
        file_id = update.message.photo[-1].file_id

    elif update.message.video:
        media_type = "video"
        file_id = update.message.video.file_id

    else:
        await update.message.reply_text(
            "Отправь фотографию или видео."
        )
        return ADD_MEDIA

    await add_media(
        user_id,
        media_type,
        file_id,
    )

    count = len(
        await get_media(user_id)
    )

    await update.message.reply_text(
        f"✅ Добавлено.\n\n"
        f"Сейчас в анкете: {count}/{MAX_MEDIA}",
        reply_markup=main_menu(),
    )

    await update.message.reply_text(
        "Что дальше?",
        reply_markup=media_menu(),
    )

    return ConversationHandler.END


async def send_gallery(message, user_id):
    media = await get_media(user_id)

    if not media:
        await message.reply_text(
            "В галерее пока ничего нет."
        )
        return

    if len(media) == 1:
        item = media[0]

        if item["media_type"] == "photo":
            await message.reply_photo(
                photo=item["file_id"],
                caption="📸 Галерея анкеты"
            )
        else:
            await message.reply_video(
                video=item["file_id"],
                caption="🎬 Галерея анкеты"
            )

        return

    media_group = []

    for i, item in enumerate(media[:MAX_MEDIA]):
        caption = "📸 Галерея анкеты" if i == 0 else None

        if item["media_type"] == "photo":
            media_group.append(
                InputMediaPhoto(
                    media=item["file_id"],
                    caption=caption,
                )
            )
        else:
            media_group.append(
                InputMediaVideo(
                    media=item["file_id"],
                    caption=caption,
                )
            )

    try:
        await message.reply_media_group(
            media=media_group,
        )
    except Exception:
        # Если Telegram не примет смешанную группу,
        # отправляем по одному.
        for item in media:
            if item["media_type"] == "photo":
                await message.reply_photo(
                    photo=item["file_id"]
                )
            else:
                await message.reply_video(
                    video=item["file_id"]
                )


# ============================================================
# DELETE PROFILE
# ============================================================

async def delete_profile_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Да, удалить",
                    callback_data="delete_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Отмена",
                    callback_data="delete_cancel"
                )
            ],
        ]
    )

    await query.message.reply_text(
        "Удалить анкету полностью?\n\n"
        "Все фото, видео, лайки и совпадения будут удалены.",
        reply_markup=keyboard,
    )


async def delete_confirm_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    await delete_profile(user_id)

    await query.message.reply_text(
        "🗑 Анкета удалена.",
        reply_markup=create_menu(),
    )


async def delete_cancel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "Удаление отменено.",
        reply_markup=main_menu(),
    )


# ============================================================
# ПРОСМОТР ЧУЖИХ АНКЕТ
# ============================================================

async def browse_profiles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    profile = await get_profile(user_id)

    if not profile:
        await update.message.reply_text(
            "Сначала создай анкету 💘",
            reply_markup=create_menu(),
        )
        return

    candidate = await get_next_profile(user_id)

    if not candidate:
        await update.message.reply_text(
            "Пока подходящих новых анкет нет 😌",
            reply_markup=main_menu(),
        )
        return

    await show_candidate(
        update,
        candidate,
    )


async def show_candidate(update, profile):
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
            ],
            [
                InlineKeyboardButton(
                    "📸 Галерея",
                    callback_data=f"gallery:{profile['user_id']}",
                )
            ]
        ]
    )

    media = await get_media(
        profile["user_id"]
    )

    if not media:
        return

    first = media[0]

    if update.callback_query:
        message = update.callback_query.message
    else:
        message = update.message

    if first["media_type"] == "photo":
        await message.reply_photo(
            photo=first["file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await message.reply_video(
            video=first["file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def gallery_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    user_id = int(
        query.data.split(":")[1]
    )

    await send_gallery(
        query.message,
        user_id,
    )


# ============================================================
# LIKE / SKIP
# ============================================================

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

    if action == "like":
        await add_like(
            user_id,
            target_id,
        )

        if await is_match(
            user_id,
            target_id,
        ):
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
                        f"Написать: {target_link}"
                    ),
                    parse_mode="HTML",
                )

                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "💞 <b>У ВАС СОВПАДЕНИЕ!</b>\n\n"
                        f"Написать: {my_link}"
                    ),
                    parse_mode="HTML",
                )

        else:
            await query.message.reply_text(
                "❤️ Лайк отправлен!"
            )

    candidate = await get_next_profile(user_id)

    if candidate:
        await show_candidate(
            update,
            candidate,
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
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=main_menu(),
    )

    return ConversationHandler.END


# ============================================================
# APPLICATION
# ============================================================

application = (
    Application.builder()
    .token(TOKEN)
    .concurrent_updates(False)
    .build()
)


create_conversation = ConversationHandler(
    entry_points=[
        MessageHandler(
            filters.Regex("^💘 Создать анкету$"),
            create_profile,
        )
    ],

    states={
        CREATE_NAME: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                create_name,
            )
        ],

        CREATE_AGE: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                create_age,
            )
        ],

        CREATE_CITY: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                create_city,
            )
        ],

        CREATE_GENDER: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                create_gender,
            )
        ],

        CREATE_LOOKING_FOR: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                create_looking_for,
            )
        ],

        CREATE_ABOUT: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                create_about,
            )
        ],

        CREATE_MEDIA: [
            MessageHandler(
                filters.PHOTO,
                create_first_media,
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


edit_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(
            edit_callback,
            pattern=r"^edit:(name|age|city|gender|looking_for|about)$",
        )
    ],

    states={
        EDIT_VALUE: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
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
                (filters.PHOTO | filters.VIDEO)
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
    MessageHandler(
        filters.Regex("^✏️ Редактировать анкету$"),
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
        pattern=r"^media:(view|delete_last|back)$",
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

application.add_handler(
    CallbackQueryHandler(
        gallery_callback,
        pattern=r"^gallery:",
    )
)


# ============================================================
# WEBHOOK
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
