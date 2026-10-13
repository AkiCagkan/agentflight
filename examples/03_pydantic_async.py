# 03 · Pydantic models and async
# Return types and tool parameters can be Pydantic models; @llm methods can be async.
# (Needs `pip install pydantic`.)

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from agentflight import Agent, llm


class Ticket(BaseModel):
    """A support ticket read from a customer e-mail."""
    category: Literal["billing", "technical", "account", "other"]
    urgency: int = Field(ge=1, le=5, description="1 = can wait, 5 = production is down")
    summary: str = Field(description="One sentence")


class Triage(Agent):
    """You read customer e-mails and turn them into support tickets."""
    temperature = 0

    @llm
    async def triage(self, email: str) -> Ticket: ...


EMAILS = [
    "Hi, I was charged twice for my October invoice. Could you refund one of the payments?",
    "Our boiler monitoring dashboard has shown no data since 6 am; the night shift cannot see the pressure readings.",
    "How do I change the e-mail address on my account? The old one belongs to a colleague who left.",
]


async def main():
    tickets = await asyncio.gather(*(Triage().triage(e) for e in EMAILS))   # the calls run concurrently
    for ticket in sorted(tickets, key=lambda t: -t.urgency):
        print(f"[{ticket.urgency}] {ticket.category:<9} {ticket.summary}")


if __name__ == "__main__":
    asyncio.run(main())
