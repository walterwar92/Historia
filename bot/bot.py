#!/usr/bin/env python3
"""
Hysteria 2 Telegram Bot — Main entry point

Features:
  - Password-protected access
  - Multi-key management (create / list / delete / block)
  - Client config & URI generation
  - Server status, traffic stats, logs
  - Inline keyboard UI
  - HTTP auth backend for Hysteria
"""
import asyncio
import html
import logging
import subprocess
import time
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from auth_backend import AuthBackend
from config import load_config, DATA_DIR
from database import KeyDatabase

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hysteria-bot")

# ── Globals ─────────────────────────────────────────────────────────────────
cfg = load_config()
db = KeyDatabase(cfg.get("db_path", f"{DATA_DIR}/keys.db"))
bot = Bot(token=cfg["bot_token"], parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)
auth_backend = AuthBackend(db, port=cfg.get("auth_backend_port", 8787))

ADMIN_PASSWORD = cfg["admin_password"]


# ── Helpers ─────────────────────────────────────────────────────────────────
def is_authed(telegram_id: int) -> bool:
    return db.is_authed(telegram_id)


def require_auth(func):
    """Decorator: require Telegram user to be authenticated."""
    async def wrapper(event, *args, **kwargs):
        tg_id = event.from_user.id if isinstance(event, (Message, CallbackQuery)) else 0
        if not is_authed(tg_id):
            text = "🔒 <b>Доступ закрыт.</b>\nВведите пароль администратора:"
            if isinstance(event, CallbackQuery):
                await event.answer("🔒 Сначала авторизуйтесь", show_alert=True)
                await event.message.answer(text)
            else:
                await event.answer(text)
            return
        return await func(event, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


def ts_format(ts: float) -> str:
    if ts <= 0:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔑 Создать ключ", callback_data="key_create"),
            InlineKeyboardButton(text="📋 Список ключей", callback_data="key_list"),
        ],
        [
            InlineKeyboardButton(text="📊 Статус сервера", callback_data="srv_status"),
            InlineKeyboardButton(text="📈 Трафик", callback_data="srv_stats"),
        ],
        [
            InlineKeyboardButton(text="👥 Онлайн", callback_data="srv_online"),
            InlineKeyboardButton(text="📜 Логи", callback_data="srv_logs"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезапуск", callback_data="srv_restart"),
        ],
    ])


def key_detail_kb(key_id: int, active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🚫 Заблокировать" if active else "✅ Разблокировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Конфиг клиента", callback_data=f"key_cfg:{key_id}"),
        ],
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"key_toggle:{key_id}"),
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"key_del:{key_id}"),
        ],
        [
            InlineKeyboardButton(text="« Назад", callback_data="key_list"),
        ],
    ])


def build_client_config(key: str) -> str:
    ip = cfg.get("server_ip", "YOUR_SERVER_IP")
    port = cfg.get("server_port", 443)
    obfs = cfg.get("obfs_password", "")

    config_text = (
        f"server: {ip}:{port}\n\n"
        f"auth: {key}\n\n"
        f"tls:\n  insecure: true\n\n"
    )
    if obfs:
        config_text += (
            f"obfs:\n"
            f"  type: salamander\n"
            f"  salamander:\n"
            f"    password: {obfs}\n\n"
        )
    config_text += (
        "socks5:\n  listen: 127.0.0.1:1080\n\n"
        "http:\n  listen: 127.0.0.1:8080\n"
    )
    return config_text


def build_uri(key: str) -> str:
    ip = cfg.get("server_ip", "YOUR_SERVER_IP")
    port = cfg.get("server_port", 443)
    obfs = cfg.get("obfs_password", "")

    uri = f"hy2://{key}@{ip}:{port}?"
    if obfs:
        uri += f"obfs=salamander&obfs-password={obfs}&"
    uri += "insecure=1#Hysteria2-VPN"
    return uri


# ── Handlers ────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    if is_authed(msg.from_user.id):
        await msg.answer(
            "👋 <b>Hysteria 2 VPN Manager</b>\n\nВыберите действие:",
            reply_markup=main_menu_kb(),
        )
    else:
        await msg.answer(
            "🔒 <b>Hysteria 2 VPN Manager</b>\n\n"
            "Для доступа введите пароль администратора:"
        )


@router.message(Command("menu"))
@require_auth
async def cmd_menu(msg: Message):
    await msg.answer("📌 <b>Главное меню</b>", reply_markup=main_menu_kb())


@router.message(Command("logout"))
async def cmd_logout(msg: Message):
    db.revoke_auth(msg.from_user.id)
    await msg.answer("🔓 Сессия завершена. Введите пароль для повторного входа.")


# ── Password handler (non-authed users) ─────────────────────────────────────

@router.message(~F.text.startswith("/"))
async def handle_text(msg: Message):
    if is_authed(msg.from_user.id):
        await msg.answer("Используйте меню:", reply_markup=main_menu_kb())
        return

    # Try to authenticate
    if msg.text and msg.text.strip() == ADMIN_PASSWORD:
        db.set_authed(msg.from_user.id)
        await msg.answer(
            "✅ <b>Авторизация успешна!</b>\n\nВыберите действие:",
            reply_markup=main_menu_kb(),
        )
        # Delete password message for security
        try:
            await msg.delete()
        except Exception:
            pass
    else:
        await msg.answer("❌ Неверный пароль. Попробуйте ещё раз.")


# ── Key management callbacks ────────────────────────────────────────────────

@router.callback_query(F.data == "key_create")
@require_auth
async def cb_key_create(cb: CallbackQuery):
    key_info = db.create_key(label="", created_by=cb.from_user.id)
    uri = build_uri(key_info["key"])

    text = (
        f"✅ <b>Ключ создан!</b>\n\n"
        f"🆔 ID: <code>{key_info['id']}</code>\n"
        f"🔑 Ключ: <code>{key_info['key']}</code>\n"
        f"📅 Создан: {ts_format(key_info['created_at'])}\n\n"
        f"📱 <b>URI для мобильного:</b>\n"
        f"<code>{html.escape(uri)}</code>\n\n"
        f"☝️ Скопируйте URI и вставьте в Hysteria-приложение."
    )
    await cb.message.edit_text(
        text,
        reply_markup=key_detail_kb(key_info["id"], True),
    )
    await cb.answer()


@router.callback_query(F.data == "key_list")
@require_auth
async def cb_key_list(cb: CallbackQuery):
    keys = db.list_keys()
    if not keys:
        await cb.message.edit_text(
            "📋 <b>Ключей нет.</b>\nСоздайте первый ключ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="key_create")],
                [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
            ]),
        )
        await cb.answer()
        return

    buttons = []
    for k in keys:
        status = "✅" if k["active"] else "🚫"
        label = k["label"] or f"Key #{k['id']}"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {label} — {k['key'][:8]}…",
                callback_data=f"key_view:{k['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="« Меню", callback_data="main_menu")])

    counts = db.count_keys()
    await cb.message.edit_text(
        f"📋 <b>Ключи</b> ({counts['active']} активных / {counts['total']} всего)\n\n"
        "Нажмите на ключ для деталей:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("key_view:"))
@require_auth
async def cb_key_view(cb: CallbackQuery):
    key_id = int(cb.data.split(":")[1])
    k = db.get_key_by_id(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    status = "✅ Активен" if k["active"] else "🚫 Заблокирован"
    expires = ts_format(k["expires_at"]) if k["expires_at"] > 0 else "Бессрочный"

    text = (
        f"🔑 <b>Ключ #{k['id']}</b>\n\n"
        f"Статус: {status}\n"
        f"Ключ: <code>{k['key']}</code>\n"
        f"Метка: {k['label'] or '—'}\n"
        f"Создан: {ts_format(k['created_at'])}\n"
        f"Истекает: {expires}\n"
    )
    await cb.message.edit_text(
        text,
        reply_markup=key_detail_kb(k["id"], bool(k["active"])),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("key_cfg:"))
@require_auth
async def cb_key_config(cb: CallbackQuery):
    key_id = int(cb.data.split(":")[1])
    k = db.get_key_by_id(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    client_cfg = build_client_config(k["key"])
    uri = build_uri(k["key"])

    text = (
        f"📱 <b>Конфиг для ключа #{k['id']}</b>\n\n"
        f"<b>config.yaml:</b>\n<pre>{html.escape(client_cfg)}</pre>\n\n"
        f"<b>URI (для мобильного):</b>\n<code>{html.escape(uri)}</code>"
    )
    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад к ключу", callback_data=f"key_view:{key_id}")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("key_toggle:"))
@require_auth
async def cb_key_toggle(cb: CallbackQuery):
    key_id = int(cb.data.split(":")[1])
    k = db.toggle_key(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    status = "✅ разблокирован" if k["active"] else "🚫 заблокирован"
    await cb.answer(f"Ключ #{key_id} {status}", show_alert=True)

    # Refresh the key view
    await cb_key_view(cb)


@router.callback_query(F.data.startswith("key_del_confirm:"))
@require_auth
async def cb_key_del_confirm(cb: CallbackQuery):
    key_id = int(cb.data.split(":")[1])
    success = db.delete_key(key_id)
    if success:
        await cb.answer(f"Ключ #{key_id} удалён", show_alert=True)
        # Go back to list
        await cb_key_list(cb)
    else:
        await cb.answer("Ключ не найден", show_alert=True)


@router.callback_query(F.data.startswith("key_del:"))
@require_auth
async def cb_key_del(cb: CallbackQuery):
    key_id = int(cb.data.split(":")[1])
    await cb.message.edit_text(
        f"⚠️ <b>Удалить ключ #{key_id}?</b>\n\nЭто действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"key_del_confirm:{key_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"key_view:{key_id}"),
            ],
        ]),
    )
    await cb.answer()


# ── Server management callbacks ─────────────────────────────────────────────

@router.callback_query(F.data == "srv_status")
@require_auth
async def cb_srv_status(cb: CallbackQuery):
    try:
        result = subprocess.run(
            ["systemctl", "status", cfg["hysteria_service"]],
            capture_output=True, text=True, timeout=10,
        )
        status_text = result.stdout or result.stderr or "Нет данных"
        # Trim to avoid Telegram message limit
        if len(status_text) > 3500:
            status_text = status_text[:3500] + "\n…"
    except Exception as e:
        status_text = f"Ошибка: {e}"

    counts = db.count_keys()

    text = (
        f"📊 <b>Статус сервера</b>\n\n"
        f"🔑 Ключи: {counts['active']} активных / {counts['total']} всего\n\n"
        f"<pre>{html.escape(status_text)}</pre>"
    )
    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_stats")
@require_auth
async def cb_srv_stats(cb: CallbackQuery):
    stats_listen = cfg.get("stats_listen", "127.0.0.1:9090")
    secret = cfg.get("stats_secret", "")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://{stats_listen}/traffic?secret={secret}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()

        if not data:
            text = "📈 <b>Трафик</b>\n\nНет данных о трафике."
        else:
            lines = []
            for user_key, traffic in data.items():
                short_key = user_key[:12] + "…"
                tx = human_bytes(traffic.get("tx", 0))
                rx = human_bytes(traffic.get("rx", 0))
                lines.append(f"<code>{short_key}</code>  ⬆{tx}  ⬇{rx}")
            text = f"📈 <b>Трафик ({len(data)} сессий)</b>\n\n" + "\n".join(lines)
    except Exception as e:
        text = f"📈 <b>Трафик</b>\n\n⚠️ Не удалось получить данные: {e}"

    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="srv_stats")],
            [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_online")
@require_auth
async def cb_srv_online(cb: CallbackQuery):
    stats_listen = cfg.get("stats_listen", "127.0.0.1:9090")
    secret = cfg.get("stats_secret", "")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://{stats_listen}/online?secret={secret}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()

        if not data:
            text = "👥 <b>Онлайн</b>\n\nНикто не подключён."
        else:
            total_online = sum(data.values()) if isinstance(data, dict) else 0
            lines = []
            for user_key, count in data.items():
                short_key = user_key[:12] + "…"
                lines.append(f"<code>{short_key}</code>  — {count} устр.")
            text = (
                f"👥 <b>Онлайн: {total_online} подключений</b>\n\n"
                + "\n".join(lines)
            )
    except Exception as e:
        text = f"👥 <b>Онлайн</b>\n\n⚠️ Не удалось получить данные: {e}"

    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="srv_online")],
            [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_logs")
@require_auth
async def cb_srv_logs(cb: CallbackQuery):
    try:
        result = subprocess.run(
            ["journalctl", "-u", cfg["hysteria_service"], "--no-pager",
             "-n", str(cfg.get("log_lines", 50))],
            capture_output=True, text=True, timeout=10,
        )
        logs = result.stdout or "Нет логов."
        if len(logs) > 3800:
            logs = logs[-3800:]
    except Exception as e:
        logs = f"Ошибка: {e}"

    await cb.message.edit_text(
        f"📜 <b>Логи</b> (последние {cfg.get('log_lines', 50)} строк)\n\n"
        f"<pre>{html.escape(logs)}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="srv_logs")],
            [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_restart")
@require_auth
async def cb_srv_restart(cb: CallbackQuery):
    await cb.message.edit_text(
        "⚠️ <b>Перезапустить Hysteria?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="srv_restart_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"),
            ],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_restart_confirm")
@require_auth
async def cb_srv_restart_confirm(cb: CallbackQuery):
    try:
        subprocess.run(
            ["systemctl", "restart", cfg["hysteria_service"]],
            timeout=15, check=True,
        )
        await cb.message.edit_text(
            "✅ <b>Сервис перезапущен.</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
            ]),
        )
    except Exception as e:
        await cb.message.edit_text(
            f"❌ <b>Ошибка перезапуска:</b> {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
            ]),
        )
    await cb.answer()


@router.callback_query(F.data == "main_menu")
@require_auth
async def cb_main_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "📌 <b>Главное меню</b>\n\nВыберите действие:",
        reply_markup=main_menu_kb(),
    )
    await cb.answer()


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    # Start auth backend
    log.info(f"Starting auth backend on 127.0.0.1:{cfg.get('auth_backend_port', 8787)}")
    await auth_backend.start()

    # Start bot polling
    log.info("Starting Telegram bot...")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await auth_backend.stop()


if __name__ == "__main__":
    asyncio.run(main())
