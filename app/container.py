from repository.manage_database import DatabaseManager
from ai_integrations import ai_integration

class Services:
    def __init__(self):
        self.db = DatabaseManager()
        self.gpt_client = ai_integration.GPTIntegration()
        self.claude_client = ai_integration.ClaudeIntegration()

    def is_running(self) -> bool:
        if self.db:
            return True
        else:
            return False

services = Services()