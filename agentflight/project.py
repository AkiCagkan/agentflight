"""Project root and `agentflight.toml` configuration.

    [project]
    name = "demo"
    model = "auto"       # default model of every agent

    [budget]
    total = 1.0          # USD per run

    [prices]             # USD per million input / output tokens
    "gemini-flash-latest" = [0.30, 2.50]
"""

from __future__ import annotations

import tomllib
from pathlib import Path

CONFIG_FILE = "agentflight.toml"


def find_root(start) -> Path:
    path = Path(start).resolve()
    if path.is_file():
        path = path.parent
    candidate = path
    while True:
        if (candidate / CONFIG_FILE).is_file():
            return candidate
        if candidate.parent == candidate:
            return path
        candidate = candidate.parent


def read_config(root: Path) -> dict:
    file = Path(root) / CONFIG_FILE
    if not file.is_file():
        return {}
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"could not read {file}: {e}") from e


def default_model(config: dict) -> str | None:
    model = (config.get("project") or {}).get("model")
    return model or None


def total_budget(config: dict) -> float | None:
    value = (config.get("budget") or {}).get("total")
    return float(value) if value is not None else None


_TEMPLATE = {
    "agentflight.toml": '[project]\nname = "{name}"\n# Default model of every agent.\n'
                 '# "auto": the first connected account or API key (Claude, ChatGPT, Gemini, OpenAI), '
                 'else a local model, else the offline "fake" model.\n'
                 'model = "auto"\n\n[budget]\n# Maximum spend of one run (USD, API keys only).\ntotal = 1.0\n',
    ".gitignore": ".agentflight/\n__pycache__/\n",
    "prompts/assistant.md": "You are a helpful assistant who gives short, clear answers.\n"
                            "Do not make things up; if you are not sure, say so.\n",
    "main.py": '''from agentflight import Agent, Eval, Schema, field, llm


class Answer(Schema):
    text: str
    confidence: float = field("0-1, how confident you are in the answer", min=0, max=1)


class Assistant(Agent):
    """prompts/assistant.md"""

    @llm
    def ask(self, question: str) -> Answer: ...


class ShortAnswers(Eval, target=Assistant.ask):
    inputs = ["What is the capital of Turkey?", "What is 2 + 2?"]

    @staticmethod
    def check(input, result):
        assert len(result.text) < 400, "answer too long"


if __name__ == "__main__":
    answer = Assistant().ask("What is an AI agent? Answer in one sentence.")
    print(answer.text, f"(confidence: {answer.confidence:.0%})")
''',
}


def create_project(folder: Path) -> list[str]:
    """Creates the files of a new project; existing files are left alone. Returns the files written."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in _TEMPLATE.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content.replace("{name}", folder.name), encoding="utf-8")
            written.append(name)
    return written
