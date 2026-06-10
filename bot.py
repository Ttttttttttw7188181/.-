#!/usr/bin/env python3
"""
WDTT + Hysteria2 deployment bot
Схема: Телефон → hy2 → VPS (wdtt-клиент + wdtt-сервер) → интернет
"""

import asyncio
import logging
import os
import re
import secrets
import string
import subprocess
import threading
import time

import paramiko
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# ─── Конфиг ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []

HY2_PORT = 443
WDTT_SERVER_PORT = 51820  # WireGuard/wdtt внутренний порт
MONITOR_INTERVAL = 60  # секунд между проверками хеша

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Хранилище сессий (в памяти) ──────────────────────────────────────────────
# sessions[user_id] = {ip, password, hash, hy2_password, status, ssh_client}
sessions: dict[int, dict] = {}
monitors: dict[int, threading.Thread] = {}

# ─── FSM States ───────────────────────────────────────────────────────────────
class Deploy(StatesGroup):
    wait_ip = State()
    wait_password = State()
    wait_hash = State()
    confirm = State()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def gen_password(length=24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ssh_connect(ip: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, username="root", password=password, timeout=15)
    return client


def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout=60) -> tuple[str, str]:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(), stderr.read().decode()


def generate_hy2_config(ip: str, hy2_password: str) -> str:
    return f"""server: {ip}:{HY2_PORT}
auth: {hy2_password}
tls:
  insecure: true
socks5:
  listen: 127.0.0.1:1080
http:
  listen: 127.0.0.1:8080
"""


DEPLOY_SCRIPT = """#!/bin/bash
set -e

HY2_PASS="{hy2_pass}"
VK_HASH="{vk_hash}"
HY2_PORT={hy2_port}

echo "[1/5] Обновление системы..."
apt-get update -qq
apt-get install -y -qq curl wget openssl wireguard-tools iproute2 iptables

echo "[2/5] Установка Hysteria2..."
bash <(curl -fsSL https://get.hy2.sh/) || true

echo "[3/5] Генерация self-signed TLS сертификата..."
mkdir -p /etc/hysteria
openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \\
  -keyout /etc/hysteria/server.key \\
  -out /etc/hysteria/server.crt \\
  -days 3650 -subj '/CN=hysteria2'

echo "[4/5] Конфиг Hysteria2..."
cat > /etc/hysteria/config.yaml <<EOF
listen: :{hy2_port}

tls:
  cert: /etc/hysteria/server.crt
  key: /etc/hysteria/server.key

auth:
  type: password
  password: $HY2_PASS

masquerade:
  type: proxy
  proxy:
    url: https://news.ycombinator.com/
    rewriteHost: true

bandwidth:
  up: 1 gbps
  down: 1 gbps
EOF

echo "[5/5] Установка wdtt-сервера..."
# Скачиваем бинарник wdtt-server если есть, иначе собираем из исходников
if ! command -v wdtt-server &>/dev/null; then
  # Попытка скачать готовый бинарник
  WDTT_URL="https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.0.0/wdtt-server-linux-amd64"
  if curl -fsSL --max-time 10 "$WDTT_URL" -o /usr/local/bin/wdtt-server 2>/dev/null; then
    chmod +x /usr/local/bin/wdtt-server
    echo "wdtt-server скачан"
  else
    echo "Бинарник wdtt-server не найден, используем прямой маршрут"
    # Fallback: NAT через iptables (трафик идёт напрямую через VPS)
    echo "FALLBACK=true" > /etc/wdtt.env
  fi
fi

# Настройка wdtt-сервера как systemd сервиса
if command -v wdtt-server &>/dev/null; then
  cat > /etc/systemd/system/wdtt-server.service <<EOF2
[Unit]
Description=WDTT Server
After=network.target

[Service]
ExecStart=/usr/local/bin/wdtt-server --port {wdtt_port} --hash $VK_HASH
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF2
  systemctl daemon-reload
  systemctl enable wdtt-server
  systemctl start wdtt-server || true
fi

# Включаем IP forwarding
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-wdtt.conf
sysctl -p /etc/sysctl.d/99-wdtt.conf

# NAT для выхода в интернет
IFACE=$(ip route | grep default | awk '{{print $5}}' | head -1)
iptables -t nat -C POSTROUTING -o "$IFACE" -j MASQUERADE 2>/dev/null || \\
  iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE

# Запуск Hysteria2
systemctl enable hysteria-server
systemctl restart hysteria-server

echo "DONE"
"""

HASH_CHECK_SCRIPT = """
#!/bin/bash
# Проверяет жив ли wdtt-сервер и активен ли хеш
if systemctl is-active --quiet wdtt-server 2>/dev/null; then
  echo "OK"
else
  # Проверяем hysteria2 как минимум
  if systemctl is-active --quiet hysteria-server 2>/dev/null; then
    echo "HY2_ONLY"
  else
    echo "DOWN"
  fi
fi
"""

# ─── Деплой на VPS ────────────────────────────────────────────────────────────

async def deploy_vps(user_id: int, ip: str, ssh_pass: str, vk_hash: str, bot: Bot) -> str:
    hy2_pass = gen_password()
    sessions[user_id] = {
        "ip": ip,
        "ssh_password": ssh_pass,
        "vk_hash": vk_hash,
        "hy2_password": hy2_pass,
        "status": "deploying",
    }

    await bot.send_message(user_id, "🔌 Подключаюсь к VPS...")

    try:
        client = ssh_connect(ip, ssh_pass)
    except Exception as e:
        sessions[user_id]["status"] = "error"
        return f"❌ Не удалось подключиться к VPS: {e}"

    sessions[user_id]["ssh_client"] = client
    await bot.send_message(user_id, "⚙️ Деплою wdtt + Hysteria2 (~2-3 мин)...")

    script = DEPLOY_SCRIPT.format(
        hy2_pass=hy2_pass,
        vk_hash=vk_hash,
        hy2_port=HY2_PORT,
        wdtt_port=WDTT_SERVER_PORT,
    )

    try:
        out, err = ssh_exec(client, script, timeout=300)
    except Exception as e:
        sessions[user_id]["status"] = "error"
        return f"❌ Ошибка при деплое: {e}"

    if "DONE" not in out:
        sessions[user_id]["status"] = "error"
        log.error("Deploy stderr: %s", err)
        return f"❌ Деплой завершился с ошибкой:\n<code>{err[-500:]}</code>"

    sessions[user_id]["status"] = "running"

    # Запускаем мониторинг хеша
    start_monitor(user_id, client, bot)

    hy2_config = generate_hy2_config(ip, hy2_pass)
    return (
        f"✅ <b>Готово!</b>\n\n"
        f"📡 Сервер: <code>{ip}:{HY2_PORT}</code>\n"
        f"🔑 Пароль hy2: <code>{hy2_pass}</code>\n"
        f"🔗 VK хеш: <code>{vk_hash}</code>\n\n"
        f"📋 <b>Конфиг Hysteria2:</b>\n<pre>{hy2_config}</pre>\n\n"
        f"📱 Используй в клиенте: <a href='https://v2.hysteria.network/'>Hysteria2 App</a>\n"
        f"⚠️ TLS insecure=true (self-signed сертификат)"
    )


# ─── Мониторинг хеша ──────────────────────────────────────────────────────────

def start_monitor(user_id: int, ssh_client: paramiko.SSHClient, bot: Bot):
    def monitor():
        consecutive_fails = 0
        while True:
            time.sleep(MONITOR_INTERVAL)
            sess = sessions.get(user_id)
            if not sess or sess.get("status") != "running":
                break
            try:
                out, _ = ssh_exec(ssh_client, HASH_CHECK_SCRIPT, timeout=15)
                status = out.strip()
                if status == "OK":
                    consecutive_fails = 0
                elif status == "HY2_ONLY":
                    consecutive_fails += 1
                    if consecutive_fails >= 2:
                        asyncio.run(bot.send_message(
                            user_id,
                            f"⚠️ <b>wdtt-сервер упал!</b>\n"
                            f"Hysteria2 работает, но wdtt недоступен.\n"
                            f"VK хеш <code>{sess['vk_hash']}</code> возможно истёк.\n"
                            f"Используй /redeploy для обновления хеша."
                        ))
                        consecutive_fails = 0
                else:
                    consecutive_fails += 1
                    if consecutive_fails >= 2:
                        sessions[user_id]["status"] = "down"
                        asyncio.run(bot.send_message(
                            user_id,
                            f"🔴 <b>Сервер упал!</b>\n"
                            f"IP: <code>{sess['ip']}</code>\n"
                            f"Hysteria2 и wdtt недоступны.\n"
                            f"Используй /status для диагностики."
                        ))
                        break
            except Exception as e:
                log.warning("Monitor error for user %d: %s", user_id, e)
                consecutive_fails += 1

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    monitors[user_id] = t


# ─── Bot handlers ─────────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 <b>WDTT + Hysteria2 Bot</b>\n\n"
        "Деплою связку wdtt + hy2 на твой VPS.\n\n"
        "Команды:\n"
        "/deploy — развернуть на новом VPS\n"
        "/status — статус текущего сервера\n"
        "/config — получить конфиг hy2\n"
        "/redeploy — обновить хеш VK\n"
        "/stop — остановить мониторинг"
    )


@dp.message(Command("deploy"))
async def cmd_deploy(msg: Message, state: FSMContext):
    await state.set_state(Deploy.wait_ip)
    await msg.answer(
        "🖥 <b>Шаг 1/3</b>\n\nВведи IP-адрес VPS:",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Deploy.wait_ip)
async def step_ip(msg: Message, state: FSMContext):
    ip = msg.text.strip()
    # Простая валидация IP
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
        await msg.answer("❌ Неверный формат IP. Попробуй ещё раз:")
        return
    await state.update_data(ip=ip)
    await state.set_state(Deploy.wait_password)
    await msg.answer("🔑 <b>Шаг 2/3</b>\n\nВведи root-пароль VPS:")


@dp.message(Deploy.wait_password)
async def step_password(msg: Message, state: FSMContext):
    await state.update_data(ssh_password=msg.text.strip())
    # Удаляем сообщение с паролем для безопасности
    try:
        await msg.delete()
    except Exception:
        pass
    await state.set_state(Deploy.wait_hash)
    await msg.answer(
        "🔗 <b>Шаг 3/3</b>\n\n"
        "Введи хеш VK-звонка.\n\n"
        "Как получить:\n"
        "1. Открой VK → создай группу\n"
        "2. Начни звонок в группе\n"
        "3. Скопируй ссылку <code>vk.com/call/join/ХЕSH</code>\n"
        "4. Отправь только <code>ХЕSH</code> (часть после последнего /)"
    )


@dp.message(Deploy.wait_hash)
async def step_hash(msg: Message, state: FSMContext):
    vk_hash = msg.text.strip()
    data = await state.get_data()
    await state.update_data(vk_hash=vk_hash)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Да, деплоить"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await state.set_state(Deploy.confirm)
    await msg.answer(
        f"📋 <b>Подтверди данные:</b>\n\n"
        f"🖥 IP: <code>{data['ip']}</code>\n"
        f"🔗 Хеш: <code>{vk_hash}</code>\n\n"
        f"Всё верно?",
        reply_markup=kb
    )


@dp.message(Deploy.confirm, F.text == "✅ Да, деплоить")
async def step_confirm(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await msg.answer("🚀 Начинаю деплой...", reply_markup=ReplyKeyboardRemove())

    result = await deploy_vps(
        user_id=msg.from_user.id,
        ip=data["ip"],
        ssh_pass=data["ssh_password"],
        vk_hash=data["vk_hash"],
        bot=bot,
    )
    await msg.answer(result, disable_web_page_preview=True)


@dp.message(Deploy.confirm, F.text == "❌ Отмена")
async def step_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Отменено.", reply_markup=ReplyKeyboardRemove())


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    sess = sessions.get(msg.from_user.id)
    if not sess:
        await msg.answer("ℹ️ Нет активных деплоев. Используй /deploy")
        return

    status_emoji = {"running": "🟢", "deploying": "🟡", "down": "🔴", "error": "❌"}.get(sess["status"], "⚪")
    await msg.answer(
        f"{status_emoji} <b>Статус сервера:</b>\n\n"
        f"🖥 IP: <code>{sess['ip']}</code>\n"
        f"🔗 Хеш: <code>{sess['vk_hash']}</code>\n"
        f"📊 Состояние: {sess['status']}"
    )


@dp.message(Command("config"))
async def cmd_config(msg: Message):
    sess = sessions.get(msg.from_user.id)
    if not sess or sess["status"] not in ("running", "deploying"):
        await msg.answer("ℹ️ Нет активного сервера. Используй /deploy")
        return

    config = generate_hy2_config(sess["ip"], sess["hy2_password"])
    await msg.answer(
        f"📋 <b>Конфиг Hysteria2:</b>\n\n<pre>{config}</pre>\n\n"
        f"⚠️ TLS insecure=true (self-signed)"
    )


@dp.message(Command("redeploy"))
async def cmd_redeploy(msg: Message, state: FSMContext):
    sess = sessions.get(msg.from_user.id)
    if not sess:
        await msg.answer("ℹ️ Нет активных деплоев. Используй /deploy")
        return
    await state.set_state(Deploy.wait_hash)
    await state.update_data(ip=sess["ip"], ssh_password=sess["ssh_password"])
    await msg.answer(
        "🔗 Введи новый хеш VK-звонка:\n\n"
        "<i>Создай новый звонок и скопируй хеш из ссылки</i>"
    )


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    sess = sessions.get(msg.from_user.id)
    if sess:
        sess["status"] = "stopped"
        client = sess.get("ssh_client")
        if client:
            try:
                client.close()
            except Exception:
                pass
        sessions.pop(msg.from_user.id, None)
    await msg.answer("⛔ Мониторинг остановлен.")


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
