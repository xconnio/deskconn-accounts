import os

# Tests must not load production database credentials or send email.
os.environ["DESKCONN_DATABASE_URL"] = "postgresql+asyncpg://test:test@127.0.0.1:1/test"
os.environ["X_DEBUG"] = "true"
