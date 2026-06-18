import os
import logging
import asyncio
from datetime import datetime, timedelta

import httpx
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
OWNER_ID        = int(os.environ["OWNER_TELEGRAM_ID"])
PF_LOGIN        = os.environ["POSIFLORA_LOGIN"]
PF_PASSWORD     = os.environ["POSIFLORA_PASSWORD"]
PF_URL          = os.environ.get("POSIFLORA_API_URL", "https://demo.posiflora.com/api")
TZ_NAME         = os.environ.get("TIMEZONE", "Europe/Moscow")
REPORT_HOUR     = int(os.environ.get("REPORT_HOUR", "21"))
REPORT_MINUTE   = int(os.environ.get("REPORT_MINUTE", "0"))

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
OPEN_PHOTO  = 1
CLOSE_PHOTO = 2
CLOSE_CASH  = 3

# ─── Runtime state ────────────────────────────────────────────────────────────
shifts: dict[int, dict] = {}   # user_id → {name, opened_at}
_pf_token: dict = {}           # {access, expires_at}


# ═══════════════════════════════════════════════════════════════════════════════
#  Posiflora API
# ═══════════════════════════════════════════════════════════════════════════════

async def _pf_auth() -> bool:
    """Login to Posiflora and cache access token."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{PF_URL}/v1/sessions",
                headers={"Content-Type": "application/vnd.api+json"},
                json={
                    "data": {
                        "type": "sessions",
                        "attributes": {"username": PF_LOGIN, "password": PF_PASSWORD},
                    }
                },
            )
            r.raise_for_status()
            attrs = r.json()["data"]["attributes"]
            _pf_token["access"] = attrs["accessToken"]
            _pf_token["expires_at"] = datetime.now() + timedelta(minutes=55)
            logger.info("Posiflora: authenticated")
            return True
    except Exception as exc:
        logger.error("Posiflora auth error: %s", exc)
        return False


async def pf_get(path: str, params: dict | None = None) -> dict | None:
    """Authenticated GET from Posiflora. Re-authenticates on expiry."""
    if not _pf_token.get("access") or datetime.now() >= _pf_token.get("expires_at", datetime.min):
        if not await _pf_auth():
            return None

    headers = {
        "Authorization": f"Bearer {_pf_token['access']}",
        "Accept": "application/vnd.api+json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{PF_URL}{path}", headers=headers, params=params)
            if r.status_code == 401:
                if await _pf_auth():
                    headers["Authorization"] = f"Bearer {_pf_token['access']}"
                    r = await client.get(f"{PF_URL}{path}", headers=headers, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.error("Posiflora GET %s error: %s", path, exc)
        return None


def _fmt_money(value) -> str:
    try:
        return f"{float(value):,.0f} ₽".replace(",", " ")
    except Exception:
        return str(value)


async def build_sales_text(date_from: datetime, date_to: datetime, title: str) -> str:
    """Fetch store general stats and return formatted message."""
    data = await pf_get(
        "/v1/stats/stores/general",
        params={
            "filter[dateFrom]": date_from.strftime("%Y-%m-%d"),
            "filter[dateTo]": date_to.strftime("%Y-%m-%d"),
        },
    )
    if not data:
        return "❌ Не удалось получить данные из Posiflora"

    try:
        attrs = data.get("data", {}).get("attributes", {})
        revenue       = attrs.get("revenue", 0)
        orders_count  = attrs.get("ordersCount", 0)
        avg_check     = attrs.get("avgCheck", 0)
        profit        = attrs.get("profit", 0)

        return (
            f"📊 *{title}*\n\n"
            f"💰 Выручка:      *{_fmt_money(revenue)}*\n"
            f"📦 Заказов:      *{orders_count}*\n"
            f"🧾 Средний чек:  *{_fmt_money(avg_check)}*\n"
            f"📈 Прибыль:      *{_fmt_money(profit)}*"
        )
    except Exception as exc:
        logger.error("Error parsing Posiflora stats: %s", exc)
        return f"❌ Ошибка разбора данных: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def full_name(update: Update) -> str:
    u = update.effective_user
    name = u.full_name
    if u.username:
        name += f" (@{u.username})"
    return name


# ═══════════════════════════════════════════════════════════════════════════════
#  General commands
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_owner(uid):
        text = (
            "👋 Привет, владелец!\n\n"
            "*Команды для тебя:*\n"
            "/sales — продажи за сегодня\n"
            "/week — сводка за 7 дней\n"
            "/staff — кто сейчас на смене\n\n"
            "*Команды для сотрудников:*\n"
            "/open\\_shift — открыть смену\n"
            "/close\\_shift — закрыть смену\n"
            "📸 Любое фото — фотоотчёт"
        )
    else:
        text = (
            "👋 Привет!\n\n"
            "/open\\_shift — открыть смену\n"
            "/close\\_shift — закрыть смену\n"
            "📸 Отправь фото — уйдёт владельцу"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper: show user's Telegram ID."""
    uid = update.effective_user.id
    await update.message.reply_text(f"Твой Telegram ID: `{uid}`", parse_mode="Markdown")


async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Только для владельца")
        return

    if not shifts:
        await update.message.reply_text("Сейчас никого нет на смене.")
        return

    tz = pytz.timezone(TZ_NAME)
    lines = ["👥 *На смене сейчас:*\n"]
    for uid, info in shifts.items():
        opened = info["opened_at"].astimezone(tz).strftime("%H:%M")
        lines.append(f"• {info['name']} — с {opened}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_sales(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Только для владельца")
        return
    await update.message.reply_text("⏳ Запрашиваю данные из Posiflora...")
    today = datetime.now(pytz.timezone(TZ_NAME))
    text = await build_sales_text(today, today, f"Продажи за {today.strftime('%d.%m.%Y')}")
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Только для владельца")
        return
    await update.message.reply_text("⏳ Запрашиваю данные из Posiflora...")
    tz = pytz.timezone(TZ_NAME)
    today = datetime.now(tz)
    week_start = today - timedelta(days=6)
    title = f"Продажи {week_start.strftime('%d.%m')}–{today.strftime('%d.%m.%Y')}"
    text = await build_sales_text(week_start, today, title)
    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  Open Shift conversation
# ═══════════════════════════════════════════════════════════════════════════════

async def open_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if uid in shifts:
        opened = shifts[uid]["opened_at"].strftime("%H:%M")
        await update.message.reply_text(
            f"⚠️ Смена уже открыта с {opened}.\n"
            "Чтобы закрыть — /close\\_shift",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    await update.message.reply_text("📸 Пришли фото витрины для открытия смены")
    return OPEN_PHOTO


async def open_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    name = full_name(update)
    now = datetime.now(pytz.timezone(TZ_NAME))
    shifts[uid] = {"name": name, "opened_at": now}

    photo_id = update.message.photo[-1].file_id
    caption = (
        f"🟢 *Смена открыта*\n"
        f"👤 {name}\n"
        f"🕐 {now.strftime('%d.%m.%Y %H:%M')}"
    )
    await context.bot.send_photo(chat_id=OWNER_ID, photo=photo_id, caption=caption, parse_mode="Markdown")
    await update.message.reply_text(f"✅ Смена открыта в {now.strftime('%H:%M')}. Удачного дня!")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  Close Shift conversation
# ═══════════════════════════════════════════════════════════════════════════════

async def close_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if uid not in shifts:
        await update.message.reply_text("⚠️ Нет открытой смены. Открой сначала: /open\\_shift", parse_mode="Markdown")
        return ConversationHandler.END
    await update.message.reply_text("📸 Пришли фото витрины/кассы для закрытия смены")
    return CLOSE_PHOTO


async def close_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["close_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("💵 Введи сумму в кассе (рублей):")
    return CLOSE_CASH


async def close_cash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    name = full_name(update)

    try:
        cash = float(update.message.text.replace(",", ".").replace(" ", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("❌ Введи число, например: 15000")
        return CLOSE_CASH

    tz = pytz.timezone(TZ_NAME)
    now = datetime.now(tz)
    shift = shifts.pop(uid, {})
    opened_at = shift.get("opened_at", now)
    duration = now - opened_at
    total_min = int(duration.total_seconds()) // 60
    hours, minutes = divmod(total_min, 60)

    photo_id = context.user_data.pop("close_photo", None)
    caption = (
        f"🔴 *Смена закрыта*\n"
        f"👤 {name}\n"
        f"🕐 {opened_at.strftime('%H:%M')} — {now.strftime('%H:%M')} ({hours}ч {minutes}м)\n"
        f"💵 Касса: *{_fmt_money(cash)}*"
    )

    if photo_id:
        await context.bot.send_photo(chat_id=OWNER_ID, photo=photo_id, caption=caption, parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text=caption, parse_mode="Markdown")

    await update.message.reply_text(f"✅ Смена закрыта. Продолжительность: {hours}ч {minutes}м. До завтра!")
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  Free-form photo report
# ═══════════════════════════════════════════════════════════════════════════════

async def photo_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any photo from a non-owner employee → forward to owner."""
    uid = update.effective_user.id
    if is_owner(uid):
        return

    name = full_name(update)
    tz = pytz.timezone(TZ_NAME)
    now = datetime.now(tz)
    user_caption = update.message.caption or ""

    caption = (
        f"📸 *Фотоотчёт*\n"
        f"👤 {name}\n"
        f"🕐 {now.strftime('%d.%m.%Y %H:%M')}"
    )
    if user_caption:
        caption += f"\n📝 {user_caption}"

    photo_id = update.message.photo[-1].file_id
    await context.bot.send_photo(chat_id=OWNER_ID, photo=photo_id, caption=caption, parse_mode="Markdown")
    await update.message.reply_text("✅ Фото отправлено")


# ═══════════════════════════════════════════════════════════════════════════════
#  Scheduled daily report
# ═══════════════════════════════════════════════════════════════════════════════

async def _daily_report_job(bot) -> None:
    logger.info("Sending scheduled daily report")
    tz = pytz.timezone(TZ_NAME)
    today = datetime.now(tz)
    text = await build_sales_text(today, today, f"Итоги дня {today.strftime('%d.%m.%Y')}")
    try:
        await bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Failed to send daily report: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Scheduler — must start inside async context (post_init)
    tz = pytz.timezone(TZ_NAME)
    scheduler = AsyncIOScheduler(timezone=tz)

    async def post_init(application: Application) -> None:
        scheduler.add_job(
            _daily_report_job,
            "cron",
            hour=REPORT_HOUR,
            minute=REPORT_MINUTE,
            args=[application.bot],
        )
        scheduler.start()
        logger.info("Scheduler started. Next report at %02d:%02d %s", REPORT_HOUR, REPORT_MINUTE, TZ_NAME)

    async def post_shutdown(application: Application) -> None:
        if scheduler.running:
            scheduler.shutdown()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Conversations
    open_conv = ConversationHandler(
        entry_points=[CommandHandler("open_shift", open_start)],
        states={OPEN_PHOTO: [MessageHandler(filters.PHOTO, open_photo)]},
        fallbacks=[CommandHandler("cancel", conv_cancel)],
    )
    close_conv = ConversationHandler(
        entry_points=[CommandHandler("close_shift", close_start)],
        states={
            CLOSE_PHOTO: [MessageHandler(filters.PHOTO, close_photo)],
            CLOSE_CASH:  [MessageHandler(filters.TEXT & ~filters.COMMAND, close_cash)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
    )

    # Handlers
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("myid",        cmd_myid))
    app.add_handler(CommandHandler("staff",       cmd_staff))
    app.add_handler(CommandHandler("sales",       cmd_sales))
    app.add_handler(CommandHandler("week",        cmd_week))
    app.add_handler(open_conv)
    app.add_handler(close_conv)
    # Free-form photo report (lowest priority)
    app.add_handler(MessageHandler(filters.PHOTO, photo_report))

    logger.info("Flower shop bot started. Owner ID: %s", OWNER_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
