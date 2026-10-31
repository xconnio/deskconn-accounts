# Deskconn Account Service

Deskconn Account Service manages user accounts, devices, and desktop information.

## Getting Started

Before starting this service, make sure the [Deskconn Router](https://github.com/xconnio/deskconn-router) is running.

The service uses the published `xconn-core[framework]` Python package. Its wheels
include the Rust runtime, so deployment needs no Rust toolchain. `make run` and
the container start `main.py`, using the existing router and cryptosign settings.

## Setup Instructions

1. Install dependencies:

```shell
make setup
```

2. Configure environment variables:

Create or edit the `.env` file with appropriate values:

```dotenv
ACCOUNT_SERVICE_DB_USER=account-service
ACCOUNT_SERVICE_DB_PASSWORD=account-service-password
ROUTER_DB_USER=router
ROUTER_DB_PASSWORD=router-password
DESKCONN_POSTGRES_HOST=localhost
DESKCONN_DATABASE_URL=postgresql+asyncpg://${ACCOUNT_SERVICE_DB_USER}:${ACCOUNT_SERVICE_DB_PASSWORD}@${DESKCONN_POSTGRES_HOST}:5432/deskconn_account_service
DESKCONN_ACCOUNT_AUTHID=deskconn-account-service
DESKCONN_ACCOUNT_PRIVATE_KEY=db3f6235591a98b704f87f46f66d74645864479f32446a32d95c4826a6791b0a
RESEND_API_KEY=your-resend-api-key
ROUTER_URL=ws://localhost:8080/ws
```

> **Debug mode:** Set `X_DEBUG=true` to skip email sending and print OTPs to stdout instead. When enabled, `RESEND_API_KEY` is not required. Useful for local development.


3. Start Postgres and apply migrations:

```shell
make db
```

## Running the Service

```shell
make run
```

## Running with Docker

Before running with Docker, make sure [Deskconn Router](https://github.com/xconnio/deskconn-router) is already running via its own `docker-compose.yml`.

Set these values in your `.env` for Docker (both stacks share the `deskconn` docker network):

```dotenv
DESKCONN_POSTGRES_HOST=deskconn-account-service-postgres
ROUTER_URL=rs://deskconn-router:8084/rs
```

> **Note:** `make db` runs migrations from the host, so keep `DESKCONN_POSTGRES_HOST=localhost` while running it.

Then start the service:

```shell
make run-docker
```
