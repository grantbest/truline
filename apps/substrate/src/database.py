from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import os

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set")

SQLALCHEMY_ECHO = os.getenv("SQLALCHEMY_ECHO", "").lower() in {"1", "true", "yes"}

engine = create_async_engine(DATABASE_URL, echo=SQLALCHEMY_ECHO)
async_session = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db():
    async with async_session() as session:
        yield session
