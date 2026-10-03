.PHONY: install dev test lint typecheck check frontend docker docker-prod

install:        ## Установить python-зависимости
	pip install -r requirements.txt -r requirements-dev.txt

dev:            ## Запуск локально (SQLite)
	python main.py

test:           ## Тесты с покрытием (гейт 79%, см. pyproject)
	pytest

lint:           ## ruff + black --check
	ruff check . && black --check .

typecheck:      ## mypy --strict
	mypy .

check: lint typecheck test  ## Все гейты качества

frontend:       ## Пересобрать SPA в web/static/spa
	cd frontend && npm ci && npm run build

docker:         ## Сборка и запуск (SQLite)
	docker compose up -d --build

docker-prod:    ## Сборка и запуск (PostgreSQL)
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
