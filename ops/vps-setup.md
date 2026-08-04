# Подъём Course Dashboard на сервере — шпаргалка эфира

Порядок шагов совпадает с конвейером со слайда 5: подготовка → доставка → конфигурация → миграция → запуск → проверка → расписание.
Домен в примерах — `cd.edunit.org`, каталог — `/srv/course-dashboard`, пользователь службы — `cd`. Заменить на свои, если отличаются.

## Шаг 0. Нулевой слой (делается ДО эфира)

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.12 python3.12-venv git curl
# Caddy из официального репозитория
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt -y install caddy

# Пользователь службы, без права входа в систему
sudo useradd --system --create-home --home-dir /srv/course-dashboard --shell /usr/sbin/nologin cd

# Наружу — только доступ по SSH и веб
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw --force enable
sudo ufw status
```

Проверка шага: `python3.12 --version`, `caddy version`, `ufw status` показывает три правила.

## Шаг 1. Код на сервере

```bash
sudo -u cd git clone <адрес репозитория> /srv/course-dashboard/app-src
cd /srv/course-dashboard/app-src
sudo -u cd python3.12 -m venv /srv/course-dashboard/.venv
sudo -u cd /srv/course-dashboard/.venv/bin/pip install -r requirements.txt   # или pip install -e .
sudo -u cd mkdir -p /srv/course-dashboard/data
```

## Шаг 2. Секреты и настройки

```bash
sudo install -m 600 -o cd -g cd /dev/null /etc/course-dashboard.env
sudo nano /etc/course-dashboard.env      # содержимое — по ops/env.example
sudo ls -l /etc/course-dashboard.env     # ЭТО показываем на экране, не содержимое
```

Сгенерировать значения: `openssl rand -hex 32` — отдельно для ключа подписи и для токена обхода.
Пароль администратора задать ДО первой миграции: его читает сид.

## Шаг 3. Миграция базы (отдельным шагом, до запуска)

```bash
# Если переносим копию рабочей базы — сначала бэкап рядом с датой
sudo -u cd cp /srv/course-dashboard/data/course_dashboard.db \
              /srv/course-dashboard/data/course_dashboard.db.bak-$(date +%F)

cd /srv/course-dashboard/app-src
sudo -u cd env $(grep -v '^#' /etc/course-dashboard.env | xargs) \
     /srv/course-dashboard/.venv/bin/alembic upgrade head
```

Проверка шага: команда закончилась без ошибок и последняя ревизия совпадает с ожидаемой.
Ловушка: адрес базы для `alembic` берётся из `alembic.ini`, для приложения — из переменной окружения. Убедиться, что это один и тот же файл (инцидент 31 июля: мигрировали пустой файл).

## Шаг 4. Служба

```bash
sudo cp ops/course-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now course-dashboard
systemctl status course-dashboard --no-pager
curl -s http://127.0.0.1:8000/health
```

Проверка шага: `active (running)` и ответ от адреса состояния. Служба поднимется сама после перезагрузки сервера — в отличие от процесса, запущенного в терминале.

## Шаг 5. Прокси и HTTPS

```bash
sudo cp ops/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo journalctl -u caddy -n 30 --no-pager     # видно выпуск сертификата
```

Предусловие: запись A домена уже указывает на этот сервер (проверить `nslookup cd.edunit.org 8.8.8.8`), порты 80 и 443 открыты.
Проверка шага — из браузера, не из терминала: `https://cd.edunit.org` открывается, замок в адресной строке есть, страница входа отдаётся.

## Шаг 6. Расписание обхода

```bash
sudo crontab -u cd -e
# 0 7,19 * * * curl -s -X POST http://127.0.0.1:8000/sync -H "X-Sync-Token: <токен>"
sudo crontab -u cd -l
```

Проверка шага: `crontab -l` показывает строку; на матрице отметка «актуально на» обновляется после первого запуска.

## Откат

```bash
cd /srv/course-dashboard/app-src
sudo -u cd git log --oneline -5
sudo -u cd git checkout <предыдущий коммит>
sudo systemctl restart course-dashboard
curl -s http://127.0.0.1:8000/health
```

База: восстановление из копии, снятой на шаге 3, — крайняя мера. Обратная миграция структуру вернёт, а удалённые ею данные — нет.

## Пять проверок глазами (слайд 8)

1. Секретов нет ни в репозитории, ни в юните службы, ни на экране.
2. Пользователь и пути в юните существуют на самом деле.
3. Приложение слушает `127.0.0.1`; наружу открыт только прокси.
4. Миграция применена к тому же файлу базы, который читает приложение.
5. Служба включена в автозапуск — переживёт перезагрузку.
