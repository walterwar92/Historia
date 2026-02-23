# Hysteria 2 VPN + Telegram Bot — Руководство

## Быстрая установка

```bash
# Загрузите папку проекта на сервер Ubuntu 24.04
# Структура: deploy.sh + bot/ (рядом)
chmod +x deploy.sh
sudo ./deploy.sh
```

Скрипт запросит:
1. **Telegram Bot Token** — получите у [@BotFather](https://t.me/BotFather)
2. **Пароль администратора** — для доступа к боту

Далее автоматически:
1. Проверит Ubuntu 24.04
2. Установит зависимости (curl, openssl, iptables, jq, cron, python3)
3. Скачает Hysteria 2 (официальный установщик)
4. Сгенерирует самоподписанный TLS-сертификат (10 лет)
5. Сгенерирует пароль обфускации и секрет API
6. Настроит Hysteria с HTTP auth backend (мульти-ключи)
7. Установит Python-бота в виртуальное окружение
8. Настроит iptables (UDP 443) и сетевые буферы
9. Создаст 2 systemd-сервиса (Hysteria + Bot)
10. Установит cron автообновление + утилиту `hysteria-manage`

---

## Telegram Bot

### Первый запуск

1. Откройте вашего бота в Telegram
2. Отправьте `/start`
3. Введите **пароль администратора** (заданный при установке)
4. Появится главное меню с inline-кнопками

### Функции бота

| Кнопка | Описание |
|--------|----------|
| **Создать ключ** | Генерирует уникальный VPN-ключ, показывает URI |
| **Список ключей** | Все ключи со статусами |
| **Конфиг клиента** | config.yaml + URI для конкретного ключа |
| **Заблокировать** | Мгновенно отключает ключ |
| **Разблокировать** | Возвращает ключ в работу |
| **Удалить** | Удаляет ключ (с подтверждением) |
| **Статус сервера** | systemd статус + количество ключей |
| **Трафик** | Статистика трафика по сессиям |
| **Онлайн** | Текущие подключения |
| **Логи** | Последние строки журнала Hysteria |
| **Перезапуск** | Перезапуск Hysteria (с подтверждением) |

### Команды бота

- `/start` — главное меню
- `/menu` — повторно показать меню
- `/logout` — выйти из сессии

### Как это работает

```
Клиент ──► Hysteria (UDP 443) ──► Auth Backend (HTTP :8787) ──► SQLite БД
                                        ▲
                                        │
                              Telegram Bot (управляет ключами)
```

1. Hysteria получает подключение с паролем (ключом)
2. Hysteria отправляет POST на `http://127.0.0.1:8787/auth`
3. Auth backend проверяет ключ в SQLite (активен? не истёк?)
4. Если валиден — 200 OK, клиент подключен
5. Если нет — 403, клиент отклонён

---

## Подключение клиентов

### Каждый ключ = безлимит устройств

Создайте ключ через бота, получите URI и конфиг. Один ключ можно использовать на неограниченном количестве устройств.

### Android / iOS (Hysteria 2 App)

1. Установите Hysteria 2 из магазина приложений
2. Скопируйте **URI** из бота (кнопка «Конфиг клиента»):
   ```
   hy2://KEY@SERVER_IP:443?insecure=1#Hysteria2-VPN
   ```
3. Импортируйте URI в приложение

### Десктоп (CLI)

1. Скачайте клиент: https://hysteria.network/docs/getting-started/Installation/
2. Скопируйте `config.yaml` из бота
3. Запустите:
```bash
./hysteria client --config config.yaml
```
4. Прокси:
   - SOCKS5: `127.0.0.1:1080`
   - HTTP: `127.0.0.1:8080`

---

## Управление сервером (CLI)

| Команда | Описание |
|---------|----------|
| `hysteria-manage status` | Статус обоих сервисов |
| `hysteria-manage start` | Запустить всё |
| `hysteria-manage stop` | Остановить всё |
| `hysteria-manage restart` | Перезапустить всё |
| `hysteria-manage logs` | Логи Hysteria (50 строк) |
| `hysteria-manage logs 200` | Логи Hysteria (200 строк) |
| `hysteria-manage bot-logs` | Логи бота |
| `hysteria-manage show-config` | Конфигурация сервера |
| `hysteria-manage stats` | Трафик и подключения |
| `hysteria-manage update` | Обновить Hysteria |
| `hysteria-manage uninstall` | Удалить всё |

---

## Отказоустойчивость

- **2 systemd-сервиса** с автоперезапуском
- Hysteria: restart через 5 сек, до 10 раз/мин, watchdog 30s
- Bot: restart через 3 сек, до 15 раз/мин
- Hysteria зависит от бота (`Requires=hysteria-bot.service`)
- Автозапуск при загрузке ОС
- Ежедневное автообновление Hysteria через cron

---

## Безопасность

- Конфигурации: chmod 600 (только root)
- Auth backend и Stats API слушают только на 127.0.0.1
- Systemd hardening: NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp
- Пароль бота удаляется из чата после авторизации
- SQLite БД ключей хранится в `/etc/hysteria/bot/keys.db`

---

## Файлы и пути

| Путь | Описание |
|------|----------|
| `/usr/local/bin/hysteria` | Бинарник Hysteria 2 |
| `/usr/local/bin/hysteria-manage` | Скрипт управления |
| `/etc/hysteria/config.yaml` | Конфигурация сервера |
| `/etc/hysteria/certs/` | TLS-сертификаты |
| `/etc/hysteria/credentials.txt` | Все credentials |
| `/etc/hysteria/bot/bot.py` | Telegram бот |
| `/etc/hysteria/bot/bot.json` | Конфигурация бота |
| `/etc/hysteria/bot/keys.db` | SQLite база ключей |
| `/etc/hysteria/bot/venv/` | Python virtual environment |
| `/etc/systemd/system/hysteria-server.service` | Сервис Hysteria |
| `/etc/systemd/system/hysteria-bot.service` | Сервис бота |
| `/etc/cron.daily/hysteria-update` | Автообновление |

---

## Устранение проблем

```bash
# Статус всех сервисов
hysteria-manage status

# Логи Hysteria
hysteria-manage logs 100

# Логи бота
hysteria-manage bot-logs 100

# Порты
ss -ulnp | grep 443      # Hysteria (UDP)
ss -tlnp | grep 8787     # Auth backend (TCP)

# Firewall
iptables -L -n | grep 443

# Перезапуск всего
hysteria-manage restart

# Ручной тест auth backend
curl -X POST http://127.0.0.1:8787/auth \
  -H 'Content-Type: application/json' \
  -d '{"addr":"1.2.3.4:1234","auth":"YOUR_KEY","tx":0,"rx":0}'
# 200 = ключ валиден, 403 = отклонён
```
