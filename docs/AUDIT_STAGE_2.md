# AUDIT_STAGE_2 — результаты работы по AUDIT.md (2026-09-29/30)

Baseline: этапы A+B (все гейты) + C1–C3. Гейты: `ruff check . && black --check . && mypy . && pytest`.

## Закрыто (ключевые находки)

| ID | Фикс |
|---|---|
| D1 | `sold_at` из API-поля `time`; unique-индекс дедупа; `ON CONFLICT DO NOTHING`; миграция 003 |
| D2 | Неликвид без порога → 30% по умолчанию |
| D3 | `THRESHOLD_MODE=net` (комиссия один раз) |
| D4 | Планировщик: rebuild сохраняет `due_at`, jitter холодного старта, один heap-элемент на defer |
| D5 | 4xx → `ItemRequestError` + карантин 1 ч |
| R2 | OOM-watchdog вычитает inactive_file (page cache) |
| R6 | DLQ: per-row изоляция, 10 попыток → dead-letter, backoff-кап |
| R8 | Дедуп дамп/мета-алертов: реалерт только при ухудшении |
| R9 | `mark_sent` — диалектозависимый upsert |
| S1 | Регистрация bootstrap-only; lock до commit |
| B2 | Тесты против PostgreSQL: `--db postgresql` |
| B1-parity | Тест паритета `alembic upgrade head` ↔ ORM-метаданные |

Полный список — в git-истории коммитов сессий. Финализация ЗБТ (3.1.1): outback-off backoff,
per-webhook доставка, админ-флаги в рантайме (D10), иконки из листинга (D11),
стабилизация по пост-инвалидационным продажам (D12) — см. CHANGELOG.md.

## Проверки владельцу (вне sandbox)

1. `pytest --db postgresql` против живого Postgres 16.
2. Фикстуры живого API: `tests/fixtures/history.json`, обрезанный `listing.json` (поле `icon`).
3. Контрольный прогон сканера после `THRESHOLD_MODE=net`.
