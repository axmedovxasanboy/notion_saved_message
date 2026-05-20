from typing import Optional

from repository.manage_database import DatabaseManager
from ai_integrations import ai_integration


class Services:
    """Application services container.

    Heavy resources (DB engine, AI clients) are constructed lazily on first
    access so that importing `container` doesn't open the DB or hit the
    network — useful for tests and for static analysis tools that import
    modules without intending to run them.
    """

    def __init__(self) -> None:
        self._db: Optional[DatabaseManager] = None
        self._gpt_client: Optional[ai_integration.GPTIntegration] = None
        self._claude_client: Optional[ai_integration.ClaudeIntegration] = None

    @property
    def db(self) -> DatabaseManager:
        if self._db is None:
            self._db = DatabaseManager()
        return self._db

    @property
    def gpt_client(self) -> ai_integration.GPTIntegration:
        if self._gpt_client is None:
            self._gpt_client = ai_integration.GPTIntegration()
        return self._gpt_client

    @property
    def claude_client(self) -> ai_integration.ClaudeIntegration:
        if self._claude_client is None:
            self._claude_client = ai_integration.ClaudeIntegration()
        return self._claude_client

    def is_running(self) -> bool:
        return self.db is not None


services = Services()
