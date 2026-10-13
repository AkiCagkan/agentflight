# 01 · Summary
# An agent, a return type and an eval.   Run:  python 01_summary.py   (or: agentflight run 01_summary.py)
#                                        Eval: agentflight eval 01_summary.py

from agentflight import Agent, Eval, Schema, field, llm


class Summary(Schema):
    title: str = field("The main topic of the text, at most 8 words")
    bullets: list[str] = field("The key points of the text", min=2, max=5)
    score: int = field("How dense and clear the text is", min=1, max=10)


class Summarizer(Agent):
    """prompts/summary.md"""
    temperature = 0
    budget = 0.20

    @llm
    def summarize(self, text: str) -> Summary: ...


class SummaryRules(Eval, target=Summarizer.summarize):
    inputs = "data/*.txt"
    repeat = 2
    threshold = 0.9

    @staticmethod
    def check(input, result):
        assert 2 <= len(result.bullets) <= 5, "2-5 bullets"
        assert len(result.title.split()) <= 8, "title must be at most 8 words"


if __name__ == "__main__":
    text = open("data/biogas.txt", encoding="utf-8").read()
    summary = Summarizer().summarize(text)

    print(f"\n{summary.title}  (score {summary.score}/10)")
    for bullet in summary.bullets:
        print(" •", bullet)
