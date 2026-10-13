# 00 · Hello
# The smallest program: one agent, one method. The body of an @llm method is not written; the model does the work.

from agentflight import Agent, llm


class Greeter(Agent):
    """You write short, warm, one-sentence greetings."""

    @llm
    def greet(self, who: str) -> str: ...


print(Greeter().greet("someone trying AgentFlight for the first time"))
