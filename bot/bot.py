#!/usr/bin/env python3
"""
Hysteria 2 Telegram Bot
  - Role-based access (Admin / User)
  - Invite-code registration
  - Multi-key management
  - Server monitoring
  - HTTP auth backend for Hysteria
"""
import asyncio
import html
import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from auth_backend import AuthBackend
from config import load_config, DATA_DIR
from database import KeyDatabase
from tt_sync import sync_tt_credentials, reload_tt_service

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hysteria-bot")

# ── Globals ─────────────────────────────────────────────────────────────────
cfg = load_config()
db = KeyDatabase(cfg.get("db_path", f"{DATA_DIR}/keys.db"))
bot = Bot(
    token=cfg["bot_token"],
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
auth_backend = AuthBackend(db, port=cfg.get("auth_backend_port", 8787))

ADMIN_PASSWORD = cfg["admin_password"]

# ── Visual constants ────────────────────────────────────────────────────────
LINE = "─" * 28
HEADER_LINE = "━" * 28


# ── Auth Middleware ─────────────────────────────────────────────────────────

class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        tg_id = user.id

        # Always allow /start
        if isinstance(event, Message) and event.text:
            text = event.text.strip()
            if text.startswith("/"):
                cmd = text.split()[0].lstrip("/").split("@")[0]
                if cmd in ("start",):
                    return await handler(event, data)

        # Always allow plain text (for password / invite code input)
        if isinstance(event, Message) and event.text and not event.text.startswith("/"):
            return await handler(event, data)

        # Check registration
        db_user = db.get_user(tg_id)
        if not db_user:
            if isinstance(event, CallbackQuery):
                await event.answer(
                    "Вы не зарегистрированы.\nОтправьте /start",
                    show_alert=True,
                )
            elif isinstance(event, Message):
                await event.answer("Вы не зарегистрированы. Отправьте /start")
            return None

        # Inject role into data for handlers
        data["user_role"] = db_user["role"]
        data["db_user"] = db_user
        return await handler(event, data)


router.message.middleware(AuthMiddleware())
router.callback_query.middleware(AuthMiddleware())
dp.include_router(router)


# ── Helpers ─────────────────────────────────────────────────────────────────

def is_admin(role: str) -> bool:
    return role == "admin"


def ts_fmt(ts: float) -> str:
    if ts <= 0:
        return "---"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M")


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_username(user) -> str:
    """Extract display name from Telegram user object or dict."""
    if isinstance(user, dict):
        return user.get("username", "") or ""
    if hasattr(user, "username") and user.username:
        return f"@{user.username}"
    if hasattr(user, "first_name"):
        return user.first_name or ""
    return ""


# ── Keyboards ───────────────────────────────────────────────────────────────

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+ Создать ключ", callback_data="key_create"),
            InlineKeyboardButton(text="Мои ключи", callback_data="my_keys"),
        ],
        [
            InlineKeyboardButton(text="Все ключи", callback_data="key_list"),
            InlineKeyboardButton(text="Пользователи", callback_data="user_list"),
        ],
        [
            InlineKeyboardButton(text="Инвайт-коды", callback_data="invite_list"),
            InlineKeyboardButton(text="+ Инвайт", callback_data="invite_create"),
        ],
        [
            InlineKeyboardButton(text="Статус", callback_data="srv_status"),
            InlineKeyboardButton(text="Трафик", callback_data="srv_stats"),
        ],
        [
            InlineKeyboardButton(text="Онлайн", callback_data="srv_online"),
            InlineKeyboardButton(text="Логи Hy2", callback_data="srv_logs"),
        ],
        [
            InlineKeyboardButton(text="Логи TT", callback_data="tt_logs"),
            InlineKeyboardButton(text="TT Статус", callback_data="tt_status"),
        ],
        [
            InlineKeyboardButton(text="Перезапуск серверов", callback_data="srv_restart"),
        ],
    ])


def user_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+ Создать ключ", callback_data="key_create"),
            InlineKeyboardButton(text="Мои ключи", callback_data="my_keys"),
        ],
        [
            InlineKeyboardButton(text="Статус сервера", callback_data="srv_status"),
        ],
    ])


def menu_kb(role: str) -> InlineKeyboardMarkup:
    return admin_menu_kb() if is_admin(role) else user_menu_kb()


def back_menu_btn() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="<< Меню", callback_data="main_menu")]


def key_detail_kb(key_id: int, active: bool, role: str, is_owner: bool) -> InlineKeyboardMarkup:
    rows = []
    rows.append([
        InlineKeyboardButton(text="Конфиг / URI", callback_data=f"key_cfg:{key_id}"),
    ])
    if is_owner or is_admin(role):
        regen_btn = InlineKeyboardButton(text="Перевыпустить", callback_data=f"key_regen:{key_id}")
        if is_admin(role):
            toggle = "Заблокировать" if active else "Разблокировать"
            rows.append([
                regen_btn,
                InlineKeyboardButton(text=toggle, callback_data=f"key_toggle:{key_id}"),
            ])
            rows.append([
                InlineKeyboardButton(text="Удалить", callback_data=f"key_del:{key_id}"),
            ])
        else:
            rows.append([regen_btn])
    back_cb = "key_list" if is_admin(role) else "my_keys"
    rows.append([InlineKeyboardButton(text="<< Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Config builders ─────────────────────────────────────────────────────────

def build_client_config(key: str) -> str:
    ip = cfg.get("server_ip", "YOUR_SERVER_IP")
    port = cfg.get("server_port", 443)
    config_text = (
        f"server: {ip}:{port}\n\n"
        f"auth: {key}\n\n"
        f"tls:\n  insecure: true\n\n"
        "quic:\n"
        "  initStreamReceiveWindow: 524288\n"
        "  maxStreamReceiveWindow: 2097152\n"
        "  initConnReceiveWindow: 1048576\n"
        "  maxConnReceiveWindow: 4194304\n\n"
        "fastOpen: true\n\n"
        "socks5:\n  listen: 127.0.0.1:1080\n\n"
        "http:\n  listen: 127.0.0.1:8080\n"
    )
    return config_text


def build_uri(key: str) -> str:
    ip = cfg.get("server_ip", "YOUR_SERVER_IP")
    port = cfg.get("server_port", 443)
    uri = f"hy2://{key}@{ip}:{port}?insecure=1#Hysteria2-VPN"
    return uri


# ── TrustTunnel config builders ───────────────────────────────────────────

def build_tt_client_config(key: str) -> str:
    ip = cfg.get("server_ip", "YOUR_SERVER_IP")
    port = cfg.get("tt_server_port", 443)
    return (
        f'[endpoint]\n'
        f'hostname = "{ip}"\n'
        f'addresses = ["{ip}:{port}"]\n'
        f'username = "{key}"\n'
        f'password = "{key}"\n'
        f'skip_verification = true\n'
        f'upstream_protocol = "http2"\n'
        f'\n'
        f'[listener.socks]\n'
        f'address = "127.0.0.1:1080"\n'
    )


def build_tt_uri(key: str) -> str:
    """Generate tt:// deep-link URI for TrustTunnel client."""
    ip = cfg.get("server_ip", "YOUR_SERVER_IP")
    port = cfg.get("tt_server_port", 443)
    # tt:// URI generated by endpoint binary; here we build a manual link
    # that the TrustTunnel CLI/app can import
    return f"tt://{ip}:{port}?username={key}&password={key}&skip_verification=true#TrustTunnel"


def _do_tt_sync():
    """Sync TrustTunnel credentials and reload the service."""
    cred_path = cfg.get("tt_credentials_path", "/etc/hysteria/bot/tt_credentials.txt")
    sync_tt_credentials(db, cred_path)
    reload_tt_service(cfg.get("tt_service", "trusttunnel"))


# ════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ════════════════════════════════════════════════════════════════════════════

# ── /start ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    db_user = db.get_user(msg.from_user.id)
    if db_user:
        role_tag = "ADMIN" if is_admin(db_user["role"]) else "USER"
        await msg.answer(
            f"{HEADER_LINE}\n"
            f"  <b>Hysteria 2 VPN</b>  [{role_tag}]\n"
            f"{HEADER_LINE}\n\n"
            f"Добро пожаловать, <b>{html.escape(get_username(msg.from_user))}</b>",
            reply_markup=menu_kb(db_user["role"]),
        )
    else:
        await msg.answer(
            f"{HEADER_LINE}\n"
            f"  <b>Hysteria 2 VPN</b>\n"
            f"{HEADER_LINE}\n\n"
            "Для доступа введите:\n"
            "  -- <b>Пароль администратора</b> (первый вход)\n"
            "  -- <b>Инвайт-код</b> (от администратора)"
        )


# ── Text input: password / invite code ──────────────────────────────────────

@router.message(F.text)
async def handle_text(msg: Message):
    if msg.text.startswith("/"):
        return

    tg_id = msg.from_user.id
    text = msg.text.strip()

    # Already registered — show menu
    db_user = db.get_user(tg_id)
    if db_user:
        await msg.answer(
            "Используйте меню:",
            reply_markup=menu_kb(db_user["role"]),
        )
        return

    # Try admin password
    if text == ADMIN_PASSWORD:
        existing_admin = db.get_user(tg_id)
        if not existing_admin:
            db.create_user(
                telegram_id=tg_id,
                username=get_username(msg.from_user),
                role="admin",
                max_keys=999,
                invited_by=0,
            )
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(
            f"{HEADER_LINE}\n"
            f"  <b>Hysteria 2 VPN</b>  [ADMIN]\n"
            f"{HEADER_LINE}\n\n"
            "Вы зарегистрированы как <b>администратор</b>.",
            reply_markup=admin_menu_kb(),
        )
        return

    # Try invite code
    invite_user = db.use_invite(text, tg_id, get_username(msg.from_user))
    if invite_user:
        try:
            await msg.delete()
        except Exception:
            pass
        role_tag = "ADMIN" if is_admin(invite_user["role"]) else "USER"
        await msg.answer(
            f"{HEADER_LINE}\n"
            f"  <b>Hysteria 2 VPN</b>  [{role_tag}]\n"
            f"{HEADER_LINE}\n\n"
            f"Инвайт принят! Ваша роль: <b>{invite_user['role']}</b>\n"
            f"Лимит ключей: <b>{invite_user['max_keys']}</b>",
            reply_markup=menu_kb(invite_user["role"]),
        )
        return

    await msg.answer("Неверный пароль или инвайт-код. Попробуйте снова.")


# ── Main menu ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, user_role: str = "user", **kwargs):
    role_tag = "ADMIN" if is_admin(user_role) else "USER"
    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Hysteria 2 VPN</b>  [{role_tag}]\n"
        f"{HEADER_LINE}\n\n"
        "Выберите действие:",
        reply_markup=menu_kb(user_role),
    )
    await cb.answer()


# ════════════════════════════════════════════════════════════════════════════
#  KEY MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "key_create")
async def cb_key_create(cb: CallbackQuery, user_role: str = "user", db_user: dict = None, **kwargs):
    tg_id = cb.from_user.id
    if db_user:
        max_k = db_user.get("max_keys", 1)
        current = db.count_user_keys(tg_id)
        if not is_admin(user_role) and current >= max_k:
            await cb.answer(
                f"Лимит ключей: {max_k}. У вас уже {current}.",
                show_alert=True,
            )
            return

    key_info = db.create_key(label="", created_by=tg_id)
    uri = build_uri(key_info["key"])
    tt_uri = build_tt_uri(key_info["key"])

    _do_tt_sync()

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Ключ создан</b>\n"
        f"{HEADER_LINE}\n\n"
        f"  ID:    <code>{key_info['id']}</code>\n"
        f"  Ключ:  <code>{key_info['key']}</code>\n"
        f"  Дата:  {ts_fmt(key_info['created_at'])}\n\n"
        f"{LINE}\n"
        f"<b>Hysteria 2 URI:</b>\n"
        f"<code>{html.escape(uri)}</code>\n\n"
        f"<b>TrustTunnel URI:</b>\n"
        f"<code>{html.escape(tt_uri)}</code>\n\n"
        f"Скопируйте URI и вставьте в приложение.",
        reply_markup=key_detail_kb(key_info["id"], True, user_role, True),
    )
    await cb.answer()


# ── My keys (for both roles) ───────────────────────────────────────────────

@router.callback_query(F.data == "my_keys")
async def cb_my_keys(cb: CallbackQuery, user_role: str = "user", **kwargs):
    keys = db.list_keys_by_user(cb.from_user.id)
    if not keys:
        await cb.message.edit_text(
            f"{HEADER_LINE}\n"
            f"  <b>Мои ключи</b>\n"
            f"{HEADER_LINE}\n\n"
            "У вас пока нет ключей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="+ Создать ключ", callback_data="key_create")],
                back_menu_btn(),
            ]),
        )
        await cb.answer()
        return

    buttons = []
    for k in keys:
        st = "ON " if k["active"] else "OFF"
        buttons.append([
            InlineKeyboardButton(
                text=f"[{st}] #{k['id']} -- {k['key'][:10]}...",
                callback_data=f"key_view:{k['id']}",
            )
        ])
    buttons.append(back_menu_btn())

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Мои ключи</b>  ({len(keys)} шт.)\n"
        f"{HEADER_LINE}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


# ── All keys (admin only) ──────────────────────────────────────────────────

@router.callback_query(F.data == "key_list")
async def cb_key_list(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    keys = db.list_keys()
    if not keys:
        await cb.message.edit_text(
            f"{HEADER_LINE}\n"
            f"  <b>Все ключи</b>\n"
            f"{HEADER_LINE}\n\n"
            "Ключей пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="+ Создать ключ", callback_data="key_create")],
                back_menu_btn(),
            ]),
        )
        await cb.answer()
        return

    buttons = []
    for k in keys:
        st = "ON " if k["active"] else "OFF"
        owner = db.get_user(k["created_by"])
        owner_name = owner["username"][:8] if owner and owner["username"] else str(k["created_by"])
        buttons.append([
            InlineKeyboardButton(
                text=f"[{st}] #{k['id']} | {owner_name} | {k['key'][:8]}...",
                callback_data=f"key_view:{k['id']}",
            )
        ])
    buttons.append(back_menu_btn())

    counts = db.count_keys()
    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Все ключи</b>\n"
        f"{HEADER_LINE}\n\n"
        f"  Активных: {counts['active']}\n"
        f"  Заблокировано: {counts['blocked']}\n"
        f"  Всего: {counts['total']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


# ── Key view ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("key_view:"))
async def cb_key_view(cb: CallbackQuery, user_role: str = "user", **kwargs):
    key_id = int(cb.data.split(":")[1])
    k = db.get_key_by_id(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    is_owner = k["created_by"] == cb.from_user.id
    if not is_owner and not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    status = "Активен" if k["active"] else "ЗАБЛОКИРОВАН"
    expires = ts_fmt(k["expires_at"]) if k["expires_at"] > 0 else "Бессрочный"
    owner = db.get_user(k["created_by"])
    owner_name = owner["username"] if owner and owner["username"] else str(k["created_by"])

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Ключ #{k['id']}</b>\n"
        f"{HEADER_LINE}\n\n"
        f"  Статус:     {status}\n"
        f"  Ключ:       <code>{k['key']}</code>\n"
        f"  Владелец:   {html.escape(owner_name)}\n"
        f"  Создан:     {ts_fmt(k['created_at'])}\n"
        f"  Истекает:   {expires}",
        reply_markup=key_detail_kb(k["id"], bool(k["active"]), user_role, is_owner),
    )
    await cb.answer()


# ── Key config/URI ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("key_cfg:"))
async def cb_key_config(cb: CallbackQuery, user_role: str = "user", **kwargs):
    key_id = int(cb.data.split(":")[1])
    k = db.get_key_by_id(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    is_owner = k["created_by"] == cb.from_user.id
    if not is_owner and not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    client_cfg = build_client_config(k["key"])
    uri = build_uri(k["key"])
    tt_cfg = build_tt_client_config(k["key"])
    tt_uri = build_tt_uri(k["key"])

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Конфиг: Ключ #{k['id']}</b>\n"
        f"{HEADER_LINE}\n\n"
        f"<b>━━ Hysteria 2 (макс. скорость) ━━</b>\n\n"
        f"<b>config.yaml:</b>\n"
        f"<pre>{html.escape(client_cfg)}</pre>\n"
        f"<b>URI:</b>\n"
        f"<code>{html.escape(uri)}</code>\n\n"
        f"{LINE}\n\n"
        f"<b>━━ TrustTunnel (обход DPI) ━━</b>\n\n"
        f"<b>config.toml:</b>\n"
        f"<pre>{html.escape(tt_cfg)}</pre>\n"
        f"<b>URI:</b>\n"
        f"<code>{html.escape(tt_uri)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="<< Назад", callback_data=f"key_view:{key_id}")],
        ]),
    )
    await cb.answer()


# ── Key regenerate ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("key_regen:"))
async def cb_key_regen(cb: CallbackQuery, user_role: str = "user", **kwargs):
    key_id = int(cb.data.split(":")[1])
    k = db.get_key_by_id(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    is_owner = k["created_by"] == cb.from_user.id
    if not is_owner and not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await cb.message.edit_text(
        f"{LINE}\n"
        f"<b>Перевыпустить ключ #{key_id}?</b>\n\n"
        f"Старый ключ перестанет работать.\n"
        f"Будет создан новый.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, перевыпустить", callback_data=f"key_regen_yes:{key_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"key_view:{key_id}"),
            ],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("key_regen_yes:"))
async def cb_key_regen_yes(cb: CallbackQuery, user_role: str = "user", **kwargs):
    key_id = int(cb.data.split(":")[1])
    k = db.get_key_by_id(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    is_owner = k["created_by"] == cb.from_user.id
    if not is_owner and not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    new_key = db.regenerate_key(key_id)
    if not new_key:
        await cb.answer("Ошибка перевыпуска", show_alert=True)
        return

    _do_tt_sync()

    uri = build_uri(new_key["key"])
    tt_uri = build_tt_uri(new_key["key"])
    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Ключ перевыпущен</b>\n"
        f"{HEADER_LINE}\n\n"
        f"  Старый ID: {key_id}  (удалён)\n"
        f"  Новый ID:  <code>{new_key['id']}</code>\n"
        f"  Ключ:      <code>{new_key['key']}</code>\n\n"
        f"{LINE}\n"
        f"<b>Hysteria 2 URI:</b>\n"
        f"<code>{html.escape(uri)}</code>\n\n"
        f"<b>TrustTunnel URI:</b>\n"
        f"<code>{html.escape(tt_uri)}</code>",
        reply_markup=key_detail_kb(new_key["id"], True, user_role, True),
    )
    await cb.answer("Ключ перевыпущен")


# ── Key toggle (admin) ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("key_toggle:"))
async def cb_key_toggle(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    key_id = int(cb.data.split(":")[1])
    k = db.toggle_key(key_id)
    if not k:
        await cb.answer("Ключ не найден", show_alert=True)
        return

    _do_tt_sync()

    status = "разблокирован" if k["active"] else "заблокирован"
    await cb.answer(f"Ключ #{key_id} {status}", show_alert=True)

    # Refresh view
    status_text = "Активен" if k["active"] else "ЗАБЛОКИРОВАН"
    expires = ts_fmt(k["expires_at"]) if k["expires_at"] > 0 else "Бессрочный"
    owner = db.get_user(k["created_by"])
    owner_name = owner["username"] if owner and owner["username"] else str(k["created_by"])
    is_owner = k["created_by"] == cb.from_user.id

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Ключ #{k['id']}</b>\n"
        f"{HEADER_LINE}\n\n"
        f"  Статус:     {status_text}\n"
        f"  Ключ:       <code>{k['key']}</code>\n"
        f"  Владелец:   {html.escape(owner_name)}\n"
        f"  Создан:     {ts_fmt(k['created_at'])}\n"
        f"  Истекает:   {expires}",
        reply_markup=key_detail_kb(k["id"], bool(k["active"]), user_role, is_owner),
    )


# ── Key delete (admin) ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("key_del:"))
async def cb_key_del(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    key_id = int(cb.data.split(":")[1])
    await cb.message.edit_text(
        f"{LINE}\n"
        f"<b>Удалить ключ #{key_id}?</b>\n\n"
        f"Это действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"key_del_yes:{key_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"key_view:{key_id}"),
            ],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("key_del_yes:"))
async def cb_key_del_yes(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    key_id = int(cb.data.split(":")[1])
    db.delete_key(key_id)
    _do_tt_sync()
    await cb.answer(f"Ключ #{key_id} удалён", show_alert=True)

    # Return to all keys
    keys = db.list_keys()
    if not keys:
        await cb.message.edit_text(
            f"{HEADER_LINE}\n  <b>Все ключи</b>\n{HEADER_LINE}\n\nКлючей нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="+ Создать ключ", callback_data="key_create")],
                back_menu_btn(),
            ]),
        )
    else:
        buttons = []
        for k in keys:
            st = "ON " if k["active"] else "OFF"
            buttons.append([
                InlineKeyboardButton(
                    text=f"[{st}] #{k['id']} | {k['key'][:8]}...",
                    callback_data=f"key_view:{k['id']}",
                )
            ])
        buttons.append(back_menu_btn())
        counts = db.count_keys()
        await cb.message.edit_text(
            f"{HEADER_LINE}\n  <b>Все ключи</b>\n{HEADER_LINE}\n\n"
            f"  Активных: {counts['active']}  |  Всего: {counts['total']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


# ════════════════════════════════════════════════════════════════════════════
#  INVITE CODES (admin only)
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "invite_create")
async def cb_invite_create(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Создать инвайт</b>\n"
        f"{HEADER_LINE}\n\n"
        "Выберите роль для приглашённого:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="User (1 ключ)", callback_data="invite_mk:user:1"),
                InlineKeyboardButton(text="User (3 ключа)", callback_data="invite_mk:user:3"),
            ],
            [
                InlineKeyboardButton(text="User (5 ключей)", callback_data="invite_mk:user:5"),
                InlineKeyboardButton(text="Admin", callback_data="invite_mk:admin:999"),
            ],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("invite_mk:"))
async def cb_invite_mk(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    parts = cb.data.split(":")
    role = parts[1]
    max_keys = int(parts[2])

    invite = db.create_invite(role=role, max_keys=max_keys, created_by=cb.from_user.id)
    role_tag = "ADMIN" if role == "admin" else "USER"

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Инвайт создан</b>  [{role_tag}]\n"
        f"{HEADER_LINE}\n\n"
        f"  Код:      <code>{invite['code']}</code>\n"
        f"  Роль:     {role}\n"
        f"  Ключей:   {max_keys}\n\n"
        f"Отправьте этот код пользователю.\n"
        f"Код одноразовый.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="+ Ещё инвайт", callback_data="invite_create")],
            [InlineKeyboardButton(text="Список инвайтов", callback_data="invite_list")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "invite_list")
async def cb_invite_list(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    invites = db.list_invites()
    if not invites:
        await cb.message.edit_text(
            f"{HEADER_LINE}\n  <b>Инвайт-коды</b>\n{HEADER_LINE}\n\n"
            "Нет инвайтов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="+ Создать", callback_data="invite_create")],
                back_menu_btn(),
            ]),
        )
        await cb.answer()
        return

    lines = []
    buttons = []
    for inv in invites:
        used = "ИСПОЛЬЗОВАН" if inv["used_by"] else "СВОБОДЕН"
        role_tag = "A" if inv["role"] == "admin" else "U"
        lines.append(
            f"  <code>{inv['code']}</code>  [{role_tag}]  {used}"
        )
        if not inv["used_by"]:
            buttons.append([
                InlineKeyboardButton(
                    text=f"Удалить: {inv['code']}",
                    callback_data=f"invite_del:{inv['code']}",
                )
            ])

    buttons.append([InlineKeyboardButton(text="+ Создать", callback_data="invite_create")])
    buttons.append(back_menu_btn())

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>Инвайт-коды</b>\n{HEADER_LINE}\n\n"
        + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("invite_del:"))
async def cb_invite_del(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    code = cb.data.split(":", 1)[1]
    db.delete_invite(code)
    await cb.answer(f"Инвайт {code} удалён", show_alert=True)

    # Refresh list by re-triggering
    cb.data = "invite_list"
    await cb_invite_list(cb, user_role=user_role)


# ════════════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT (admin only)
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "user_list")
async def cb_user_list(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    users = db.list_users()
    counts = db.count_users()

    buttons = []
    for u in users:
        role_tag = "A" if u["role"] == "admin" else "U"
        name = u["username"] or str(u["telegram_id"])
        key_count = db.count_user_keys(u["telegram_id"])
        buttons.append([
            InlineKeyboardButton(
                text=f"[{role_tag}] {name}  |  {key_count} ключей",
                callback_data=f"user_view:{u['telegram_id']}",
            )
        ])
    buttons.append(back_menu_btn())

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>Пользователи</b>\n{HEADER_LINE}\n\n"
        f"  Админов: {counts['admins']}  |  Юзеров: {counts['users']}  |  Всего: {counts['total']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("user_view:"))
async def cb_user_view(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    tg_id = int(cb.data.split(":")[1])
    u = db.get_user(tg_id)
    if not u:
        await cb.answer("Пользователь не найден", show_alert=True)
        return

    key_count = db.count_user_keys(tg_id)
    role_tag = "ADMIN" if u["role"] == "admin" else "USER"
    name = u["username"] or str(u["telegram_id"])

    toggle_role = "user" if u["role"] == "admin" else "admin"
    toggle_text = "Понизить до User" if u["role"] == "admin" else "Повысить до Admin"

    rows = [
        [InlineKeyboardButton(text=f"Ключи ({key_count})", callback_data=f"user_keys:{tg_id}")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"user_role:{tg_id}:{toggle_role}")],
        [InlineKeyboardButton(text="Удалить пользователя", callback_data=f"user_del:{tg_id}")],
        [InlineKeyboardButton(text="<< Назад", callback_data="user_list")],
    ]

    await cb.message.edit_text(
        f"{HEADER_LINE}\n"
        f"  <b>Пользователь</b>  [{role_tag}]\n"
        f"{HEADER_LINE}\n\n"
        f"  Имя:       {html.escape(name)}\n"
        f"  TG ID:     <code>{tg_id}</code>\n"
        f"  Роль:      {u['role']}\n"
        f"  Ключей:    {key_count} / {u['max_keys']}\n"
        f"  Дата рег:  {ts_fmt(u['created_at'])}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("user_keys:"))
async def cb_user_keys(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    tg_id = int(cb.data.split(":")[1])
    keys = db.list_keys_by_user(tg_id)

    if not keys:
        await cb.message.edit_text(
            f"{LINE}\nУ пользователя нет ключей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="<< Назад", callback_data=f"user_view:{tg_id}")],
            ]),
        )
        await cb.answer()
        return

    buttons = []
    for k in keys:
        st = "ON " if k["active"] else "OFF"
        buttons.append([
            InlineKeyboardButton(
                text=f"[{st}] #{k['id']} -- {k['key'][:10]}...",
                callback_data=f"key_view:{k['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="<< Назад", callback_data=f"user_view:{tg_id}")])

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>Ключи пользователя</b>\n{HEADER_LINE}\n\n"
        f"  Всего: {len(keys)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("user_role:"))
async def cb_user_role(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    parts = cb.data.split(":")
    tg_id = int(parts[1])
    new_role = parts[2]

    if tg_id == cb.from_user.id:
        await cb.answer("Нельзя изменить свою роль", show_alert=True)
        return

    db.update_user_role(tg_id, new_role)
    await cb.answer(f"Роль изменена на {new_role}", show_alert=True)

    # Refresh
    cb.data = f"user_view:{tg_id}"
    await cb_user_view(cb, user_role=user_role)


@router.callback_query(F.data.startswith("user_del:"))
async def cb_user_del(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    tg_id = int(cb.data.split(":")[1])

    if tg_id == cb.from_user.id:
        await cb.answer("Нельзя удалить себя", show_alert=True)
        return

    u = db.get_user(tg_id)
    name = u["username"] if u else str(tg_id)

    await cb.message.edit_text(
        f"{LINE}\n"
        f"<b>Удалить пользователя {html.escape(name)}?</b>\n\n"
        f"Все его ключи тоже будут удалены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"user_del_yes:{tg_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"user_view:{tg_id}"),
            ],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("user_del_yes:"))
async def cb_user_del_yes(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    tg_id = int(cb.data.split(":")[1])
    if tg_id == cb.from_user.id:
        await cb.answer("Нельзя удалить себя", show_alert=True)
        return

    db.delete_user(tg_id)
    _do_tt_sync()
    await cb.answer("Пользователь удалён", show_alert=True)

    cb.data = "user_list"
    await cb_user_list(cb, user_role=user_role)


# ════════════════════════════════════════════════════════════════════════════
#  SERVER MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "srv_status")
async def cb_srv_status(cb: CallbackQuery, user_role: str = "user", **kwargs):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", cfg["hysteria_service"]],
            capture_output=True, text=True, timeout=5,
        )
        hy_status = result.stdout.strip() or "unknown"
    except Exception as e:
        hy_status = f"error: {e}"

    try:
        result = subprocess.run(
            ["systemctl", "is-active", cfg.get("tt_service", "trusttunnel")],
            capture_output=True, text=True, timeout=5,
        )
        tt_status = result.stdout.strip() or "unknown"
    except Exception as e:
        tt_status = f"error: {e}"

    counts = db.count_keys()
    user_counts = db.count_users()

    hy_icon = "🟢" if hy_status == "active" else "🔴"
    tt_icon = "🟢" if tt_status == "active" else "🔴"

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>Статус серверов</b>\n{HEADER_LINE}\n\n"
        f"  {hy_icon} Hysteria 2:    {hy_status}\n"
        f"  {tt_icon} TrustTunnel:   {tt_status}\n\n"
        f"  Ключи:          {counts['active']} активных / {counts['total']} всего\n"
        f"  Пользователи:   {user_counts['total']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="srv_status")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_stats")
async def cb_srv_stats(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

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
            text = f"{HEADER_LINE}\n  <b>Трафик</b>\n{HEADER_LINE}\n\nНет данных."
        else:
            lines = []
            for user_key, traffic in data.items():
                short_key = user_key[:12] + "..."
                tx = human_bytes(traffic.get("tx", 0))
                rx = human_bytes(traffic.get("rx", 0))
                lines.append(f"  <code>{short_key}</code>  UP {tx}  DN {rx}")
            text = (
                f"{HEADER_LINE}\n  <b>Трафик</b>  ({len(data)} сессий)\n{HEADER_LINE}\n\n"
                + "\n".join(lines)
            )
    except Exception as e:
        text = f"{HEADER_LINE}\n  <b>Трафик</b>\n{HEADER_LINE}\n\nОшибка: {e}"

    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="srv_stats")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_online")
async def cb_srv_online(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

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
            text = f"{HEADER_LINE}\n  <b>Онлайн</b>\n{HEADER_LINE}\n\nНикто не подключён."
        else:
            total = sum(data.values()) if isinstance(data, dict) else 0
            lines = []
            for user_key, count in data.items():
                short_key = user_key[:12] + "..."
                lines.append(f"  <code>{short_key}</code>  -- {count} устр.")
            text = (
                f"{HEADER_LINE}\n  <b>Онлайн: {total}</b>\n{HEADER_LINE}\n\n"
                + "\n".join(lines)
            )
    except Exception as e:
        text = f"{HEADER_LINE}\n  <b>Онлайн</b>\n{HEADER_LINE}\n\nОшибка: {e}"

    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="srv_online")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_logs")
async def cb_srv_logs(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    try:
        result = subprocess.run(
            ["journalctl", "-u", cfg["hysteria_service"], "--no-pager",
             "-n", str(cfg.get("log_lines", 50))],
            capture_output=True, text=True, timeout=10,
        )
        logs = result.stdout or "Нет логов."
        if len(logs) > 3500:
            logs = logs[-3500:]
    except Exception as e:
        logs = f"Ошибка: {e}"

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>Логи</b>\n{HEADER_LINE}\n\n"
        f"<pre>{html.escape(logs)}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="srv_logs")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_restart")
async def cb_srv_restart(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await cb.message.edit_text(
        f"{LINE}\n<b>Перезапустить Hysteria + TrustTunnel?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data="srv_restart_yes"),
                InlineKeyboardButton(text="Отмена", callback_data="main_menu"),
            ],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "srv_restart_yes")
async def cb_srv_restart_yes(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    errors = []
    for svc in [cfg["hysteria_service"], cfg.get("tt_service", "trusttunnel")]:
        try:
            subprocess.run(
                ["systemctl", "restart", svc],
                timeout=15, check=True,
            )
        except Exception as e:
            errors.append(f"{svc}: {e}")

    if errors:
        await cb.message.edit_text(
            f"{LINE}\n<b>Ошибки перезапуска:</b>\n" + "\n".join(errors),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_menu_btn()]),
        )
    else:
        await cb.message.edit_text(
            f"{LINE}\n<b>Оба сервиса перезапущены.</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_menu_btn()]),
        )
    await cb.answer()


# ════════════════════════════════════════════════════════════════════════════
#  TRUSTTUNNEL MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "tt_status")
async def cb_tt_status(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    tt_svc = cfg.get("tt_service", "trusttunnel")
    try:
        result = subprocess.run(
            ["systemctl", "status", tt_svc],
            capture_output=True, text=True, timeout=10,
        )
        status_text = result.stdout or result.stderr or "Нет данных"
        if len(status_text) > 3000:
            status_text = status_text[:3000] + "\n..."
    except Exception as e:
        status_text = f"Ошибка: {e}"

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>TrustTunnel — Статус</b>\n{HEADER_LINE}\n\n"
        f"<pre>{html.escape(status_text)}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="tt_status")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "tt_logs")
async def cb_tt_logs(cb: CallbackQuery, user_role: str = "user", **kwargs):
    if not is_admin(user_role):
        await cb.answer("Нет доступа", show_alert=True)
        return

    tt_svc = cfg.get("tt_service", "trusttunnel")
    try:
        result = subprocess.run(
            ["journalctl", "-u", tt_svc, "--no-pager",
             "-n", str(cfg.get("log_lines", 50))],
            capture_output=True, text=True, timeout=10,
        )
        logs = result.stdout or "Нет логов."
        if len(logs) > 3500:
            logs = logs[-3500:]
    except Exception as e:
        logs = f"Ошибка: {e}"

    await cb.message.edit_text(
        f"{HEADER_LINE}\n  <b>TrustTunnel — Логи</b>\n{HEADER_LINE}\n\n"
        f"<pre>{html.escape(logs)}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="tt_logs")],
            back_menu_btn(),
        ]),
    )
    await cb.answer()


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"Starting auth backend on 127.0.0.1:{cfg.get('auth_backend_port', 8787)}")
    await auth_backend.start()

    # Sync TrustTunnel credentials on startup
    try:
        cred_path = cfg.get("tt_credentials_path", "/etc/hysteria/bot/tt_credentials.txt")
        sync_tt_credentials(db, cred_path)
        log.info("TrustTunnel credentials synced on startup")
    except Exception as e:
        log.warning("TrustTunnel sync failed on startup: %s", e)

    log.info("Starting Telegram bot...")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await auth_backend.stop()


if __name__ == "__main__":
    asyncio.run(main())
