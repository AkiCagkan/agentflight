# 02 · Research pipeline
# Agents with tools, parallel calls, a human approval step and chained agents.
# Open the panel (`agentflight watch` or the AgentFlight panel in your editor) and run this file: you see the three
# researchers working at the same time, their tool calls, and the approval request.

import json
import urllib.parse
import urllib.request

from agentflight import Agent, Schema, field, llm, parallel, require_approval, tool


class Report(Schema):
    topic: str
    summary: str = field("3-4 sentence summary")
    sources: list[str] = field("Titles of the Wikipedia articles used")


class Newsletter(Schema):
    title: str = field("Title of the newsletter, at most 10 words")
    intro: str = field("Two sentences that introduce the topics")
    sections: list[str] = field("A short paragraph for each topic", min=1)


@tool
def wikipedia_lookup(topic: str) -> str:
    """Fetches the summary of an English Wikipedia article.

    topic: article title, e.g. "Biogas"
    """
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(topic.strip().replace(" ", "_"))
    request = urllib.request.Request(url, headers={"User-Agent": "agentflight-example/0.4"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except Exception as error:  # noqa: BLE001 - the model gets the error as the tool result
        return f"no article found for '{topic}' ({error})"
    return f"{data.get('title')}: {data.get('extract', '')}"


class Researcher(Agent):
    """prompts/researcher.md"""
    tools = [wikipedia_lookup]
    budget = 0.30

    @llm
    def research(self, topic: str) -> Report: ...


class Editor(Agent):
    """You write short, readable science newsletters.
    Use only the information in the reports you are given."""

    @llm
    def write_newsletter(self, reports: list[Report]) -> Newsletter: ...


class Translator(Agent):
    """You translate texts into fluent Turkish, keeping the meaning. Return only the translation."""

    @llm
    def translate(self, text: str) -> str: ...


if __name__ == "__main__":
    # The three researchers run at the same time.
    biogas, heat_pump, turbine = parallel(
        lambda: Researcher().research("Biogas"),
        lambda: Researcher().research("Heat pump"),
        lambda: Researcher().research("Wind turbine"),
    )

    newsletter = Editor().write_newsletter([biogas, heat_pump, turbine])

    # The program waits here: Approve / Edit / Reject in the panel (or a question in the terminal).
    newsletter = require_approval(newsletter, "Publish this newsletter?")

    turkish = Translator().translate(newsletter.intro)

    print("\n" + newsletter.title)
    print(newsletter.intro)
    for section in newsletter.sections:
        print("\n" + section)
    print("\nTR:", turkish)
