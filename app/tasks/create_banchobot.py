"""BanchoBot user creation startup task."""

from app.const import BANCHOBOT_ID
from app.database.statistics import UserStatistics
from app.database.user import User
from app.dependencies.database import with_db
from app.log import logger
from app.models.score import GameMode
from app.service.stable_import.bancho_db import get_bancho_engine

from sqlalchemy import text
from sqlmodel import exists, select


async def create_banchobot() -> None:
    """Create the BanchoBot system user if it doesn't exist.

    Reads identity data (username, email, country) from the shared stable DB
    so the lazer profile matches bancho.py's BanchoBot exactly.  Falls back to
    ppy.sh defaults when the stable DB is unreachable (standalone deploys).
    """
    async with with_db() as session:
        is_exist = (await session.exec(select(exists()).where(User.id == BANCHOBOT_ID))).first()
        if not is_exist:
            username, email, country_code = "BanchoBot", "banchobot@ppy.sh", "SH"
            try:
                async with get_bancho_engine().connect() as conn:
                    row = (
                        await conn.execute(
                            text("SELECT name, email, country FROM users WHERE id = :id"),
                            {"id": BANCHOBOT_ID},
                        )
                    ).mappings().first()
                    if row:
                        username = row["name"]
                        email = row["email"]
                        country_code = (row["country"] or "SH").upper()
            except Exception:
                pass  # stable DB unreachable — use defaults

            banchobot = User(
                id=BANCHOBOT_ID,
                username=username,
                email=email,
                is_bot=True,
                pw_bcrypt="0",
                avatar_url=f"https://a.ppy.sh/{BANCHOBOT_ID}",
                country_code=country_code,
                website="https://twitter.com/banchoboat",
            )
            session.add(banchobot)
            session.add(UserStatistics(user_id=BANCHOBOT_ID, mode=GameMode.OSU))
            await session.commit()
            logger.success(f"BanchoBot user created (username={username}, country={country_code})")
