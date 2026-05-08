import json
import re
from os import getenv

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel

load_dotenv()


class GPTResponseStructure(BaseModel):
    title: str


class GPTIntegration:
    client: AsyncOpenAI

    def __init__(self):
        self.client = AsyncOpenAI(api_key=getenv("OPENAI_API_KEY"))

    async def get_post_overview(self, post: str, model: str = "gpt-5.4-mini") -> str:
        response = await self.client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": getenv("PROMPT_POST_OVERVIEW", "")},
                {"role": "user", "content": post},
            ],
            text_format=GPTResponseStructure,
        )
        return response.output_parsed.title


class ClaudeIntegration:
    client: AsyncAnthropic

    def __init__(self):
        self.client = AsyncAnthropic(api_key=getenv("CLAUDE_API_KEY"))

    async def get_post_title(self, post: str, model: str = "claude-sonnet-4-6") -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            system=getenv("PROMPT_POST_OVERVIEW", "return <PROMPT NOT GIVEN> as a response"),
            messages=[{"role": "user", "content": post}],
        )
        return _extract_title(response.content[0].text)


def _extract_title(raw: str) -> str:
    """The prompt promises strict JSON, but tolerate stray prose around it just in case."""
    try:
        return json.loads(raw)["title"]
    except (json.JSONDecodeError, KeyError, TypeError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))["title"]
        raise
