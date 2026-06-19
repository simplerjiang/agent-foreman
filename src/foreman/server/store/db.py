"""Server database session/engine wrapper (team/relay mode). Separate from the client's
store. Placeholder for P7 (DESIGN §7.2)."""

from __future__ import annotations

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from . import models  # noqa: F401  (registers server tables on SQLModel.metadata)


class ServerStore:
    def __init__(self, db_path: str = "foreman-server.db") -> None:
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )

    def init(self) -> None:
        """Create ONLY the server tables (scoped, so a shared metadata never pulls in client tables)."""
        SQLModel.metadata.create_all(self.engine, tables=[m.__table__ for m in models.SERVER_TABLES])

    def session(self) -> DBSession:
        return DBSession(self.engine)
