import os
import io
import logging
from html import escape
from pathlib import Path

import asyncpg
from aiohttp import web

from PIL import (
    Image,
    ImageDraw,
    ImageFont,
    ImageFilter,
    ImageOps,
)

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    BotCommand,
    MenuButtonCommands,
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
PORT = int(os.getenv("PORT", "10000"))

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

MAX_MEDIA = 6
INITIAL_AUTO_PROFILES = 3
MAX_MATCHES_TO_SHOW = 10


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
# DESIGN
# ============================================================

CARD_WIDTH = 1080
CARD_HEIGHT = 1350

PINK = (244, 95, 160)
PINK_DARK = (205, 55, 119)

TEXT_DARK = (37, 31, 43)
TEXT_GRAY = (91, 84, 98)

WHITE = (255, 255, 255)


def load_font(size, bold=False):

    if bold:

        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]

    else:

        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]

    for path in candidates:

        if Path(path).exists():

            return ImageFont.truetype(
                path,
                size=size,
            )

    return ImageFont.load_default()


FONT_BRAND = load_font(31, True)
FONT_NAME = load_font(54, True)
FONT_INFO = load_font(32, False)

FONT_ABOUT_TITLE = load_font(29, True)
FONT_ABOUT = load_font(30, False)

FONT_SMALL = load_font(23, False)
FONT_COUNT = load_font(25, True)


def make_gradient():

    image = Image.new(
        "RGB",
        (CARD_WIDTH, CARD_HEIGHT),
    )

    draw = ImageDraw.Draw(image)

    top = (255, 212, 232)
    middle = (233, 214, 255)
    bottom = (203, 234, 255)

    for y in range(CARD_HEIGHT):

        ratio = y / CARD_HEIGHT

        if ratio < 0.5:

            local = ratio / 0.5

            color = tuple(
                int(
                    top[i] * (1 - local)
                    + middle[i] * local
                )
                for i in range(3)
            )

        else:

            local = (ratio - 0.5) / 0.5

            color = tuple(
                int(
                    middle[i] * (1 - local)
                    + bottom[i] * local
                )
                for i in range(3)
            )

        draw.line(
            (0, y, CARD_WIDTH, y),
            fill=color,
        )

    return image


def add_background_glow(image):

    overlay = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(overlay)

    draw.ellipse(
        (-120, 50, 420, 590),
        fill=(255, 255, 255, 80),
    )

    draw.ellipse(
        (720, -100, 1230, 410),
        fill=(255, 255, 255, 65),
    )

    draw.ellipse(
        (700, 1030, 1280, 1580),
        fill=(255, 255, 255, 55),
    )

    overlay = overlay.filter(
        ImageFilter.GaussianBlur(65)
    )

    return Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
    ).convert("RGB")


def add_main_card(image):

    shadow = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )

    shadow_draw = ImageDraw.Draw(shadow)

    shadow_draw.rounded_rectangle(
        (55, 66, 1025, 1305),
        radius=52,
        fill=(0, 0, 0, 55),
    )

    shadow = shadow.filter(
        ImageFilter.GaussianBlur(18)
    )

    base = Image.alpha_composite(
        image.convert("RGBA"),
        shadow,
    )

    overlay = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(overlay)

    draw.rounded_rectangle(
        (47, 50, 1017, 1289),
        radius=52,
        fill=(255, 255, 255, 242),
        outline=(255, 132, 185, 255),
        width=6,
    )

    draw.rounded_rectangle(
        (63, 66, 1001, 1273),
        radius=43,
        outline=(255, 214, 232, 255),
        width=2,
    )

    return Image.alpha_composite(
        base,
        overlay,
    ).convert("RGB")


def crop_photo(
    image,
    width,
    height,
):

    return ImageOps.fit(
        image.convert("RGB"),
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )


def paste_round(
    base,
    picture,
    x,
    y,
    width,
    height,
    radius=28,
):

    picture = crop_photo(
        picture,
        width,
        height,
    )

    mask = Image.new(
        "L",
        (width, height),
        0,
    )

    mask_draw = ImageDraw.Draw(mask)

    mask_draw.rounded_rectangle(
        (0, 0, width, height),
        radius=radius,
        fill=255,
    )

    base.paste(
        picture,
        (x, y),
        mask,
    )


def make_video_tile(
    width,
    height,
):

    image = Image.new(
        "RGB",
        (width, height),
        (232, 226, 242),
    )

    draw = ImageDraw.Draw(image)

    center_x = width // 2
    center_y = height // 2

    radius = min(
        width,
        height,
    ) // 8

    draw.ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        fill=PINK,
    )

    draw.polygon(
        [
            (
                center_x - radius // 4,
                center_y - radius // 2,
            ),
            (
                center_x - radius // 4,
                center_y + radius // 2,
            ),
            (
                center_x + radius // 2,
                center_y,
            ),
        ],
        fill=WHITE,
    )

    return image


async def telegram_photo_to_pil(
    bot,
    file_id,
):

    try:

        telegram_file = await bot.get_file(
            file_id
        )

        buffer = io.BytesIO()

        await telegram_file.download_to_memory(
            out=buffer
        )

        buffer.seek(0)

        return Image.open(
            buffer
        ).convert("RGB")

    except Exception as error:

        logger.warning(
            "Cannot download photo %s: %s",
            file_id,
            error,
        )

        return None


async def get_visual_tiles(
    bot,
    media,
):

    tiles = []

    for item in media[:4]:

        if item["media_type"] == "photo":

            picture = await telegram_photo_to_pil(
                bot,
                item["file_id"],
            )

            if picture:

                tiles.append(
                    picture
                )

        elif item["media_type"] == "video":

            tiles.append(
                make_video_tile(
                    500,
                    500,
                )
            )

    return tiles


def render_collage(
    base,
    pictures,
):

    x = 92
    y = 150

    width = 896
    height = 575

    gap = 14

    count = len(pictures)

    if count == 0:

        blank = Image.new(
            "RGB",
            (width, height),
            (240, 235, 244),
        )

        paste_round(
            base,
            blank,
            x,
            y,
            width,
            height,
            36,
        )

        return

    if count == 1:

        paste_round(
            base,
            pictures[0],
            x,
            y,
            width,
            height,
            36,
        )

        return

    if count == 2:

        each_width = (
            width - gap
        ) // 2

        paste_round(
            base,
            pictures[0],
            x,
            y,
            each_width,
            height,
            32,
        )

        paste_round(
            base,
            pictures[1],
            x + each_width + gap,
            y,
            each_width,
            height,
            32,
        )

        return

    if count == 3:

        left_width = 560

        right_width = (
            width
            - left_width
            - gap
        )

        half_height = (
            height
            - gap
        ) // 2

        paste_round(
            base,
            pictures[0],
            x,
            y,
            left_width,
            height,
            32,
        )

        paste_round(
            base,
            pictures[1],
            x + left_width + gap,
            y,
            right_width,
            half_height,
            28,
        )

        paste_round(
            base,
            pictures[2],
            x + left_width + gap,
            y + half_height + gap,
            right_width,
            half_height,
            28,
        )

        return

    half_width = (
        width - gap
    ) // 2

    half_height = (
        height - gap
    ) // 2

    positions = [
        (x, y),

        (
            x + half_width + gap,
            y,
        ),

        (
            x,
            y + half_height + gap,
        ),

        (
            x + half_width + gap,
            y + half_height + gap,
        ),
    ]

    for picture, position in zip(
        pictures[:4],
        positions,
    ):

        paste_round(
            base,
            picture,
            position[0],
            position[1],
            half_width,
            half_height,
            28,
        )


def split_text_by_pixels(
    draw,
    text,
    font,
    max_width,
):

    words = text.split()

    if not words:
        return []

    lines = []
    current = words[0]

    for word in words[1:]:

        trial = (
            current
            + " "
            + word
        )

        box = draw.textbbox(
            (0, 0),
            trial,
            font=font,
        )

        width = (
            box[2]
            - box[0]
        )

        if width <= max_width:

            current = trial

        else:

            lines.append(
                current
            )

            current = word

    lines.append(
        current
    )

    return lines


async def generate_profile_card(
    bot,
    profile,
):

    media = await get_media(
        profile["user_id"]
    )

    tiles = await get_visual_tiles(
        bot,
        media,
    )

    image = make_gradient()

    image = add_background_glow(
        image
    )

    image = add_main_card(
        image
    )

    render_collage(
        image,
        tiles,
    )

    draw = ImageDraw.Draw(
        image
    )

    # BRAND

    brand = "OLIVKA MATCH"

    brand_box = draw.textbbox(
        (0, 0),
        brand,
        font=FONT_BRAND,
    )

    brand_width = (
        brand_box[2]
        - brand_box[0]
    )

    draw.rounded_rectangle(
        (
            92,
            93,
            92 + brand_width + 42,
            134,
        ),
        radius=19,
        fill=(255, 229, 241),
    )

    draw.text(
        (113, 99),
        brand,
        font=FONT_BRAND,
        fill=PINK_DARK,
    )

    # MEDIA COUNT

    count_text = (
        f"{len(media)}/{MAX_MEDIA}"
    )

    box = draw.textbbox(
        (0, 0),
        count_text,
        font=FONT_COUNT,
    )

    count_width = (
        box[2]
        - box[0]
    )

    draw.rounded_rectangle(
        (
            920 - count_width,
            665,
            967,
            705,
        ),
        radius=18,
        fill=(35, 31, 43),
    )

    draw.text(
        (
            939 - count_width,
            672,
        ),
        count_text,
        font=FONT_COUNT,
        fill=WHITE,
    )

    # NAME

    name = str(
        profile.get(
            "name",
            "Без имени",
        )
    )

    age = str(
        profile.get(
            "age",
            "",
        )
    )

    title = (
        f"{name}, {age}"
    )

    draw.text(
        (92, 765),
        title,
        font=FONT_NAME,
        fill=TEXT_DARK,
    )

    # INFO

    city = str(
        profile.get(
            "city",
            "",
        )
    )

    gender = str(
        profile.get(
            "gender",
            "",
        )
    )

    gender = (
        gender
        .replace("👩 ", "")
        .replace("👨 ", "")
    )

    looking = str(
        profile.get(
            "looking_for",
            "",
        )
    )

    looking = (
        looking
        .replace("👩 ", "")
        .replace("👨 ", "")
        .replace("💞 ", "")
    )

    info_lines = [
        f"Город: {city}",
        f"Пол: {gender}",
        f"Ищу: {looking}",
    ]

    info_y = 840

    for line in info_lines:

        draw.text(
            (94, info_y),
            line,
            font=FONT_INFO,
            fill=TEXT_GRAY,
        )

        info_y += 45

    # ABOUT

    draw.text(
        (92, 992),
        "О СЕБЕ",
        font=FONT_ABOUT_TITLE,
        fill=PINK_DARK,
    )

    about = str(
        profile.get(
            "about",
            "",
        )
    ).strip()

    about_lines = split_text_by_pixels(
        draw,
        about,
        FONT_ABOUT,
        870,
    )

    about_y = 1038

    for line in about_lines[:4]:

        draw.text(
            (92, about_y),
            line,
            font=FONT_ABOUT,
            fill=TEXT_DARK,
        )

        about_y += 42

    # FOOTER

    footer = (
        "Знакомства • Симпатии • Match"
    )

    footer_box = draw.textbbox(
        (0, 0),
        footer,
        font=FONT_SMALL,
    )

    footer_width = (
        footer_box[2]
        - footer_box[0]
    )

    draw.text(
        (
            CARD_WIDTH
            - 92
            - footer_width,
            1235,
        ),
        footer,
        font=FONT_SMALL,
        fill=(150, 141, 155),
    )

    output = io.BytesIO()

    output.name = (
        "olivka_match_profile.jpg"
    )

    image.save(
        output,
        format="JPEG",
        quality=94,
        optimize=True,
    )

    output.seek(0)

    return output


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
                PRIMARY KEY (
                    from_user,
                    to_user
                )
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skips (
                from_user BIGINT NOT NULL,
                to_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    from_user,
                    to_user
                )
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                user1 BIGINT NOT NULL,
                user2 BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    user1,
                    user2
                )
            )
            """
        )

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

        # Старые фото переносим в media.
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

            WHERE
                p.photo IS NOT NULL

                AND p.photo <> ''

                AND NOT EXISTS (

                    SELECT 1

                    FROM profile_media pm

                    WHERE
                        pm.user_id
                        = p.user_id
                )
            """
        )

    logger.info(
        "Database initialized"
    )


async def get_profile(
    user_id,
):

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


async def save_profile(
    profile,
):

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
                $1,$2,$3,$4,$5,
                $6,$7,$8,$9
            )

            ON CONFLICT (user_id)

            DO UPDATE SET

                username =
                    EXCLUDED.username,

                name =
                    EXCLUDED.name,

                age =
                    EXCLUDED.age,

                city =
                    EXCLUDED.city,

                gender =
                    EXCLUDED.gender,

                looking_for =
                    EXCLUDED.looking_for,

                about =
                    EXCLUDED.about,

                photo =
                    EXCLUDED.photo,

                updated_at =
                    CURRENT_TIMESTAMP
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


async def update_username(
    user_id,
    username,
):

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            UPDATE profiles

            SET
                username = $1,
                updated_at =
                    CURRENT_TIMESTAMP

            WHERE user_id = $2
            """,
            username,
            user_id,
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

        SET
            {field} = $1,
            updated_at =
                CURRENT_TIMESTAMP

        WHERE user_id = $2
    """

    async with db_pool.acquire() as conn:

        await conn.execute(
            query,
            value,
            user_id,
        )


# ============================================================
# MEDIA
# ============================================================

async def get_media(
    user_id,
):

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

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO profile_media (
                user_id,
                media_type,
                file_id,
                sort_order
            )

            VALUES (
                $1,$2,$3,$4
            )
            """,
            user_id,
            media_type,
            file_id,
            len(current),
        )

    return True


async def replace_with_first_photo(
    user_id,
    file_id,
):

    # photo в старой базе NOT NULL.
    # Поэтому никогда не ставим photo = NULL.

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

                SET
                    photo = $1,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE user_id = $2
                """,
                file_id,
                user_id,
            )


async def delete_last_media(
    user_id,
):

    media = await get_media(
        user_id
    )

    # Всегда оставляем главное фото.

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
# LIKES
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

            ON CONFLICT
            DO NOTHING
            """,
            from_user,
            to_user,
        )

        await conn.execute(
            """
            DELETE FROM skips

            WHERE
                from_user = $1
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

            ON CONFLICT
            DO NOTHING
            """,
            from_user,
            to_user,
        )


async def create_match_if_mutual(
    user_a,
    user_b,
):

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

                    WHERE
                        from_user = $1
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

                    WHERE
                        from_user = $1
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

            created = await conn.fetchrow(
                """
                INSERT INTO matches (
                    user1,
                    user2
                )

                VALUES ($1,$2)

                ON CONFLICT
                DO NOTHING

                RETURNING
                    user1,
                    user2
                """,
                first,
                second,
            )

            return (
                created is not None
            )


async def get_people_who_liked_me(
    user_id,
):

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT p.*

            FROM likes l

            JOIN profiles p
                ON p.user_id =
                    l.from_user

            WHERE
                l.to_user = $1

                AND NOT EXISTS (

                    SELECT 1

                    FROM likes reverse_like

                    WHERE
                        reverse_like.from_user
                            = $1

                        AND
                        reverse_like.to_user
                            = l.from_user
                )

                AND NOT EXISTS (

                    SELECT 1

                    FROM skips s

                    WHERE
                        s.from_user = $1

                        AND
                        s.to_user =
                            l.from_user
                )

            ORDER BY
                l.created_at DESC
            """,
            user_id,
        )

    return [
        dict(row)
        for row in rows
    ]


async def get_my_matches(
    user_id,
):

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                p.*,
                m.created_at
                    AS match_created_at

            FROM matches m

            JOIN profiles p

            ON p.user_id =

                CASE

                    WHEN m.user1 = $1
                    THEN m.user2

                    ELSE m.user1

                END

            WHERE
                m.user1 = $1
                OR m.user2 = $1

            ORDER BY
                m.created_at DESC

            LIMIT $2
            """,
            user_id,
            MAX_MATCHES_TO_SHOW,
        )

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# DELIVERIES
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

                WHERE
                    recipient_user = $1

                    AND
                    profile_user = $2
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

            ON CONFLICT
            DO NOTHING
            """,
            recipient_user,
            profile_user,
        )


# ============================================================
# DELETE PROFILE
# ============================================================

async def delete_profile(
    user_id,
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

                WHERE
                    from_user = $1
                    OR to_user = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM skips

                WHERE
                    from_user = $1
                    OR to_user = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM matches

                WHERE
                    user1 = $1
                    OR user2 = $1
                """,
                user_id,
            )

            await conn.execute(
                """
                DELETE FROM profile_deliveries

                WHERE
                    recipient_user = $1
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
    looking_for,
    viewer_gender,
):

    if looking_for == "💞 Неважно":

        return True

    if looking_for == "👩 Девушку":

        return (
            viewer_gender
            == "👩 Девушка"
        )

    if looking_for == "👨 Мужчину":

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


async def get_next_profile(
    user_id,
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

            WHERE
                p.user_id <> $1

                AND NOT EXISTS (

                    SELECT 1

                    FROM likes l

                    WHERE
                        l.from_user = $1

                        AND
                        l.to_user =
                            p.user_id
                )

                AND NOT EXISTS (

                    SELECT 1

                    FROM skips s

                    WHERE
                        s.from_user = $1

                        AND
                        s.to_user =
                            p.user_id
                )

            ORDER BY
                p.updated_at DESC

            LIMIT 100
            """,
            user_id,
        )

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

        if media:

            return candidate

    return None


async def compatible_profiles(
    user_id,
    limit=3,
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

            WHERE
                user_id <> $1

            ORDER BY
                updated_at DESC
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

        if len(result) >= limit:

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
                "💌 Кто меня лайкнул",
                "💞 Мои мэтчи",
            ],

            [
                "👤 Моя анкета",
                "✏️ Редактировать",
            ],

            [
                "ℹ️ Помощь"
            ],
        ],

        resize_keyboard=True,

        # Главное:
        is_persistent=True,

        one_time_keyboard=False,

        input_field_placeholder=(
            "Выбери действие 💗"
        ),
    )


def create_menu():

    return ReplyKeyboardMarkup(
        [
            [
                "💘 Создать анкету"
            ],

            [
                "ℹ️ Помощь"
            ],
        ],

        resize_keyboard=True,

        is_persistent=True,

        one_time_keyboard=False,

        input_field_placeholder=(
            "Создай анкету 💗"
        ),
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
            [
                "👩 Девушку"
            ],

            [
                "👨 Мужчину"
            ],

            [
                "💞 Неважно"
            ],
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


def like_keyboard(
    profile_id,
):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👎 Пропустить",
                    callback_data=(
                        f"skip:{profile_id}"
                    ),
                ),

                InlineKeyboardButton(
                    "❤️ Нравится",
                    callback_data=(
                        f"like:{profile_id}"
                    ),
                ),
            ]
        ]
    )


def contact_keyboard(
    profile,
):

    username = profile.get(
        "username"
    )

    if username:

        link = (
            "https://t.me/"
            + username
        )

    else:

        link = (
            "tg://user?id="
            + str(
                profile["user_id"]
            )
        )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💬 Написать",
                    url=link,
                )
            ]
        ]
    )


# ============================================================
# SEND PROFILE
# ============================================================

async def send_profile_card(
    bot,
    chat_id,
    profile,
):

    try:

        card = await generate_profile_card(
            bot,
            profile,
        )

        await bot.send_photo(
            chat_id=chat_id,
            photo=card,
        )

    except Exception as error:

        logger.exception(
            "Card generation error: %s",
            error,
        )

        # Если Pillow-карточка не собралась,
        # показываем обычную фотографию.

        await bot.send_photo(
            chat_id=chat_id,
            photo=profile["photo"],
            caption=(
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
            ),
            parse_mode="HTML",
        )


async def send_extra_real_media(
    bot,
    chat_id,
    profile,
):

    media = await get_media(
        profile["user_id"]
    )

    extras = []

    # Первые 4 фото уже находятся
    # внутри дизайнерской карточки.
    # Видео оставляем настоящими.
    # 5-е и 6-е фото тоже показываем отдельно.

    for index, item in enumerate(
        media
    ):

        if item["media_type"] == "video":

            extras.append(
                InputMediaVideo(
                    media=item["file_id"]
                )
            )

        elif (
            item["media_type"] == "photo"
            and index >= 4
        ):

            extras.append(
                InputMediaPhoto(
                    media=item["file_id"]
                )
            )

    if not extras:

        return

    try:

        if len(extras) == 1:

            item = extras[0]

            if isinstance(
                item,
                InputMediaVideo,
            ):

                await bot.send_video(
                    chat_id=chat_id,
                    video=item.media,
                )

            else:

                await bot.send_photo(
                    chat_id=chat_id,
                    photo=item.media,
                )

        else:

            await bot.send_media_group(
                chat_id=chat_id,
                media=extras,
            )

    except Exception as error:

        logger.warning(
            "Extra media error: %s",
            error,
        )


async def send_profile_display(
    bot,
    chat_id,
    profile,
):

    await send_profile_card(
        bot,
        chat_id,
        profile,
    )

    await send_extra_real_media(
        bot,
        chat_id,
        profile,
    )


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

    await send_profile_display(
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
# AUTOMATIC PROFILES
# ============================================================

async def send_initial_profiles(
    bot,
    user_id,
):

    profiles = await compatible_profiles(
        user_id,
        INITIAL_AUTO_PROFILES,
    )

    for profile in profiles:

        if await already_delivered(
            user_id,
            profile["user_id"],
        ):

            continue

        try:

            await send_profile_for_choice(
                bot,
                user_id,
                profile,
                header=(
                    "🔥 Подходящая "
                    "анкета для тебя"
                ),
            )

            await mark_delivered(
                user_id,
                profile["user_id"],
            )

        except Exception as error:

            logger.warning(
                "Auto profile error: %s",
                error,
            )


async def notify_about_new_profile(
    bot,
    new_user_id,
):

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

            WHERE
                user_id <> $1
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

        if await already_delivered(
            recipient["user_id"],
            newcomer["user_id"],
        ):

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

            logger.warning(
                "New profile delivery: %s",
                error,
            )


# ============================================================
# MATCH
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

    if not profile_a or not profile_b:

        return

    # A получает B.

    try:

        await bot.send_message(
            chat_id=user_a,
            text=(
                "💞 <b>ЭТО MATCH!</b> 💞\n\n"
                "Вы понравились "
                "друг другу ❤️"
            ),
            parse_mode="HTML",
        )

        await send_profile_display(
            bot,
            user_a,
            profile_b,
        )

        await bot.send_message(
            chat_id=user_a,
            text=(
                "🔥 Симпатия взаимна!\n"
                "Самое время познакомиться."
            ),
            reply_markup=contact_keyboard(
                profile_b
            ),
        )

    except Exception as error:

        logger.exception(
            "Match A error: %s",
            error,
        )

    # B получает A.

    try:

        await bot.send_message(
            chat_id=user_b,
            text=(
                "💞 <b>ЭТО MATCH!</b> 💞\n\n"
                "Вы понравились "
                "друг другу ❤️"
            ),
            parse_mode="HTML",
        )

        await send_profile_display(
            bot,
            user_b,
            profile_a,
        )

        await bot.send_message(
            chat_id=user_b,
            text=(
                "🔥 Симпатия взаимна!\n"
                "Самое время познакомиться."
            ),
            reply_markup=contact_keyboard(
                profile_a
            ),
        )

    except Exception as error:

        logger.exception(
            "Match B error: %s",
            error,
        )


# ============================================================
# START / HELP
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    profile = await get_profile(
        update.effective_user.id
    )

    if profile:

        await update_username(
            update.effective_user.id,
            update.effective_user.username,
        )

        await update.message.reply_text(
            (
                "💗 Добро пожаловать "
                "в OLIVKA MATCH!\n\n"
                "Главное меню теперь "
                "всегда под рукой 👇"
            ),
            reply_markup=main_menu(),
        )

    else:

        await update.message.reply_text(
            (
                "💗 Добро пожаловать "
                "в OLIVKA MATCH!\n\n"
                "Создай свою анкету."
            ),
            reply_markup=create_menu(),
        )


async def help_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    profile = await get_profile(
        update.effective_user.id
    )

    await update.message.reply_text(
        (
            "💗 <b>OLIVKA MATCH</b>\n\n"

            "🔥 <b>Смотреть анкеты</b> — "
            "листать подходящих людей.\n\n"

            "💌 <b>Кто меня лайкнул</b> — "
            "посмотреть входящие симпатии.\n\n"

            "💞 <b>Мои мэтчи</b> — "
            "взаимные симпатии.\n\n"

            "👤 <b>Моя анкета</b> — "
            "посмотреть свою карточку.\n\n"

            "✏️ <b>Редактировать</b> — "
            "изменить данные, фото и видео.\n\n"

            "При взаимном ❤️ "
            "MATCH автоматически "
            "приходит обоим."
        ),
        parse_mode="HTML",
        reply_markup=(
            main_menu()
            if profile
            else create_menu()
        ),
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

    value = (
        update.message.text
        .strip()
    )

    if len(value) < 2:

        await update.message.reply_text(
            "Напиши имя чуть подробнее 🙂"
        )

        return CREATE_NAME

    context.user_data[
        "name"
    ] = value

    await update.message.reply_text(
        "Сколько тебе лет?\n\n"
        "Только 18+ 🔞"
    )

    return CREATE_AGE


async def create_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    value = (
        update.message.text
        .strip()
    )

    if not value.isdigit():

        await update.message.reply_text(
            "Возраст напиши цифрами."
        )

        return CREATE_AGE

    age = int(value)

    if age < 18 or age > 100:

        await update.message.reply_text(
            "Возраст должен быть "
            "от 18 до 100."
        )

        return CREATE_AGE

    context.user_data[
        "age"
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
        "city"
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

    value = (
        update.message.text
        .strip()
    )

    if value not in (
        "👩 Девушка",
        "👨 Мужчина",
    ):

        await update.message.reply_text(
            "Выбери вариант кнопкой."
        )

        return CREATE_GENDER

    context.user_data[
        "gender"
    ] = value

    await update.message.reply_text(
        "Кого хочешь найти? 💘",
        reply_markup=looking_keyboard(),
    )

    return CREATE_LOOKING


async def create_looking(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    value = (
        update.message.text
        .strip()
    )

    if value not in (
        "👩 Девушку",
        "👨 Мужчину",
        "💞 Неважно",
    ):

        await update.message.reply_text(
            "Выбери вариант кнопкой."
        )

        return CREATE_LOOKING

    context.user_data[
        "looking_for"
    ] = value

    await update.message.reply_text(
        "Расскажи немного о себе ✨",
        reply_markup=ReplyKeyboardRemove(),
    )

    return CREATE_ABOUT


async def create_about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    value = (
        update.message.text
        .strip()
    )

    if len(value) > 500:

        await update.message.reply_text(
            "Максимум 500 символов."
        )

        return CREATE_ABOUT

    context.user_data[
        "about"
    ] = value

    await update.message.reply_text(
        "Теперь отправь "
        "главное фото 📸"
    )

    return CREATE_PHOTO


async def create_first_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message.photo:

        await update.message.reply_text(
            "Отправь именно "
            "фотографию 📸"
        )

        return CREATE_PHOTO

    user = update.effective_user

    file_id = (
        update.message
        .photo[-1]
        .file_id
    )

    profile = {

        "user_id":
            user.id,

        "username":
            user.username,

        "name":
            context.user_data[
                "name"
            ],

        "age":
            context.user_data[
                "age"
            ],

        "city":
            context.user_data[
                "city"
            ],

        "gender":
            context.user_data[
                "gender"
            ],

        "looking_for":
            context.user_data[
                "looking_for"
            ],

        "about":
            context.user_data[
                "about"
            ],

        "photo":
            file_id,
    }

    try:

        await save_profile(
            profile
        )

        await replace_with_first_photo(
            user.id,
            file_id,
        )

    except Exception as error:

        logger.exception(
            "Profile save error: %s",
            error,
        )

        await update.message.reply_text(
            (
                "Не удалось сохранить "
                "анкету 😔\n"
                "Попробуй фото ещё раз."
            )
        )

        return CREATE_PHOTO

    context.user_data.clear()

    await update.message.reply_text(
        (
            "✅ Анкета готова!\n\n"
            "Меню теперь всегда "
            "находится внизу 👇"
        ),
        reply_markup=main_menu(),
    )

    saved = await get_profile(
        user.id
    )

    await send_profile_display(
        context.bot,
        user.id,
        saved,
    )

    await send_initial_profiles(
        context.bot,
        user.id,
    )

    await notify_about_new_profile(
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
            "У тебя пока нет анкеты.",
            reply_markup=create_menu(),
        )

        return

    await send_profile_display(
        context.bot,
        user_id,
        profile,
    )

    await update.message.reply_text(
        "💗 Главное меню",
        reply_markup=main_menu(),
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
            "Сначала создай анкету.",
            reply_markup=create_menu(),
        )

        return

    candidate = await get_next_profile(
        user_id
    )

    if not candidate:

        await update.message.reply_text(
            (
                "Пока новых подходящих "
                "анкет нет 😌\n\n"
                "Новые анкеты будут "
                "приходить автоматически."
            ),
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
# WHO LIKED ME
# ============================================================

async def who_liked_me(
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
            "Сначала создай анкету 💗",
            reply_markup=create_menu(),
        )

        return

    people = await get_people_who_liked_me(
        user_id
    )

    if not people:

        await update.message.reply_text(
            (
                "💌 Пока нет новых "
                "входящих лайков.\n\n"
                "Когда кто-то поставит "
                "тебе ❤️, он появится здесь."
            ),
            reply_markup=main_menu(),
        )

        return

    await update.message.reply_text(
        (
            "💌 Тебя лайкнули: "
            f"{len(people)}"
        )
    )

    for liked_profile in people[:10]:

        await send_profile_for_choice(
            context.bot,
            user_id,
            liked_profile,
            header=(
                "❤️ Ты понравилась "
                "этому человеку"
            ),
        )


# ============================================================
# MY MATCHES
# ============================================================

async def my_matches(
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
            "Сначала создай анкету 💗",
            reply_markup=create_menu(),
        )

        return

    matches = await get_my_matches(
        user_id
    )

    if not matches:

        await update.message.reply_text(
            (
                "💞 Пока мэтчей нет.\n\n"
                "Как только лайк станет "
                "взаимным, бот сразу "
                "уведомит вас обоих."
            ),
            reply_markup=main_menu(),
        )

        return

    await update.message.reply_text(
        (
            "💞 Твои мэтчи: "
            f"{len(matches)}"
        )
    )

    for match_profile in matches:

        await send_profile_display(
            context.bot,
            user_id,
            match_profile,
        )

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "💞 Вы понравились "
                "друг другу"
            ),
            reply_markup=contact_keyboard(
                match_profile
            ),
        )

    await update.message.reply_text(
        "💗 Главное меню",
        reply_markup=main_menu(),
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

        action, target = (
            query.data.split(
                ":",
                1,
            )
        )

        target_id = int(
            target
        )

    except Exception:

        return

    user_id = (
        query.from_user.id
    )

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
            "👎 Пропущено",
            reply_markup=main_menu(),
        )

    elif action == "like":

        await add_like(
            user_id,
            target_id,
        )

        matched = (
            await create_match_if_mutual(
                user_id,
                target_id,
            )
        )

        if matched:

            await send_match_notifications(
                context.bot,
                user_id,
                target_id,
            )

        else:

            await query.message.reply_text(
                "❤️ Лайк отправлен!",
                reply_markup=main_menu(),
            )

    # После выбора сразу предлагаем
    # следующую подходящую анкету.

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
            (
                "Пока это всё 😊\n"
                "Новые анкеты будут "
                "приходить автоматически."
            ),
            reply_markup=main_menu(),
        )


# ============================================================
# EDIT
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
            "Сначала создай анкету.",
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
            (
                "📸 Фото и видео\n\n"
                f"Сейчас: "
                f"{len(media)}/{MAX_MEDIA}"
            ),
            reply_markup=media_menu(),
        )

        return ConversationHandler.END

    context.user_data[
        "edit_field"
    ] = action

    if action == "gender":

        await query.message.reply_text(
            "Выбери пол:",
            reply_markup=gender_keyboard(),
        )

    elif action == "looking_for":

        await query.message.reply_text(
            "Кого ищешь?",
            reply_markup=looking_keyboard(),
        )

    else:

        names = {

            "name":
                "Новое имя:",

            "age":
                "Новый возраст:",

            "city":
                "Новый город:",

            "about":
                "Новое описание:",
        }

        await query.message.reply_text(
            names[action],
            reply_markup=ReplyKeyboardRemove(),
        )

    return EDIT_VALUE


async def save_edit_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    field = (
        context.user_data
        .get("edit_field")
    )

    if not field:

        return ConversationHandler.END

    value = (
        update.message.text
        .strip()
    )

    if field == "age":

        if not value.isdigit():

            await update.message.reply_text(
                "Возраст напиши цифрами."
            )

            return EDIT_VALUE

        value = int(value)

        if value < 18 or value > 100:

            await update.message.reply_text(
                "Возраст от 18 до 100."
            )

            return EDIT_VALUE

    elif field == "gender":

        if value not in (
            "👩 Девушка",
            "👨 Мужчина",
        ):

            await update.message.reply_text(
                "Выбери вариант кнопкой."
            )

            return EDIT_VALUE

    elif field == "looking_for":

        if value not in (
            "👩 Девушку",
            "👨 Мужчину",
            "💞 Неважно",
        ):

            await update.message.reply_text(
                "Выбери вариант кнопкой."
            )

            return EDIT_VALUE

    elif field == "about":

        if len(value) > 500:

            await update.message.reply_text(
                "Максимум 500 символов."
            )

            return EDIT_VALUE

    await update_profile_field(
        update.effective_user.id,
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

        if len(media) >= MAX_MEDIA:

            await query.message.reply_text(
                (
                    f"Уже добавлен максимум "
                    f"{MAX_MEDIA} файлов."
                ),
                reply_markup=main_menu(),
            )

            return ConversationHandler.END

        await query.message.reply_text(
            "Отправь фото или видео 📸🎬",
            reply_markup=ReplyKeyboardRemove(),
        )

        return ADD_MEDIA

    if action == "delete_last":

        removed = await delete_last_media(
            user_id
        )

        if not removed:

            await query.message.reply_text(
                (
                    "Главное фото удалить нельзя.\n\n"
                    "В анкете должно остаться "
                    "хотя бы одно фото."
                ),
                reply_markup=main_menu(),
            )

        else:

            media = await get_media(
                user_id
            )

            await query.message.reply_text(
                (
                    "🗑 Последний файл удалён.\n\n"
                    f"Осталось: "
                    f"{len(media)}/{MAX_MEDIA}"
                ),
                reply_markup=media_menu(),
            )

        return ConversationHandler.END

    if action == "back":

        await query.message.reply_text(
            "✏️ Редактирование",
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

    current = await get_media(
        user_id
    )

    if len(current) >= MAX_MEDIA:

        await update.message.reply_text(
            (
                f"Максимум "
                f"{MAX_MEDIA} файлов."
            ),
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
            "Нужно отправить фото или видео."
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
            "Не удалось сохранить файл 😔"
        )

        return ADD_MEDIA

    if not success:

        await update.message.reply_text(
            (
                f"Максимум "
                f"{MAX_MEDIA} файлов."
            ),
            reply_markup=main_menu(),
        )

        return ConversationHandler.END

    current = await get_media(
        user_id
    )

    await update.message.reply_text(
        (
            "✅ Добавлено!\n\n"
            f"Сейчас: "
            f"{len(current)}/{MAX_MEDIA}"
        ),
        reply_markup=main_menu(),
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
        (
            "Точно удалить анкету?\n\n"
            "Фото, видео, лайки "
            "и мэтчи будут удалены."
        ),
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

    context.user_data.clear()

    profile = await get_profile(
        update.effective_user.id
    )

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
# UNKNOWN TEXT
# ============================================================

async def unknown_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    profile = await get_profile(
        update.effective_user.id
    )

    await update.message.reply_text(
        "Выбери действие из меню 👇",
        reply_markup=(
            main_menu()
            if profile
            else create_menu()
        ),
    )


# ============================================================
# ERROR
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
# APPLICATION
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "BOT_TOKEN missing"
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
# COMMANDS
# ============================================================

application.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

application.add_handler(
    CommandHandler(
        "profile",
        my_profile,
    )
)

application.add_handler(
    CommandHandler(
        "browse",
        browse_profiles,
    )
)

application.add_handler(
    CommandHandler(
        "likes",
        who_liked_me,
    )
)

application.add_handler(
    CommandHandler(
        "matches",
        my_matches,
    )
)

application.add_handler(
    CommandHandler(
        "help",
        help_handler,
    )
)


# ============================================================
# CONVERSATIONS
# ============================================================

application.add_handler(
    create_conversation
)

application.add_handler(
    edit_conversation
)

application.add_handler(
    media_conversation
)


# ============================================================
# MAIN MENU BUTTONS
# ============================================================

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
            r"^💌 Кто меня лайкнул$"
        ),
        who_liked_me,
    )
)

application.add_handler(
    MessageHandler(
        filters.Regex(
            r"^💞 Мои мэтчи$"
        ),
        my_matches,
    )
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
            r"^✏️ Редактировать$"
        ),
        open_edit_menu,
    )
)

application.add_handler(
    MessageHandler(
        filters.Regex(
            r"^ℹ️ Помощь$"
        ),
        help_handler,
    )
)


# ============================================================
# CALLBACKS
# ============================================================

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


# ============================================================
# FALLBACK
# ============================================================

application.add_handler(
    MessageHandler(
        filters.TEXT
        & ~filters.COMMAND,
        unknown_text,
    )
)


application.add_error_handler(
    error_handler
)


# ============================================================
# WEBHOOK
# ============================================================

async def health(
    request,
):

    return web.Response(
        text=(
            "OLIVKA MATCH "
            "is running 💗"
        )
    )


async def telegram_webhook(
    request,
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


# ============================================================
# STARTUP
# ============================================================

async def on_startup(
    web_app,
):

    await init_database()

    await application.initialize()

    await application.start()

    render_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL missing"
        )

    webhook_url = (
        render_url.rstrip("/")
        + "/telegram"
    )

    await application.bot.set_webhook(
        webhook_url
    )

    # ========================================================
    # SYSTEM TELEGRAM MENU
    # ========================================================

    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Главное меню",
            ),

            BotCommand(
                "browse",
                "Смотреть анкеты",
            ),

            BotCommand(
                "likes",
                "Кто меня лайкнул",
            ),

            BotCommand(
                "matches",
                "Мои мэтчи",
            ),

            BotCommand(
                "profile",
                "Моя анкета",
            ),

            BotCommand(
                "help",
                "Помощь",
            ),
        ]
    )

    try:

        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )

    except Exception as error:

        logger.warning(
            "Telegram menu warning: %s",
            error,
        )

    logger.info(
        "Webhook: %s",
        webhook_url,
    )

    logger.info(
        "OLIVKA MATCH STARTED"
    )


# ============================================================
# CLEANUP
# ============================================================

async def on_cleanup(
    web_app,
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
