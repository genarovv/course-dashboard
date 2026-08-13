# Подъём Course Dashboard на сервере — шпаргалка эфира

Порядок шагов совпадает с конвейером со слайда 5: подготовка → доставка → конфигурация → миграция → запуск → проверка → расписание.
Домен в примерах — `coursedashboard.edunit.org`, каталог — `/srv/course-dashboard`, пользователь службы — `cd`. Заменить на свои, если отличаются.

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
С S76 (#76) `alembic` уважает `CD_DATABASE_URL` — при запуске с загруженным окружением (как в команде выше) миграция и приложение гарантированно смотрят в один файл. Прежняя ловушка (адрес только из `alembic.ini`; инцидент 31 июля: мигрировали пустой файл) закрыта, но окружение при вызове `alembic` загружать по-прежнему обязательно.
База живёт в `/srv/course-dashboard/data/` — вне git-дерева `app-src`, рядом с бэкапами (перенос выполнен на боевом 2026-08-14).

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

Предусловие: запись A домена уже указывает на этот сервер (проверить `nslookup coursedashboard.edunit.org 8.8.8.8`), порты 80 и 443 открыты.
Проверка шага — из браузера, не из терминала: `https://coursedashboard.edunit.org` открывается, замок в адресной строке есть, страница входа отдаётся.

### Обновление Caddyfile (в том числе фильтр сканерного мусора, D46)

`ops/Caddyfile` в репозитории — источник правды; на сервере лежит его копия. Любое изменение доставляется так: скопировать, проверить синтаксис, перечитать без простоя.

```bash
cd /srv/course-dashboard/app-src
sudo cp ops/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Контрольная проверка фильтра: мусорные пути получают 404 от прокси, живые работают как раньше.

```bash
# Мусор — 404 от Caddy, в журнале приложения этих запросов нет
curl -s -o /dev/null -w "%{http_code}\n" https://coursedashboard.edunit.org/.env
curl -s -o /dev/null -w "%{http_code}\n" https://coursedashboard.edunit.org/wp-login.php
# Живые пути — как раньше: /health отвечает 200, /login отдаёт страницу входа
curl -s -o /dev/null -w "%{http_code}\n" https://coursedashboard.edunit.org/health
curl -s -o /dev/null -w "%{http_code}\n" https://coursedashboard.edunit.org/login
```

Как убедиться, что фильтр работает по назначению: запрос `/.env` виден в `/var/log/caddy/course-dashboard.log`, но НЕ появляется в `journalctl -u course-dashboard` — значит, до uvicorn он не дошёл.

## Шаг 6. Расписание обхода

```bash
sudo crontab -u cd -e
# 0 7,19 * * * (curl -sS -m 900 -X POST http://127.0.0.1:8000/sync -H "X-Sync-Token: <токен>" || echo "sync request FAILED rc=$?") 2>&1 | logger -t cd-sync-cron
sudo crontab -u cd -l
```

Вывод curl уходит в journald тегом `cd-sync-cron` (`journalctl -t cd-sync-cron`): виден и ответ обхода, и отказ, когда служба лежит в момент запуска. Строка с `curl -s` без logger — прежняя редакция: она глотала вывод, и упавший по расписанию обход не оставлял следа нигде (аудит журнала 2026-08-14).

Проверка шага: `crontab -l` показывает строку; на матрице отметка «актуально на» обновляется после первого запуска; `journalctl -t cd-sync-cron` после первого запуска содержит JSON-ответ обхода.

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
