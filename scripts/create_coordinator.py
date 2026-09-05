"""Создать координатора программы без HTTP.

Для стенда и первого продакшен-аккаунта, когда COORDINATOR_INVITE_CODE
ещё не раздали.

    python scripts/create_coordinator.py \\
        --email coord@example.com --password '...' --name 'Координатор'
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Репозиторий в PYTHONPATH, если запускают из корня или из scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.auth import hash_password
from app.database import async_session_factory
from app.models import User, UserRole


async def main() -> None:
    parser = argparse.ArgumentParser(description="Create a programme coordinator")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    async with async_session_factory() as session:
        existing = await session.scalar(select(User).where(User.email == args.email))
        if existing is not None:
            if existing.role == UserRole.coordinator:
                print(f"Уже есть координатор {args.email}")
                return
            raise SystemExit(
                f"Пользователь {args.email} уже существует с ролью {existing.role.value}"
            )

        session.add(
            User(
                email=args.email,
                full_name=args.name,
                password_hash=hash_password(args.password),
                role=UserRole.coordinator,
            )
        )
        await session.commit()
        print(f"Координатор создан: {args.email}")


if __name__ == "__main__":
    asyncio.run(main())
