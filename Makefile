.PHONY: setup db down

IMAGE := xconnio/deskconn-account-service
VERSION := $(shell git describe --tags --always)

-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

REQUIRED_VARS = DESKCONN_ACCOUNT_AUTHID DESKCONN_ACCOUNT_PRIVATE_KEY

# check for missing or empty envs
define check_defined
    @: $(foreach v,$(1),$(if $(filter-out "",$(strip $($(v)))),,$(error Missing or empty environment variable: $(v))))
endef

install_uv:
	@if ! command -v uv >/dev/null 2>&1; then \
  		curl -LsSf https://astral.sh/uv/install.sh | sh; \
  	fi

setup:
	cp example.env .env
	make install_uv
	uv venv
	uv pip install -e .[test] -U

format:
	./.venv/bin/ruff format .

lint:
	./.venv/bin/ruff check .

test:
	./.venv/bin/pytest -s -v tests

clean:
	rm -rf *.egg-info build

run:
	$(call check_defined,$(REQUIRED_VARS))
	./.venv/bin/python main.py

migration:
	./.venv/bin/alembic revision --autogenerate -m "$(name)"

migrate:
	./.venv/bin/alembic upgrade head

db:
	docker compose up -d postgres
	until docker compose exec -T postgres pg_isready -U postgres -d postgres >/dev/null 2>&1; do sleep 1; done
	docker compose exec -T postgres /docker-entrypoint-initdb.d/01-init.sh
	$(MAKE) migrate

down:
	docker compose down

DOCKER_BUILD := docker buildx build -t $(IMAGE):$(VERSION) -t $(IMAGE):latest

build-docker:
	$(DOCKER_BUILD) --platform linux/amd64,linux/arm64 .

build-docker-amd64:
	$(DOCKER_BUILD) --platform linux/amd64 .

build-docker-arm64:
	$(DOCKER_BUILD) --platform linux/arm64 .

push-docker:
	@[ "$$CI" = true ] || { echo "push-docker only runs in CI"; exit 1; }
	$(DOCKER_BUILD) --platform linux/amd64,linux/arm64 --push .

run-docker:
	docker compose up deskconn-account-service
