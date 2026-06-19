"""Database session/engine wrapper."""

from __future__ import annotations

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from . import models  # noqa: F401  (import registers tables on SQLModel.metadata)


class Store:
    def __init__(self, db_path: str = "foreman.db") -> None:
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    def init(self) -> None:
        """Create tables if they don't exist."""
        SQLModel.metadata.create_all(self.engine)

    def session(self) -> DBSession:
        return DBSession(self.engine)
