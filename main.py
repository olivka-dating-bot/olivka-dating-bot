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
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("olivka-match")


# ============================================================
# SETTINGS
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

PORT = int(
    os.getenv("PORT", "10000")
)

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(
    os.getenv("DB_PORT", "5432")
)
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

MAX_MEDIA = 6

# Сколько существующих анкет автоматически
# показать человеку после регистрации.
INITIAL_AUTO_PROFILES = 3


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

        await conn.execute(
            """
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
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_media (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS likes (
                from_user BIGINT NOT NULL,
                to_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_user, to_user)
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skips (
                from_user BIGINT NOT NULL,
                to_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_user, to_user)
            )
            """
        )

        # Отдельная таблица реальных мэтчей.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                user1 BIGINT NOT NULL,
                user2 BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user1, user2)
            )
            """
        )

        # Помнит, кому бот уже автоматически
        # отправлял конкретную анкету.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_deliveries (
                recipient_user BIGINT NOT NULL,
                profile_user BIGINT NOT NULL,
                delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    recipient_user,
                    profile_user
                )
            )
            """
        )

        # Перенос старого главного фото в галерею.
        await conn.execute(
            """
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
            """
        )

    logger.info("Database initialized")


# ============================================================
# PROFILE DATABASE
# ============================================================

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


async def update_profile_field(
    user_id,
    field,
    value,
):
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


# ============================================================
# MEDIA DATABASE
# ============================================================

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
            ORDER BY
                sort_order ASC,
                id ASC
            """,
            user_id,
        )

    return [
        dict(row)
        for row in rows
    ]


async def add_media(
    user_id,
    media_type,
    file_id,
):

    current = await get_media(
        user_id
    )

    if len(current) >= MAX_MEDIA:
        return False

    sort_order = len(current)

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


async def replace_with_first_photo(
    user_id,
    file_id,
):
    """
    Главное фото никогда не превращаем в NULL.
    Это важно для старой базы, где photo = NOT NULL.
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
                VALUES (
                    $1,
                    'photo',
                    $2,
                    0
                )
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


async def delete_last_media(
    user_id
):
    media = await get_media(
        user_id
    )

    # Главное фото оставляем.
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


# ============================================================
# LIKES / SKIPS / MATCHES
# ============================================================

async def add_like(
    from_user,
    to_user,
):

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

        # Если раньше человек нажал "пропустить",
        # а теперь somehow поставил лайк —
        # пропуск убираем.
        await conn.execute(
            """
            DELETE FROM skips
            WHERE from_user = $1
              AND to_user = $2
            """,
            from_user,
            to_user,
        )


async def add_skip(
    from_user,
    to_user,
):

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


async def create_match_if_mutual(
    user_a,
    user_b,
):
    """
    Возвращает True ТОЛЬКО если:

    1. user_a лайкнул user_b
    2. user_b лайкнул user_a
    3. такой MATCH ещё не создавался
    """

    first = min(
        user_a,
        user_b,
    )

    second = max(
        user_a,
        user_b,
    )

    async with db_pool.acquire() as conn:

        async with conn.transaction():

            a_likes_b = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM likes
                    WHERE from_user = $1
                      AND to_user = $2
                )
                """,
                user_a,
                user_b,
            )

            b_likes_a = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM likes
                    WHERE from_user = $1
                      AND to_user = $2
                )
                """,
                user_b,
                user_a,
            )

            if not (
                a_likes_b
                and b_likes_a
            ):
                return False

            new_match = await conn.fetchrow(
                """
                INSERT INTO matches (
                    user1,
                    user2
                )
                VALUES ($1,$2)

                ON CONFLICT DO NOTHING

                RETURNING
                    user1,
                    user2
                """,
                first,
                second,
            )

            # None = мэтч уже был раньше.
            return new_match is not None


# ============================================================
# AUTO DELIVERY
# ============================================================

async def already_delivered(
    recipient_user,
    profile_user,
):

    async with db_pool.acquire() as conn:

        result = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM profile_deliveries
                WHERE recipient_user = $1
                  AND profile_user = $2
            )
            """,
            recipient_user,
            profile_user,
        )

    return bool(result)


async def mark_delivered(
    recipient_user,
    profile_user,
):

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO profile_deliveries (
                recipient_user,
                profile_user
            )
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
            """,
            recipient_user,
            profile_user,
        )


# ============================================================
# DELETE PROFILE
# ============================================================

async def delete_profile(
    user_id
):

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
                DELETE FROM matches
                WHERE user1 = $1
                   OR user2 = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM profile_deliveries
                WHERE recipient_user = $1
                   OR profile_user = $1
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


# ============================================================
# COMPATIBILITY
# ============================================================

def gender_matches(
    looking_for,
    candidate_gender,
):

    if looking_for == "💞 Неважно":
        return True

    if looking_for == "👩 Девушку":
        return (
            candidate_gender
            == "👩 Девушка"
        )

    if looking_for == "👨 Мужчину":
        return (
            candidate_gender
            == "👨 Мужчина"
        )

    return True


def candidate_accepts(
    candidate_looking_for,
    viewer_gender,
):

    if (
        candidate_looking_for
        == "💞 Неважно"
    ):
        return True

    if (
        candidate_looking_for
        == "👩 Девушку"
    ):
        return (
            viewer_gender
            == "👩 Девушка"
        )

    if (
        candidate_looking_for
        == "👨 Мужчину"
    ):
        return (
            viewer_gender
            == "👨 Мужчина"
        )

    return True


def profiles_are_compatible(
    profile_a,
    profile_b,
):

    return (
        gender_matches(
            profile_a["looking_for"],
            profile_b["gender"],
        )
        and
        candidate_accepts(
            profile_b["looking_for"],
            profile_a["gender"],
        )
    )


# ============================================================
# FIND PROFILES
# ============================================================

async def get_next_profile(
    user_id
):

    viewer = await get_profile(
        user_id
    )

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

            ORDER BY
                p.updated_at DESC

            LIMIT 100
            """,
            user_id,
        )

    for row in rows:

        candidate = dict(row)

        media = await get_media(
            candidate["user_id"]
        )

        if not media:
            continue

        if not profiles_are_compatible(
            viewer,
            candidate,
        ):
            continue

        return candidate

    return None


async def get_compatible_profiles_for_user(
    user_id,
    limit=None,
):

    viewer = await get_profile(
        user_id
    )

    if not viewer:
        return []

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT *
            FROM profiles
            WHERE user_id <> $1
            ORDER BY updated_at DESC
            """,
            user_id,
        )

    result = []

    for row in rows:

        candidate = dict(row)

        if not profiles_are_compatible(
            viewer,
            candidate,
        ):
            continue

        media = await get_media(
            candidate["user_id"]
        )

        if not media:
            continue

        result.append(
            candidate
        )

        if (
            limit is not None
            and len(result) >= limit
        ):
            break

    return result


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():

    return ReplyKeyboardMarkup(
        [
            [
                "🔥 Смотреть анкеты"
            ],
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
            [
                "💘 Создать анкету"
            ]
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
                ),
            ],

            [
                InlineKeyboardButton(
                    "🗑 Удалить последнее",
                    callback_data="media:delete_last",
                ),
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="media:back",
                ),
            ],
        ]
    )


def like_keyboard(
    profile_user_id
):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👎 Пропустить",
                    callback_data=(
                        f"skip:{profile_user_id}"
                    ),
                ),

                InlineKeyboardButton(
                    "❤️ Нравится",
                    callback_data=(
                        f"like:{profile_user_id}"
                    ),
                ),
            ]
        ]
    )


def contact_keyboard(
    profile
):
    username = profile.get(
        "username"
    )

    if username:

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💬 Написать",
                        url=(
                            f"https://t.me/"
                            f"{username}"
                        ),
                    )
                ]
            ]
        )

    # Если username у человека нет,
    # Telegram всё равно умеет открыть пользователя
    # по tg://user?id=...
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💬 Написать",
                    url=(
                        "tg://user?id="
                        f"{profile['user_id']}"
                    ),
                )
            ]
        ]
    )


# ============================================================
# PROFILE TEXT
# ============================================================

def profile_caption(
    profile
):

    return (
        f"💗 <b>"
        f"{escape(profile['name'])}, "
        f"{profile['age']}"
        f"</b>\n"

        f"📍 "
        f"{escape(profile['city'])}\n"

        f"{escape(profile['gender'])}\n"

        f"🔎 Ищу: "
        f"{escape(profile['looking_for'])}\n\n"

        f"✨ "
        f"{escape(profile['about'])}"
    )


# ============================================================
# SEND PROFILE ALBUM
# ============================================================

async def send_profile_album(
    bot,
    chat_id,
    profile,
):

    media = await get_media(
        profile["user_id"]
    )

    caption = profile_caption(
        profile
    )

    if not media:

        if profile.get("photo"):

            await bot.send_photo(
                chat_id=chat_id,
                photo=profile["photo"],
                caption=caption,
                parse_mode="HTML",
            )

        else:

            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
            )

        return

    # Один файл.
    if len(media) == 1:

        item = media[0]

        if (
            item["media_type"]
            == "photo"
        ):

            await bot.send_photo(
                chat_id=chat_id,
                photo=item["file_id"],
                caption=caption,
                parse_mode="HTML",
            )

        else:

            await bot.send_video(
                chat_id=chat_id,
                video=item["file_id"],
                caption=caption,
                parse_mode="HTML",
            )

        return

    # Несколько файлов = Telegram-альбом.
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

        if (
            item["media_type"]
            == "photo"
        ):

            album.append(
                InputMediaPhoto(
                    media=item["file_id"],
                    caption=item_caption,
                    parse_mode=item_parse_mode,
                )
            )

        elif (
            item["media_type"]
            == "video"
        ):

            album.append(
                InputMediaVideo(
                    media=item["file_id"],
                    caption=item_caption,
                    parse_mode=item_parse_mode,
                )
            )

    try:

        await bot.send_media_group(
            chat_id=chat_id,
            media=album,
        )

    except Exception as error:

        logger.exception(
            "Album error: %s",
            error,
        )

        # Запасной режим.
        for index, item in enumerate(
            media[:MAX_MEDIA]
        ):

            item_caption = (
                caption
                if index == 0
                else None
            )

            if (
                item["media_type"]
                == "photo"
            ):

                await bot.send_photo(
                    chat_id=chat_id,
                    photo=item["file_id"],
                    caption=item_caption,
                    parse_mode=(
                        "HTML"
                        if item_caption
                        else None
                    ),
                )

            else:

                await bot.send_video(
                    chat_id=chat_id,
                    video=item["file_id"],
                    caption=item_caption,
                    parse_mode=(
                        "HTML"
                        if item_caption
                        else None
                    ),
                )


# ============================================================
# SEND PROFILE WITH LIKE BUTTONS
# ============================================================

async def send_profile_for_choice(
    bot,
    chat_id,
    profile,
    header=None,
):

    if header:

        await bot.send_message(
            chat_id=chat_id,
            text=header,
        )

    await send_profile_album(
        bot,
        chat_id,
        profile,
    )

    await bot.send_message(
        chat_id=chat_id,
        text="💘 Твой выбор:",
        reply_markup=like_keyboard(
            profile["user_id"]
        ),
    )


# ============================================================
# AUTOMATIC PROFILE DELIVERY
# ============================================================

async def send_initial_profiles(
    bot,
    user_id,
):

    """
    После создания анкеты новый пользователь
    автоматически получает несколько
    существующих подходящих анкет.
    """

    profiles = (
        await
        get_compatible_profiles_for_user(
            user_id,
            limit=INITIAL_AUTO_PROFILES,
        )
    )

    for profile in profiles:

        delivered = await already_delivered(
            user_id,
            profile["user_id"],
        )

        if delivered:
            continue

        try:

            await send_profile_for_choice(
                bot,
                user_id,
                profile,
                header=(
                    "🔥 Нашла подходящую "
                    "анкету для тебя"
                ),
            )

            await mark_delivered(
                user_id,
                profile["user_id"],
            )

        except Exception as error:

            logger.exception(
                "Initial delivery failed: %s",
                error,
            )


async def notify_users_about_new_profile(
    bot,
    new_user_id,
):

    """
    Когда новый человек зарегистрировался,
    его анкета автоматически приходит
    подходящим существующим пользователям.
    """

    newcomer = await get_profile(
        new_user_id
    )

    if not newcomer:
        return

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT *
            FROM profiles
            WHERE user_id <> $1
            """,
            new_user_id,
        )

    for row in rows:

        recipient = dict(row)

        if not profiles_are_compatible(
            recipient,
            newcomer,
        ):
            continue

        delivered = await already_delivered(
            recipient["user_id"],
            newcomer["user_id"],
        )

        if delivered:
            continue

        try:

            await send_profile_for_choice(
                bot,
                recipient["user_id"],
                newcomer,
                header=(
                    "🔥 Новая анкета "
                    "для тебя!"
                ),
            )

            await mark_delivered(
                recipient["user_id"],
                newcomer["user_id"],
            )

        except Exception as error:

            # Например пользователь заблокировал бота.
            logger.warning(
                "Cannot deliver profile to %s: %s",
                recipient["user_id"],
                error,
            )


# ============================================================
# MATCH NOTIFICATION
# ============================================================

async def send_match_notifications(
    bot,
    user_a,
    user_b,
):

    profile_a = await get_profile(
        user_a
    )

    profile_b = await get_profile(
        user_b
    )

    if (
        not profile_a
        or not profile_b
    ):
        return

    # ---------- A получает B ----------

    try:

        await bot.send_message(
            chat_id=user_a,
            text=(
                "💞💞💞 "
                "<b>ЭТО MATCH!</b> "
                "💞💞💞\n\n"
                "Вы понравились друг другу ❤️"
            ),
            parse_mode="HTML",
        )

        await send_profile_album(
            bot,
            user_a,
            profile_b,
        )

        await bot.send_message(
            chat_id=user_a,
            text=(
                "🔥 Симпатия взаимна!\n"
                "Можно знакомиться 😏"
            ),
            reply_markup=contact_keyboard(
                profile_b
            ),
        )

    except Exception as error:

        logger.exception(
            "Match notify A failed: %s",
            error,
        )

    # ---------- B получает A ----------

    try:

        await bot.send_message(
            chat_id=user_b,
            text=(
                "💞💞💞 "
                "<b>ЭТО MATCH!</b> "
                "💞💞💞\n\n"
                "Вы понравились друг другу ❤️"
            ),
            parse_mode="HTML",
        )

        await send_profile_album(
            bot,
            user_b,
            profile_a,
        )

        await bot.send_message(
            chat_id=user_b,
            text=(
                "🔥 Симпатия взаимна!\n"
                "Можно знакомиться 😏"
            ),
            reply_markup=contact_keyboard(
                profile_a
            ),
        )

    except Exception as error:

        logger.exception(
            "Match notify B failed: %s",
            error,
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
            "💗 Добро пожаловать "
            "в OLIVKA MATCH!\n\n"
            "Твоя анкета на месте ✅",
            reply_markup=main_menu(),
        )

    else:

        await update.message.reply_text(
            "💗 Добро пожаловать "
            "в OLIVKA MATCH!\n\n"
            "Создай анкету и "
            "начинай знакомиться.",
            reply_markup=create_menu(),
        )


# ============================================================
# CREATE PROFILE
# ============================================================

async def create_profile_start(
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

    name = (
        update.message.text
        .strip()
    )

    if len(name) < 2:

        await update.message.reply_text(
            "Напиши имя чуть подробнее 🙂"
        )

        return CREATE_NAME

    context.user_data[
        "new_name"
    ] = name

    await update.message.reply_text(
        "Сколько тебе лет?\n\n"
        "Только 18+ 🔞"
    )

    return CREATE_AGE


async def create_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        update.message.text
        .strip()
    )

    if not text.isdigit():

        await update.message.reply_text(
            "Напиши возраст цифрами 🙂"
        )

        return CREATE_AGE

    age = int(text)

    if age < 18:

        await update.message.reply_text(
            "OLIVKA MATCH "
            "доступен только 18+ 🔞",
            reply_markup=create_menu(),
        )

        return ConversationHandler.END

    if age > 100:

        await update.message.reply_text(
            "Проверь возраст 🙂"
        )

        return CREATE_AGE

    context.user_data[
        "new_age"
    ] = age

    await update.message.reply_text(
        "Из какого ты города? 📍"
    )

    return CREATE_CITY


async def create_city(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data[
        "new_city"
    ] = (
        update.message.text
        .strip()
    )

    await update.message.reply_text(
        "Кто ты?",
        reply_markup=gender_keyboard(),
    )

    return CREATE_GENDER


async def create_gender(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    gender = (
        update.message.text
        .strip()
    )

    if gender not in (
        "👩 Девушка",
        "👨 Мужчина",
    ):

        await update.message.reply_text(
            "Выбери вариант кнопкой 👇"
        )

        return CREATE_GENDER

    context.user_data[
        "new_gender"
    ] = gender

    await update.message.reply_text(
        "Кого хочешь найти? 💘",
        reply_markup=looking_keyboard(),
    )

    return CREATE_LOOKING


async def create_looking(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    looking = (
        update.message.text
        .strip()
    )

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
        "Расскажи немного "
        "о себе ✨",
        reply_markup=ReplyKeyboardRemove(),
    )

    return CREATE_ABOUT


async def create_about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    about = (
        update.message.text
        .strip()
    )

    if len(about) > 500:

        await update.message.reply_text(
            "Описание должно быть "
            "до 500 символов."
        )

        return CREATE_ABOUT

    context.user_data[
        "new_about"
    ] = about

    await update.message.reply_text(
        "Теперь отправь "
        "главное фото 📸\n\n"
        "После создания анкеты "
        "можно добавить ещё "
        "фото и видео."
    )

    return CREATE_PHOTO


async def create_first_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message.photo:

        await update.message.reply_text(
            "Нужно отправить "
            "именно фотографию 📸"
        )

        return CREATE_PHOTO

    user = update.effective_user

    photo_id = (
        update.message
        .photo[-1]
        .file_id
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

        await save_profile(
            profile
        )

        await replace_with_first_photo(
            user.id,
            photo_id,
        )

    except Exception as error:

        logger.exception(
            "Create profile error: %s",
            error,
        )

        await update.message.reply_text(
            "Не удалось сохранить "
            "анкету 😔\n"
            "Попробуй отправить фото "
            "ещё раз."
        )

        return CREATE_PHOTO

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Анкета сохранена!\n\n"
        "Теперь OLIVKA MATCH "
        "сам будет присылать "
        "подходящие новые анкеты 🔥",
        reply_markup=main_menu(),
    )

    saved = await get_profile(
        user.id
    )

    await send_profile_album(
        context.bot,
        user.id,
        saved,
    )

    # Новому пользователю отправляем
    # существующие подходящие анкеты.
    await send_initial_profiles(
        context.bot,
        user.id,
    )

    # А существующим подходящим людям
    # отправляем нового пользователя.
    await notify_users_about_new_profile(
        context.bot,
        user.id,
    )

    return ConversationHandler.END


# ============================================================
# MY PROFILE
# ============================================================

async def my_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    profile = await get_profile(
        user_id
    )

    if not profile:

        await update.message.reply_text(
            "У тебя пока нет "
            "анкеты 💗",
            reply_markup=create_menu(),
        )

        return

    await send_profile_album(
        context.bot,
        user_id,
        profile,
    )


# ============================================================
# BROWSE
# ============================================================

async def browse_profiles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    profile = await get_profile(
        user_id
    )

    if not profile:

        await update.message.reply_text(
            "Сначала создай "
            "свою анкету 💘",
            reply_markup=create_menu(),
        )

        return

    candidate = await get_next_profile(
        user_id
    )

    if not candidate:

        await update.message.reply_text(
            "Пока подходящих "
            "новых анкет нет 😌\n\n"
            "Новые анкеты будут "
            "приходить автоматически.",
            reply_markup=main_menu(),
        )

        return

    await send_profile_for_choice(
        context.bot,
        user_id,
        candidate,
    )

    await mark_delivered(
        user_id,
        candidate["user_id"],
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

    try:

        action, target_text = (
            query.data.split(
                ":",
                1,
            )
        )

        target_id = int(
            target_text
        )

    except Exception:

        return

    user_id = query.from_user.id

    # Убираем старые кнопки,
    # чтобы случайно не нажать дважды.
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

        await query.message.reply_text(
            "👎 Пропущено"
        )

    elif action == "like":

        await add_like(
            user_id,
            target_id,
        )

        # Надёжная проверка двух лайков.
        new_match = (
            await create_match_if_mutual(
                user_id,
                target_id,
            )
        )

        if new_match:

            # MATCH автоматически
            # отправляется обоим.
            await send_match_notifications(
                context.bot,
                user_id,
                target_id,
            )

        else:

            await query.message.reply_text(
                "❤️ Лайк отправлен!"
            )

    # После действия сразу пытаемся
    # показать следующую анкету.
    candidate = await get_next_profile(
        user_id
    )

    if candidate:

        await send_profile_for_choice(
            context.bot,
            user_id,
            candidate,
        )

        await mark_delivered(
            user_id,
            candidate["user_id"],
        )

    else:

        await query.message.reply_text(
            "На данный момент "
            "подходящие анкеты закончились 😊\n\n"
            "Я пришлю новые автоматически.",
            reply_markup=main_menu(),
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
            "Сначала создай "
            "анкету 💘",
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

    action = (
        query.data
        .split(
            ":",
            1,
        )[1]
    )

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
            "📸 Фото и видео\n\n"
            f"Сейчас: "
            f"{len(media)}/"
            f"{MAX_MEDIA}",
            reply_markup=media_menu(),
        )

        return ConversationHandler.END

    context.user_data[
        "edit_field"
    ] = action

    prompts = {
        "name":
            "Напиши новое имя:",

        "age":
            "Напиши новый возраст:",

        "city":
            "Напиши новый город:",

        "gender":
            "Выбери пол:",

        "looking_for":
            "Кого хочешь найти?",

        "about":
            "Напиши новое описание:",
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

    user_id = (
        update.effective_user.id
    )

    field = (
        context.user_data
        .get("edit_field")
    )

    if not field:

        return ConversationHandler.END

    text = (
        update.message.text
        .strip()
    )

    if field == "age":

        if not text.isdigit():

            await update.message.reply_text(
                "Возраст напиши "
                "цифрами."
            )

            return EDIT_VALUE

        value = int(text)

        if (
            value < 18
            or value > 100
        ):

            await update.message.reply_text(
                "Возраст должен быть "
                "от 18 до 100."
            )

            return EDIT_VALUE

    elif field == "gender":

        if text not in (
            "👩 Девушка",
            "👨 Мужчина",
        ):

            await update.message.reply_text(
                "Выбери вариант "
                "кнопкой."
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
                "Выбери вариант "
                "кнопкой."
            )

            return EDIT_VALUE

        value = text

    elif field == "about":

        if len(text) > 500:

            await update.message.reply_text(
                "Описание должно быть "
                "до 500 символов."
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

    action = (
        query.data
        .split(
            ":",
            1,
        )[1]
    )

    user_id = (
        query.from_user.id
    )

    if action == "add":

        media = await get_media(
            user_id
        )

        if (
            len(media)
            >= MAX_MEDIA
        ):

            await query.message.reply_text(
                f"Уже добавлено максимум "
                f"{MAX_MEDIA} фото/видео."
            )

            return ConversationHandler.END

        await query.message.reply_text(
            "Отправь новое "
            "фото или видео 📸🎬\n\n"
            f"Сейчас: "
            f"{len(media)}/{MAX_MEDIA}"
        )

        return ADD_MEDIA

    if action == "delete_last":

        success = (
            await delete_last_media(
                user_id
            )
        )

        if not success:

            await query.message.reply_text(
                "Главное фото удалить "
                "нельзя.\n\n"
                "В анкете должно "
                "остаться хотя бы "
                "одно фото."
            )

            return ConversationHandler.END

        media = await get_media(
            user_id
        )

        await query.message.reply_text(
            "🗑 Последнее фото/видео "
            "удалено.\n\n"
            f"Осталось: "
            f"{len(media)}/{MAX_MEDIA}",
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

    user_id = (
        update.effective_user.id
    )

    media = await get_media(
        user_id
    )

    if len(media) >= MAX_MEDIA:

        await update.message.reply_text(
            f"Максимум "
            f"{MAX_MEDIA} фото/видео.",
            reply_markup=main_menu(),
        )

        return ConversationHandler.END

    if update.message.photo:

        media_type = "photo"

        file_id = (
            update.message
            .photo[-1]
            .file_id
        )

    elif update.message.video:

        media_type = "video"

        file_id = (
            update.message
            .video
            .file_id
        )

    else:

        await update.message.reply_text(
            "Нужно отправить "
            "фото или видео."
        )

        return ADD_MEDIA

    try:

        success = await add_media(
            user_id,
            media_type,
            file_id,
        )

    except Exception as error:

        logger.exception(
            "Add media error: %s",
            error,
        )

        await update.message.reply_text(
            "Не удалось сохранить "
            "файл 😔\n"
            "Попробуй ещё раз."
        )

        return ADD_MEDIA

    if not success:

        await update.message.reply_text(
            f"Максимум "
            f"{MAX_MEDIA} фото/видео."
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
        "Можно добавить ещё "
        "или вернуться назад.",
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
                    callback_data=(
                        "delete_confirm"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Отмена",
                    callback_data=(
                        "delete_cancel"
                    ),
                )
            ],
        ]
    )

    await query.message.reply_text(
        "Точно удалить анкету?\n\n"
        "Фото, видео, лайки "
        "и мэтчи будут удалены.",
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
    update,
    context,
):

    logger.error(
        "Telegram error",
        exc_info=context.error,
    )


# ============================================================
# TELEGRAM APPLICATION
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


# ============================================================
# CREATE CONVERSATION
# ============================================================

create_conversation = ConversationHandler(

    entry_points=[
        MessageHandler(
            filters.Regex(
                r"^💘 Создать анкету$"
            ),
            create_profile_start,
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


# ============================================================
# EDIT CONVERSATION
# ============================================================

edit_conversation = ConversationHandler(

    entry_points=[
        CallbackQueryHandler(
            edit_callback,
            pattern=(
                r"^edit:"
                r"(name|age|city|gender|"
                r"looking_for|about)$"
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


# ============================================================
# MEDIA CONVERSATION
# ============================================================

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


# ============================================================
# HANDLERS
# ============================================================

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
        pattern=(
            r"^media:"
            r"(delete_last|back)$"
        ),
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
# RENDER / WEBHOOK
# ============================================================

async def health(
    request
):

    return web.Response(
        text=(
            "OLIVKA MATCH "
            "is running 💗"
        )
    )


async def telegram_webhook(
    request
):

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


async def on_startup(
    web_app
):

    await init_database()

    await application.initialize()

    await application.start()

    render_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL "
            "is missing"
        )

    webhook_url = (
        f"{render_url.rstrip('/')}"
        f"/telegram"
    )

    await application.bot.set_webhook(
        webhook_url
    )

    logger.info(
        "Webhook: %s",
        webhook_url,
    )

    logger.info(
        "OLIVKA MATCH started"
    )


async def on_cleanup(
    web_app
):

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
# WEB SERVER
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
