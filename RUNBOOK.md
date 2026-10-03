# RUNBOOK: запуск Stalzone Bot

Два сценария: **локальный запуск** (для проверки) и **прод на сервере** (Docker, 0.5 ГБ RAM). Фронтенд собирается из `frontend/` (`npm run build` -> `web/static/spa`); для запуска бота Node.js не нужен.

---

## Часть 1. Локально (Python 3.12)

### 1.1. Установите зависимости

```bash
cd stalzone-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 1.2. Настройте .env

```bash
cp .env.example .env
```

Обязательно заполните:

| Переменная | Что вписать |
|---|---|
| `CLIENT_ID` / `CLIENT_SECRET` | ключи приложения EXBO (https://stalcrafthub.ru/dev) |
| `DISCORD_WEBHOOK_URL` | вебхук канала Discord (можно пусто — уведомления не уходят, всё остальное работает) |

Остальное по умолчанию: SQLite (`DATABASE_URL=sqlite+aiosqlite:///./data/stalzone.db`), ничего ставить не нужно. Для тестов на демо-API: `BASE_API_URL=https://dapi.stalcraft.net`.

> Никогда не вставляйте в `.env` ключи, которые где-либо «засветились». Перевыпустите и используйте свежие.

### 1.3. Запуск

```bash
python main.py
```

При первом старте автоматически: создаётся БД (`data/stalzone.db`), применяются миграции Alembic, поднимаются воркеры и веб-сервер. Откройте **http://localhost:8080** — первый зарегистрированный пользователь становится администратором. Остановка: `Ctrl+C`.

### 1.4. Если что-то пошло не так

| Симптом | Решение |
|---|---|
| `pip` ругается на версию Python | Нужен Python 3.12+: `python --version` |
| Порт 8080 занят | В `.env`: `WEB_PORT=8090` |
| 401 в логах API | Проверьте `CLIENT_ID`/`CLIENT_SECRET`; бот уходит в `AUTH_BACKOFF_SEC` и не спамит. После правки `.env` перезапустите |
| Логи нечитаемы | `LOG_JSON=false` в `.env` |
| Начать с чистого листа | Остановите бота, удалите `data/stalzone.db`, запустите снова |

### 1.5. База от старой версии проекта

Если `DATABASE_URL` указывает на базу прошлой версии (ошибки `Can't locate revision identified by '005'`), — начиная с v3 старые таблицы автоматически переименуются в `legacy_*` (данные сохранятся), заметки перенесутся в сделки. Учётки не переносятся — зарегистрируйтесь заново.

---

## Часть 2. Сервер (Linux + Docker)

Лимит памяти контейнера 480 МБ, OOM-сторож следит за запасом (`OOM_WATCHDOG_MB=50`).

```bash
cd /opt/stalzone-bot
cp .env.example .env   # CLIENT_ID, CLIENT_SECRET, DISCORD_WEBHOOK_URL
docker compose up -d --build                                   # SQLite
# или PostgreSQL:
echo "DB_PASSWORD=$(openssl rand -hex 16)" >> .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

```bash
docker compose logs -f stalzone     # логи
docker compose restart stalzone     # перезапуск (подтянет config/*.yaml)
```

- Метрики: `http://<ip>:8080/metrics` (с `METRICS_TOKEN` — только с `?token=`)
- Health: `/health` (readiness) и `/health/live`
- Бэкап SQLite: volume `stalzone-data`; Postgres: `docker exec stalzone-db pg_dump -U stalzone stalzone > backup.sql`

## Часть 3. Горячая конфигурация без перезапуска

- `config/artifacts.yaml`, `config/seasons.yaml` — перечитываются автоматически (~раз в 30 с).
- Фичефлаги админки (`scanner_enabled`, `history_sync_enabled`, `meta_monitor_enabled`, `season_monitor_enabled`, `charts_enabled`, `discord_enabled`) применяются рантаймом каждые 30 с — см. DECISIONS D10.

## Часть 4. Разработка

```bash
pip install -r requirements-dev.txt
pytest && ruff check . --fix && black . && mypy .
cd frontend && npm ci && npm run dev      # dev-сервер с прокси на :8080
```
