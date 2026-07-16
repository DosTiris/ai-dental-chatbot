from sqlalchemy import create_engine  # Import engine creator for SQLAlchemy
from sqlalchemy.orm import sessionmaker, declarative_base  # Import session factory and base class builder
from app.config import DATABASE_URL  # Import DB url from config

engine = create_engine(DATABASE_URL, pool_pre_ping=True)  # Create DB engine and keep connections healthy
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)  # Create DB session factory
Base = declarative_base()  # Create base class for ORM models
