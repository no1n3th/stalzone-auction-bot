"""Сидер тестовой истории продаж: наполняет auction_history за N дней.

Usage:  python -m scripts.seed_history            # 9 предметов × 30 дней
Осторожно: для артефактов из artifacts.yaml идемпотентен (dedup-индекс),
повторный прогон не плодит дубликаты.
"""
from __future__ import annotations

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta

from config.settings import get_settings
from database.engine import Database
from database.repositories.history import HistoryRepository

BASE: dict[str, float] = {
    "y5vw": 1500, "rn1z": 800, "ljpq": 400, "qoq6": 250, "5rd1": 350,
    "w4jo": 900, "rnkl": 300, "51l0": 450, "yq3o": 200,
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.database_url)
    await db.connect()

    now = datetime.now(UTC)
    rng = random.Random(42)  # детерминированно

    async with db.session() as session:
        repo = HistoryRepository(session, rows_per_item=50)
        for item_id, base in BASE.items():
            rows = []
            for day in range(args.days, 0, -1):
                for _ in range(rng.randint(3, 12)):
                    price = base * (1 + rng.uniform(-0.15, 0.15))
                    rows.append({
                        "item_id": item_id, "quality": rng.choice([3, 4, 4, 5]),
                        "upgrade": rng.choice([0, 15]), "price": round(price, 2),
                        "amount": 1,
                        "sold_at": now - timedelta(days=day, hours=rng.uniform(0, 20)),
                    })
            inserted = await repo.bulk_insert(rows)
            print(f"  {item_id}: вставлено {inserted} продаж")

    await db.close()
    print("Готово. Обновите страницу «Рынок».")


if __name__ == "__main__":
    asyncio.run(main())
