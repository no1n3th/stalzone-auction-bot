# Stalzone Bot v3

Асинхронная система мониторинга аукциона **STALCRAFT: X**: сканер лотов, история продаж, поиск недооценённых лотов (включая средние заточки +2…+14), мета-аналитика, сезонность, уведомления в Discord и веб-панель с кабинетом трейдера.

> **ЗБТ (2026-09-30):** числа тестов и порог покрытия в README/pyproject приведены к факту
> после прогона `pytest` и `npm test` на целевой машине. См. `CHANGELOG.md` и `DECISIONS.md`.

## Стек

| Слой | Технологии |
|---|---|
| Ядро | Python 3.12, полностью async (asyncio); блокирующие вызовы — в thread pool |
| Web API | FastAPI, Pydantic v2 (автогенерация OpenAPI: Swagger UI на `/docs`) |
| БД | SQLAlchemy 2.0 async: SQLite (aiosqlite) локально / PostgreSQL (asyncpg) на сервере, Alembic-миграции |
| Фронтенд | SPA: React 18 + Vite + TypeScript + Tailwind (готовая сборка лежит в `web/static/spa`) |
| Лайв-лента | WebSocket `/api/ws/feed` |
| Уведомления | Discord webhook (embed из словаря + PNG-график на чистом stdlib), outbox для гарантированной доставки, DLQ с повторной отправкой |

## Быстрый старт

Подробный гайд — в [RUNBOOK.md](RUNBOOK.md). Кратко:

**Windows / локально (нужен только Python 3.12+):**
```bat
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   :: вписать CLIENT_ID / CLIENT_SECRET
python main.py
```
Открыть http://localhost:8080. «Рынок» доступен без входа; для «Сделок» нужна регистрация (`/#/auth`). Первый пользователь становится администратором (режим `REGISTRATION_MODE=bootstrap`).

**Сервер (Docker):**
```bash
cp .env.example .env     # вписать CLIENT_ID / CLIENT_SECRET
docker compose up -d --build                       # SQLite
# или с PostgreSQL:
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## Проверка качества

```bash
pip install -r requirements-dev.txt
ruff check . && black --check . && mypy .    # линтеры, строгая типизация
pytest                                       # тесты (порог покрытия — в pyproject)
cd frontend && npm ci && npm run typecheck && npm run lint && npm test && npm run build
```

## Структура

- `scanner/` — фильтры лотов, взвешенный планировщик, движок сканирования, воркеры
- `analytics/` — мета-правила (дамп/выход/вход), сезонность, дайджесты и прогноз
- `database/` — ORM, репозитории, кольцевой буфер истории
- `deals/` — кабинет трейдера: сделки, математика профита, optimistic locking
- `web/` — FastAPI-роутеры, схемы, SPA-статика
- `config/` — `settings.py`, `artifacts.yaml`, `seasons.yaml` (hot reload)
- `frontend/` — исходники React SPA (пересборка: `cd frontend && npm ci && npm run build`)

Ключевые архитектурные решения — в [DECISIONS.md](DECISIONS.md), история изменений — в [CHANGELOG.md](CHANGELOG.md).

## Безопасность

За HTTPS включите `COOKIE_SECURE=true`. Все секреты задаются только через `.env` (см. `.env.example` — там плейсхолдеры, а не реальные значения). Никогда не коммитьте `.env` и не переиспользуйте учётные данные, которые где-либо публиковались: перевыпустите `CLIENT_ID`/`CLIENT_SECRET` у EXBO и вебхуки Discord.
