"""Agents: classes whose `@llm` methods are carried out by a model.

    class Summarizer(Agent):
        \"\"\"prompts/summary.md\"\"\"          # instructions: text or a file path
        model = "claude-opus-5-5"
        tools = [read_file]
        budget = 0.50

        @llm
        def summarize(self, text: str) -> Summary: ...

A call to an `@llm` method:
  1. builds the prompt from the instructions + the method's task (docstring) + the return schema,
  2. talks to the model turn by turn, running tools the model asks for,
  3. validates the final answer against the return type and asks the model to fix it if needed,
  4. publishes every step as an event (live view) and writes it to the run record (replay).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import sys
import time
import typing
from dataclasses import asdict
from pathlib import Path

from .errors import AgentStopped, InstructionsNotFound, MaxTurnsExceeded, ProviderError, SchemaError
from .prompts import format_error_message, interjection, system_prompt, user_prompt
from .providers import find_provider, mark_unavailable
from .providers.base import Request
from .runtime import caller_location, current_call, current_run
from .schema import convert, from_json, output_schema, parse_output, to_jsonable, type_name
from .tools import to_tools

SETTINGS = ("model", "temperature", "seed", "tools", "budget", "max_turns", "retries", "memory", "effort", "max_tokens")


def _resolve_instructions(doc: str, klass) -> str:
    text = inspect.cleandoc(doc)
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    if first.endswith((".md", ".txt")) and " " not in first:
        module = sys.modules.get(klass.__module__)
        file = getattr(module, "__file__", None) if module else None
        base = Path(file).parent if file else Path.cwd()
        path = (base / first).resolve()
        if not path.is_file():
            raise InstructionsNotFound(f"instructions file for {klass.__name__} not found: {path}")
        rest = "\n".join(lines[1:]).strip()
        content = path.read_text(encoding="utf-8").strip()
        return content + ("\n\n" + rest if rest else "")
    return text


def _own_instructions(klass) -> str | None:
    """The instructions a class defines itself: `instructions = "..."` or its docstring."""
    value = klass.__dict__.get("instructions")
    if isinstance(value, str) and value.strip():
        return value
    return klass.__dict__.get("__doc__") or None


class Agent:
    """Base class of agents. Methods marked with `@llm` are carried out by the model."""

    __agentflight_agent__ = True
    model: str | None = None
    temperature: float | None = None
    seed: int | None = None
    tools: typing.Sequence = ()
    budget: float | None = None
    max_turns: int = 12
    retries: int = 2
    memory: bool = False
    effort: str | None = None
    max_tokens: int | None = None

    def __init__(self, **settings):
        for name, value in settings.items():
            if name not in SETTINGS:
                raise TypeError(f"{type(self).__name__}: unknown setting '{name}' (valid: {', '.join(SETTINGS)})")
            setattr(self, name, value)
        self._history: list[dict] = []

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        # Lets evals find the agent from `target=Class.method` (classes defined inside functions included).
        for value in cls.__dict__.values():
            if getattr(value, "__agentflight_llm__", False) and getattr(value, "__agentflight_owner__", None) is None:
                value.__agentflight_owner__ = cls

    @classmethod
    def instructions_text(cls) -> str:
        """The resolved instructions (file contents if the instructions are a file path)."""
        for klass in cls.__mro__:
            if klass is Agent or klass is object:
                break
            doc = _own_instructions(klass)
            if doc:
                return _resolve_instructions(doc, klass)
        return ""

    @classmethod
    def instructions_file(cls) -> str | None:
        """The path of the instructions file, if the instructions come from a file."""
        for klass in cls.__mro__:
            if klass is Agent or klass is object:
                break
            doc = _own_instructions(klass)
            if doc:
                first = inspect.cleandoc(doc).splitlines()[0].strip()
                if first.endswith((".md", ".txt")) and " " not in first:
                    module = sys.modules.get(klass.__module__)
                    file = getattr(module, "__file__", None) if module else None
                    return str(((Path(file).parent if file else Path.cwd()) / first).resolve())
                return None
        return None

    @classmethod
    def llm_methods(cls) -> list[str]:
        return [name for name in dir(cls) if getattr(getattr(cls, name, None), "__agentflight_llm__", False)]

    def clear_memory(self) -> None:
        """Forgets the conversation kept by `memory = True`."""
        self._history.clear()

    def __repr__(self):
        return f"<agent {type(self).__name__}>"


def _type_hints(fn) -> dict:
    try:
        return typing.get_type_hints(fn)
    except Exception as e:
        raise SchemaError(f"{fn.__qualname__}: could not resolve type annotations ({e})") from e


_F = typing.TypeVar("_F", bound=typing.Callable[..., typing.Any])


def llm(fn: _F) -> _F:
    """Binds an agent method to the model: the body is not written (`...`), the model does the work.

        class Summarizer(Agent):
            \"\"\"prompts/summary.md\"\"\"

            @llm
            def summarize(self, text: str) -> Summary: ...

            @llm
            async def summarize_async(self, text: str) -> Summary: ...   # awaitable

    The method's docstring is sent as the task of that call; its parameters are the input and its
    return annotation is the schema the answer must match."""
    signature = inspect.signature(fn)

    if inspect.iscoroutinefunction(fn):
        import asyncio

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            # Model calls are synchronous (run in a thread); async code awaits them without blocking the event loop.
            return await asyncio.to_thread(_agent_call, self, fn, signature, args, kwargs)

        wrapper.__agentflight_llm__ = True
        wrapper.__agentflight_signature__ = signature
        return typing.cast(_F, wrapper)

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        return _agent_call(self, fn, signature, args, kwargs)

    wrapper.__agentflight_llm__ = True
    wrapper.__agentflight_signature__ = signature
    return typing.cast(_F, wrapper)


def _signature_text(fn, signature, hints, returns) -> str:
    params = []
    for p in list(signature.parameters.values())[1:]:
        if p.name in hints:
            params.append(f"{p.name}: {type_name(hints[p.name])}")
        else:
            params.append(p.name)
    return f"{fn.__name__}({', '.join(params)}) -> {type_name(returns)}"


def _plain_messages(messages: list[dict], truncate: int = 4000) -> list[dict]:
    """Messages for events and records, without the raw provider content."""
    result = []
    for m in messages:
        plain = {k: v for k, v in m.items() if k not in ("raw", "provider")}
        result.append(to_jsonable(plain, truncate))
    return result


def _agent_call(agent: Agent, fn, signature, args, kwargs):
    run = current_run()
    klass = type(agent)
    agent_name, method = klass.__name__, fn.__name__

    bound = signature.bind(agent, *args, **kwargs)
    bound.apply_defaults()
    hints = _type_hints(fn)
    returns = hints.get("return", str)
    inputs = {}
    errors: list[str] = []
    for name, value in list(bound.arguments.items())[1:]:
        inputs[name] = convert(hints[name], value, name, errors) if name in hints else value
    if errors:
        raise SchemaError(errors, f"{agent_name}.{method}: input does not match its types: " + "; ".join(errors))

    requested_model = run.choose_model(agent.model)
    auto = requested_model in ("auto", "")
    provider, model = find_provider(requested_model)
    instructions = klass.instructions_text()
    task = inspect.getdoc(fn)
    signature_text = _signature_text(fn, signature, hints, returns)
    schema = None if returns is str else output_schema(returns)
    strict = None if returns is str else output_schema(returns, strict=True)
    tools = to_tools(list(agent.tools or []))
    system = system_prompt(instructions, signature_text, task, schema, bool(tools))
    user = user_prompt(inputs)
    key = hashlib.sha256(json.dumps({
        "agent": agent_name, "method": method, "signature": signature_text, "system": system, "input": user,
        "model": model, "tools": sorted(t.name for t in tools),
        "history": _plain_messages(agent._history) if agent.memory else [],
    }, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:24]

    call_id = run.new_id("c")
    step = run.next_step()
    parent = current_call.get()
    file, line = caller_location()
    run.control.call_started(call_id, parent)
    run.bus.publish(
        "call_started", id=call_id, step=step, parent=parent, agent=agent_name, method=method,
        model=model, provider=provider.name, subscription=getattr(provider, "subscription", False),
        input=to_jsonable(inputs, truncate=1500), file=file,
        line=line, tools=[t.name for t in tools], returns=type_name(returns), signature=signature_text,
        system=system[:8000], instructions_file=klass.instructions_file(),
    )
    record = {
        "step": step, "id": call_id, "parent": parent, "key": key, "agent": agent_name,
        "method": method, "model": model, "file": file, "line": line, "input": to_jsonable(inputs),
        "system": system, "signature": signature_text,
    }
    t0 = time.monotonic()
    # Tokens and cost of this call, kept up to date turn by turn so that a call that fails or is stopped still
    # reports what it spent (the run totals always include it).
    usage = {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    token = current_call.set(call_id)
    try:
        previous = run.replay.take(key) if run.replay is not None else None
        if previous is not None:
            result = from_json(returns, previous["result"])
            record.update(status="done", result=previous["result"], messages=previous.get("messages", []),
                          input_tokens=0, output_tokens=0, cost=0.0, duration_ms=0, turns=0,
                          from_record=True, source_step=previous.get("step"))
            run.bus.publish("call_finished", id=call_id, status="done",
                            result=to_jsonable(previous["result"], truncate=4000),
                            from_record=True, duration_ms=0, cost=0.0, input_tokens=0, output_tokens=0, turns=0,
                            messages=previous.get("messages", []))
            if agent.memory:
                agent._history += [{"role": "user", "content": user},
                                   {"role": "assistant", "content": previous.get("raw_text", "")}]
            return result

        while True:
            try:
                loop = _session_loop if getattr(provider, "session_based", False) else _loop
                result, details = loop(run, agent, agent_name, method, call_id, provider, model, system, user,
                                       tools, returns, schema, strict, usage)
                break
            except ProviderError as e:
                # With `auto`, a provider that fails with an access error is skipped and the next one is tried.
                if not auto or provider.name == "fake" or getattr(e, "status", None) not in (401, 403, 404):
                    raise
                mark_unavailable(provider.name)
                old = model
                provider, model = find_provider("auto")
                record["model"] = model
                run.bus.publish("model_switched", id=call_id, old=old, new=model, reason=str(e)[:300])
        duration = int((time.monotonic() - t0) * 1000)
        result_json = to_jsonable(result)
        messages = _plain_messages(details["messages"])
        record.update(status="done", result=result_json, messages=messages, input_tokens=details["input_tokens"],
                      output_tokens=details["output_tokens"], cost=details["cost"], duration_ms=duration,
                      turns=details["turns"], raw_text=details["raw_text"], from_record=False)
        run.bus.publish("call_finished", id=call_id, status="done", result=to_jsonable(result_json, truncate=4000),
                        from_record=False, duration_ms=duration, cost=details["cost"],
                        input_tokens=details["input_tokens"], output_tokens=details["output_tokens"],
                        turns=details["turns"], messages=messages,
                        subscription=getattr(provider, "subscription", False))
        if agent.memory:
            agent._history += [{"role": "user", "content": user},
                               {"role": "assistant", "content": details["raw_text"]}]
        return result
    except AgentStopped:
        duration = int((time.monotonic() - t0) * 1000)
        record.update(status="stopped", duration_ms=duration, **usage)
        run.bus.publish("call_finished", id=call_id, status="stopped", duration_ms=duration, **usage)
        return None
    except Exception as e:
        duration = int((time.monotonic() - t0) * 1000)
        record.update(status="error", error=f"{type(e).__name__}: {e}", duration_ms=duration, **usage)
        run.bus.publish("call_finished", id=call_id, status="error", error=f"{type(e).__name__}: {e}",
                        duration_ms=duration, **usage)
        raise
    finally:
        current_call.reset(token)
        run.control.call_finished(call_id)
        if run.recorder is not None:
            run.recorder.call(record)


def _mismatch_message(agent_name, method, returns, retries, errors) -> str:
    return (f"{agent_name}.{method}: the answer did not match {type_name(returns)} "
            f"(after {retries} correction attempts): " + "; ".join(errors))


def _run_tool(run, call_id, tool_map, name, arguments) -> tuple[str, bool]:
    """Runs one tool call with live-view events: (result text, is error)."""
    run.control.checkpoint(call_id)
    tool_call_id = run.new_id("t")
    run.bus.publish("tool_started", id=call_id, tool_call_id=tool_call_id, name=name,
                    arguments=to_jsonable(arguments, truncate=500))
    t0 = time.monotonic()
    error = False
    tool = tool_map.get(name)
    if tool is None:
        content, error = f"Error: there is no tool named '{name}'.", True
    else:
        try:
            content = tool.run(arguments)
        except AgentStopped:
            raise
        except Exception as e:  # noqa: BLE001 - a failing tool is reported to the model, not raised
            content, error = f"Error: {e}", True
    run.bus.publish("tool_finished", id=call_id, tool_call_id=tool_call_id, name=name, result=content[:1500],
                    error=error, duration_ms=int((time.monotonic() - t0) * 1000))
    return content, error


def _loop(run, agent, agent_name, method, call_id, provider, model, system, user, tools, returns, schema, strict,
          usage):
    messages: list[dict] = [dict(m) for m in agent._history] if agent.memory else []
    messages.append({"role": "user", "content": user})
    tool_map = {t.name: t for t in tools}
    tool_definitions = [t.definition() for t in tools]
    format_attempts = 0

    def on_text(chunk: str):
        run.control.checkpoint(call_id)
        run.bus.publish("token", id=call_id, text=chunk)

    for turn in range(1, max(1, int(agent.max_turns)) + 1):
        run.control.checkpoint(call_id)
        for m in run.control.take_messages(call_id):
            messages.append({"role": "user", "content": interjection(m)})
        run.check_budget(agent_name, agent.budget)
        run.bus.publish("turn_started", id=call_id, turn=turn)
        usage["turns"] += 1
        request = Request(
            model=model, system=system, messages=messages, tools=tool_definitions, schema=schema,
            strict_schema=strict, temperature=agent.temperature, seed=agent.seed, effort=agent.effort,
            max_tokens=agent.max_tokens,
        )
        response = provider.call(request, on_text)
        subscription = getattr(provider, "subscription", False)
        cost = run.spend(agent_name, model, response.input_tokens, response.output_tokens, subscription=subscription)
        usage["input_tokens"] += response.input_tokens
        usage["output_tokens"] += response.output_tokens
        usage["cost"] += cost
        run.bus.publish("turn_finished", id=call_id, turn=turn, input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens, cost=cost, stop_reason=response.stop_reason,
                        total_cost=run.total_cost, subscription=subscription)
        messages.append({
            "role": "assistant", "content": response.text,
            "tool_calls": [asdict(tc) for tc in response.tool_calls],
            "raw": response.raw, "provider": provider.name,
        })

        if response.tool_calls:
            results = []
            for tc in response.tool_calls:
                content, error = _run_tool(run, call_id, tool_map, tc.name, tc.arguments)
                results.append({"id": tc.id, "name": tc.name, "content": content, "error": error})
            messages.append({"role": "tool", "results": results})
            continue

        pending = run.control.take_messages(call_id)
        if pending:
            for m in pending:
                messages.append({"role": "user", "content": interjection(m)})
            continue

        try:
            result = parse_output(returns, response.text)
        except SchemaError as e:
            format_attempts += 1
            run.bus.publish("format_error", id=call_id, errors=e.errors, attempt=format_attempts,
                            max_attempts=agent.retries, text=response.text[:2000])
            if format_attempts > agent.retries:
                raise SchemaError(e.errors, _mismatch_message(agent_name, method, returns, agent.retries,
                                                              e.errors)) from None
            messages.append({"role": "user", "content": format_error_message(e.errors)})
            continue
        return result, {"messages": messages, **usage, "raw_text": response.text}

    raise MaxTurnsExceeded(f"{agent_name}.{method} did not reach a result in {agent.max_turns} turns")


def _history_text(history: list[dict]) -> str:
    lines = []
    for m in history:
        if m["role"] == "user":
            lines.append(f"[user]\n{m['content']}")
        elif m["role"] == "assistant" and m.get("content"):
            lines.append(f"[you]\n{m['content']}")
    return "\n\n".join(lines)


def _session_loop(run, agent, agent_name, method, call_id, provider, model, system, user, tools, returns,
                  schema, strict, usage):
    """The path where the provider runs the tool loop itself (Claude Code). Tools are offered through a local
    MCP bridge; every tool call still goes through `_run_tool` (live view, approval, stopping)."""
    import contextvars

    from .mcp_bridge import ToolServer

    history = [dict(m) for m in agent._history] if agent.memory else []
    messages: list[dict] = history + [{"role": "user", "content": user}]
    tool_map = {t.name: t for t in tools}
    base_context = contextvars.copy_context()
    subscription = getattr(provider, "subscription", False)
    counters = usage

    def run_tool(name, arguments):
        # The MCP request arrives on another thread; run it in the context of the agent call.
        try:
            return base_context.copy().run(_run_tool, run, call_id, tool_map, name, arguments)
        except AgentStopped:
            return "Error: the run was stopped.", True

    def on_text(chunk: str):
        run.control.checkpoint(call_id)
        run.bus.publish("token", id=call_id, text=chunk)

    class Hooks:
        @staticmethod
        def turn_started():
            counters["turns"] += 1
            run.control.checkpoint(call_id)
            run.check_budget(agent_name, agent.budget)
            run.bus.publish("turn_started", id=call_id, turn=counters["turns"])

        @staticmethod
        def turn_finished(input_tokens, output_tokens, stop_reason):
            cost = run.spend(agent_name, model, input_tokens, output_tokens, subscription=subscription)
            counters["input_tokens"] += input_tokens
            counters["output_tokens"] += output_tokens
            counters["cost"] += cost
            run.bus.publish("turn_finished", id=call_id, turn=counters["turns"], input_tokens=input_tokens,
                            output_tokens=output_tokens, cost=cost, stop_reason=stop_reason,
                            total_cost=run.total_cost, subscription=subscription)

    run.control.checkpoint(call_id)
    run.check_budget(agent_name, agent.budget)
    server = ToolServer(tools, run_tool) if tools else None
    request = Request(model=model, system=system, messages=messages, tools=[t.definition() for t in tools],
                      schema=schema, strict_schema=strict, temperature=agent.temperature, seed=agent.seed,
                      effort=agent.effort, max_tokens=agent.max_tokens)
    session = None
    finished = False
    try:
        session = provider.open_session(request, server, agent.max_turns)
        text = user if not history else (
            "Our conversation so far:\n\n" + _history_text(history) + "\n\nNew request:\n\n" + user)
        format_attempts = 0
        while True:
            response = session.send(text, on_text, Hooks)
            messages.extend(response.raw or [])
            if not response.raw or response.raw[-1].get("content") != response.text:
                messages.append({"role": "assistant", "content": response.text, "tool_calls": []})
            pending = run.control.take_messages(call_id)
            if pending:
                text = "\n\n".join(interjection(m) for m in pending)
                messages.append({"role": "user", "content": text})
                continue
            try:
                result = parse_output(returns, response.text)
            except SchemaError as e:
                format_attempts += 1
                run.bus.publish("format_error", id=call_id, errors=e.errors, attempt=format_attempts,
                                max_attempts=agent.retries, text=response.text[:2000])
                if format_attempts > agent.retries:
                    raise SchemaError(e.errors, _mismatch_message(agent_name, method, returns, agent.retries,
                                                                  e.errors)) from None
                text = format_error_message(e.errors)
                messages.append({"role": "user", "content": text})
                continue
            finished = True
            return result, {"messages": messages, "turns": counters["turns"], "input_tokens": counters["input_tokens"],
                            "output_tokens": counters["output_tokens"], "cost": counters["cost"],
                            "raw_text": response.text}
    finally:
        if session is not None:
            if not finished:
                session.stop()
            session.close()
        if server is not None:
            server.close()
