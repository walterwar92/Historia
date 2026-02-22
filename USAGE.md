# Hysteria 2 VPN — Руководство по использованию

## Быстрая установка на сервере

```bash
# Загрузите deploy.sh на сервер Ubuntu 24.04 и запустите:
chmod +x deploy.sh
sudo ./deploy.sh
```

Скрипт автоматически:
1. Проверит, что ОС — Ubuntu 24.04
2. Установит зависимости (curl, openssl, iptables, jq, cron)
3. Скачает Hysteria 2 (официальный установщик)
4. Сгенерирует самоподписанный TLS-сертификат (10 лет)
5. Сгенерирует PSK-ключ, пароль обфускации и секрет API
6. Создаст оптимизированную конфигурацию с Salamander obfuscation
7. Настроит iptables (UDP порт 443) и сетевые буферы
8. Создаст systemd-сервис с автозапуском и watchdog
9. Установит cron-задачу для ежедневного автообновления
10. Установит утилиту управления `hysteria-manage`

После установки на экране появятся **все данные для подключения**.

---

## Подключение клиентов

### Один ключ = безлимит устройств

Все клиенты используют один и тот же ключ. Количество одновременных подключений не ограничено.

### Android / iOS (Hysteria 2 App)

1. Установите приложение Hysteria 2 из [Google Play](https://play.google.com/store/apps/details?id=io.nekohasekai.sfa) / App Store
2. Импортируйте **Connection URI**, который показывается при установке:
   ```
   hy2://AUTH_KEY@SERVER_IP:443?obfs=salamander&obfs-password=OBFS_PASS&insecure=1#Hysteria2-VPN
   ```
3. Или скопируйте URI командой на сервере:
   ```bash
   hysteria-manage show-client
   ```

### Десктоп (CLI)

1. Скачайте клиент: https://hysteria.network/docs/getting-started/Installation/
2. Создайте файл `config.yaml`:

```yaml
server: SERVER_IP:443

auth: YOUR_AUTH_KEY

tls:
  insecure: true

obfs:
  type: salamander
  salamander:
    password: YOUR_OBFS_PASSWORD

socks5:
  listen: 127.0.0.1:1080

http:
  listen: 127.0.0.1:8080
```

3. Запустите:
```bash
./hysteria client --config config.yaml
```

4. Настройте браузер/систему на использование прокси:
   - SOCKS5: `127.0.0.1:1080`
   - HTTP:   `127.0.0.1:8080`

### Опционально: Bandwidth (для Brutal congestion control)

Если хотите использовать алгоритм Brutal вместо BBR, укажите реальную пропускную способность вашего канала в клиентском конфиге:

```yaml
bandwidth:
  up: 50 mbps
  down: 100 mbps
```

---

## Управление сервером

Утилита `hysteria-manage` устанавливается автоматически:

| Команда | Описание |
|---------|----------|
| `hysteria-manage status` | Статус сервиса |
| `hysteria-manage start` | Запустить сервис |
| `hysteria-manage stop` | Остановить сервис |
| `hysteria-manage restart` | Перезапустить |
| `hysteria-manage logs` | Последние 50 строк логов |
| `hysteria-manage logs 200` | Последние 200 строк логов |
| `hysteria-manage show-config` | Показать конфигурацию сервера |
| `hysteria-manage show-client` | Показать клиентский конфиг и URI |
| `hysteria-manage stats` | Активные подключения и трафик |
| `hysteria-manage change-key` | Сменить ключ аутентификации |
| `hysteria-manage change-obfs` | Сменить пароль обфускации |
| `hysteria-manage update` | Обновить Hysteria до последней версии |
| `hysteria-manage uninstall` | Полностью удалить Hysteria |

---

## Отказоустойчивость

- **Systemd**: автоматический перезапуск при падении (через 5 сек, до 10 раз в минуту)
- **Watchdog**: systemd отслеживает процесс каждые 30 сек
- **Автозапуск**: сервис стартует при загрузке ОС
- **Автообновление**: ежедневная проверка новых версий через cron

---

## Безопасность

- Конфигурация: `/etc/hysteria/config.yaml` (chmod 600)
- Сертификаты: `/etc/hysteria/certs/` (ключ chmod 600)
- Credentials: `/etc/hysteria/credentials.txt` (chmod 600)
- Systemd hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`
- Stats API слушает только на `127.0.0.1` (недоступен извне)

---

## Сетевые оптимизации

Скрипт автоматически применяет:
- `net.ipv4.ip_forward=1` — IP forwarding
- `net.core.rmem_max=16MB` — увеличенные буферы приёма
- `net.core.wmem_max=16MB` — увеличенные буферы отправки
- QUIC window: 16MB stream / 32MB connection

---

## Файлы и пути

| Путь | Описание |
|------|----------|
| `/usr/local/bin/hysteria` | Бинарник Hysteria 2 |
| `/usr/local/bin/hysteria-manage` | Скрипт управления |
| `/etc/hysteria/config.yaml` | Конфигурация сервера |
| `/etc/hysteria/certs/` | TLS-сертификаты |
| `/etc/hysteria/credentials.txt` | Сохранённые данные для подключения |
| `/etc/systemd/system/hysteria-server.service` | Systemd unit |
| `/etc/cron.daily/hysteria-update` | Скрипт автообновления |
| `/var/log/hysteria-update.log` | Лог обновлений |

---

## Устранение проблем

```bash
# Проверить статус сервиса
hysteria-manage status

# Посмотреть логи
hysteria-manage logs 100

# Проверить, слушает ли порт
ss -ulnp | grep 443

# Проверить firewall
iptables -L -n | grep 443

# Перезапустить
hysteria-manage restart

# Тест подключения с клиента (если hysteria-cli установлен)
hysteria ping --config config.yaml
```
