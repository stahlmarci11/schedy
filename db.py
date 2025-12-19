import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Render/Neon esetén gyakran "postgres://..." jön, SQLAlchemy viszont "postgresql://"-t szeret
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    # helyi fejlesztéshez marad az SQLite
    engine = create_engine("sqlite:///./family_calendar.db", connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
