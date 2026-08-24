import os
from html import escape

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

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

NAME, AGE, CITY, GENDER, LOOKING_FOR, ABOUT, PHOTO = range(7)

# Пока анкеты хранятся в памяти.
# После проверки подключим бесплатную постоянную базу.
PROFILES = {}
LIKES = set()


def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["💘 Создать анкету"],
            ["🔥 Смотреть анкеты"],
            ["👤 Моя анкета"],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💗 Добро пожаловать в OLIVKA MATCH!\n\n"
        "Создай анкету и начинай знакомиться.",
        reply_markup=main_menu(),
    )


async def create_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как тебя зовут? 😊",
        reply_markup=ReplyKeyboardRemove(),
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()

    await update.message.reply_text(
        "Сколько тебе лет?\n\nТолько 18+ 🔞"
    )
    return AGE


async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("Напиши возраст цифрами 🙂")
        return AGE

    age = int(text)

    if age < 18:
        await update.message.reply_text(
            "OLIVKA MATCH доступен только пользователям 18+."
        )
        return ConversationHandler.END

    if age > 100:
        await update.message.reply_text("Проверь возраст 🙂")
        return AGE

    context.user_data["age"] = age

    await update.message.reply_text("Из какого ты города? 📍")
    return CITY


async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["city"] = update.message.text.strip()

    keyboard = ReplyKeyboardMarkup(
        [["👩 Девушка", "👨 Мужчина"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "Кто ты?",
        reply_markup=keyboard,
    )

    return GENDER


async def get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["gender"] = update.message.text.strip()

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
        "Кого хочешь найти?",
        reply_markup=keyboard,
    )

    return LOOKING_FOR


async def get_looking_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["looking_for"] = update.message.text.strip()

    await update.message.reply_text(
        "Расскажи немного о себе ✨\n\n"
        "Например: что любишь, чем увлекаешься и кого хочешь встретить.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return ABOUT


async def get_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["about"] = update.message.text.strip()

    await update.message.reply_text(
        "Теперь отправь свою фотографию 📸"
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

    PROFILES[user.id] = profile

    caption = (
        f"💗 <b>Твоя анкета готова!</b>\n\n"
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


async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = PROFILES.get(user_id)

    if not profile:
        await update.message.reply_text(
            "У тебя пока нет анкеты 💗\n"
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


def find_next_profile(user_id):
    for profile_id, profile in PROFILES.items():
        if profile_id != user_id:
            return profile

    return None


async def browse_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in PROFILES:
        await update.message.reply_text(
            "Сначала создай свою анкету 💘",
            reply_markup=main_menu(),
        )
        return

    profile = find_next_profile(user_id)

    if not profile:
        await update.message.reply_text(
            "Пока других анкет нет 😌\n\n"
            "Как только появятся новые люди — они будут здесь.",
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


async def profile_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, target_id = query.data.split(":")
    target_id = int(target_id)

    user_id = query.from_user.id

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "like":
        LIKES.add((user_id, target_id))

        if (target_id, user_id) in LIKES:
            my_profile_data = PROFILES.get(user_id)
            target_profile = PROFILES.get(target_id)

            if my_profile_data and target_profile:
                me_link = (
                    f"@{my_profile_data['username']}"
                    if my_profile_data["username"]
                    else f'<a href="tg://user?id={user_id}">'
                         f'{escape(my_profile_data["name"])}</a>'
                )

                target_link = (
                    f"@{target_profile['username']}"
                    if target_profile["username"]
                    else f'<a href="tg://user?id={target_id}">'
                         f'{escape(target_profile["name"])}</a>'
                )

                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "💞 <b>У вас совпадение!</b>\n\n"
                        f"Можно написать: {target_link}"
                    ),
                    parse_mode="HTML",
                )

                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "💞 <b>У вас совпадение!</b>\n\n"
                        f"Можно написать: {me_link}"
                    ),
                    parse_mode="HTML",
                )
        else:
            await query.message.reply_text("❤️ Лайк отправлен!")

    next_profile = find_next_profile(user_id)

    if next_profile:
        await show_profile(update, next_profile)
    else:
        await query.message.reply_text(
            "На сегодня анкеты закончились 😊",
            reply_markup=main_menu(),
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Создание анкеты отменено.",
        reply_markup=main_menu(),
    )
    return ConversationHandler.END


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
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
        AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age)],
        CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_city)],
        GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_gender)],
        LOOKING_FOR: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_looking_for)
        ],
        ABOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_about)],
        PHOTO: [MessageHandler(filters.PHOTO, get_photo)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


application.add_handler(CommandHandler("start", start))
application.add_handler(profile_conversation)

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


async def health(request):
    return web.Response(text="OLIVKA MATCH is running")


async def telegram_webhook(request):
    data = await request.json()

    update = Update.de_json(
        data,
        application.bot,
    )

    await application.process_update(update)

    return web.Response(text="OK")


async def on_startup(web_app):
    await application.initialize()
    await application.start()

    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if render_url:
        await application.bot.set_webhook(
            f"{render_url}/telegram"
        )


async def on_cleanup(web_app):
    await application.stop()
    await application.shutdown()


web_app = web.Application()

web_app.router.add_get("/", health)
web_app.router.add_post("/telegram", telegram_webhook)

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

web.run_app(
    web_app,
    host="0.0.0.0",
    port=PORT,
)
