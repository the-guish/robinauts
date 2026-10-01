# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The ``AgentEngine`` port driven end to end, in one process, with no provider.

Run from the repository root::

    uv run --project backend examples/agent_engines/standalone.py

The same conversation as ``main.py`` of `agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_ -- the warmer of
three cities, then the coolest, which can only be answered from memory --
but through ``robinauts.ports.agent_engine.AgentEngine``, the way the
platform would drive it:

1. ``create`` the conversation in the engine, on purpose;
2. ``stream`` the first prompt with no checkpoint, print the events as they
   arrive, and keep the ``checkpoint_id`` that ``Done`` carries on the
   answer, the way the transcript does;
3. ``stream`` the second prompt **from that checkpoint**, which is where the
   memory comes from: the engine is told where to continue, never asked to
   remember;
4. ``fork`` a new conversation at the first answer's checkpoint and ask it
   the second question too: it answers from the memory as it was then, and
   the two conversations are independent;
5. show the refusals the port promises, and ``forget`` both.

**The engine is a stand-in, and the mechanics are real.** It keeps its
conversations in LangGraph's own checkpointer (in memory here, Postgres in
a deployment), so the checkpoint ids are LangGraph's as they are, continuing
from one is the framework's own time travel, and a fork is a copy of the
source's checkpoints under the target's thread. What is scripted is the
model: a function that asks for the weather tool for each city named and
answers from the results in its history, so that nothing here needs a key
or the network. A real adapter replaces that one node with the vendor's
model and the agent's MCP tools, and keeps everything else.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from robinauts.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    ConversationNotFoundError,
    Done,
    Engine,
    Event,
    InvalidValueError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.ports.agent_engine import AgentEngine

# --- the tool, as the examples have it ----------------------------------------------------------

_WEATHER = {
    "montevideo": "18°C, partly cloudy",
    "tampa": "21°C, sunny",
    "madrid": "10°C, clear",
}


def get_weather(city: str) -> str:
    """Current weather for a city.

    Args:
        city: Name of the city, e.g. "Montevideo".
    """
    return _WEATHER.get(city.strip().lower(), f"No weather data for {city}.")


# --- the scripted model: one graph node that stands in for the vendor's -----------------------

CITIES = ("Tampa", "Madrid", "Montevideo")


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _temperatures(messages: Iterable[BaseMessage]) -> dict[str, int]:
    """What the history already knows: each city a tool answered for, and its degrees."""
    city_of_call: dict[str, str] = {}
    known: dict[str, int] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                city_of_call[call["id"] or ""] = str(call["args"].get("city", ""))
        elif isinstance(message, ToolMessage):
            found = re.search(r"(-?\d+)°C", str(message.content))
            if found and message.tool_call_id in city_of_call:
                known[city_of_call[message.tool_call_id]] = int(found.group(1))
    return known


def model(state: State, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
    """Ask for the weather of every city named that the history has no answer for; then answer.

    The system prompt reaches it through the run's config, which is how a
    real adapter hands the definition to the framework at every call and
    never stores it: here it decides nothing but the case of the answer.
    """
    messages = state["messages"]
    question = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
    known = _temperatures(messages)
    wanted = [city for city in CITIES if city.lower() in str(question.content).lower()]
    missing = [city for city in wanted if city not in known]
    if missing and messages[-1] is question:
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"call_{city.lower()}",
                            "name": "get_weather",
                            "args": {"city": city},
                        }
                        for city in missing
                    ],
                )
            ]
        }
    if not known:
        answer = "I have no weather to compare."
    elif "cool" in str(question.content).lower():
        answer = min(known, key=known.__getitem__)
    else:
        answer = max(known, key=known.__getitem__)
    if "upper case" in str(config["configurable"].get("system_prompt", "")).lower():
        answer = answer.upper()
    return {"messages": [AIMessage(content=answer)]}


def tools(state: State) -> dict[str, list[BaseMessage]]:
    """Run each call the model made; the framework's tool node, in three lines."""
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    return {
        "messages": [
            ToolMessage(
                content=get_weather(**call["args"]),
                tool_call_id=call["id"] or "",
                name=call["name"],
            )
            for call in last.tool_calls
        ]
    }


def _route(state: State) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


def build_graph(saver: InMemorySaver) -> Any:
    graph = StateGraph(State)
    graph.add_node("model", model)
    graph.add_node("tools", tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", _route)
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=saver)


# --- the engine ----------------------------------------------------------------------------------


class StandInEngine(AgentEngine):
    """An ``AgentEngine`` over LangGraph's checkpointer, with the scripted model above.

    One thread per conversation, named by the conversation id. A conversation
    exists from ``create`` until ``forget``, which the engine notes for
    itself, since a LangGraph thread has no existence apart from its
    checkpoints and a created conversation has none yet.
    """

    def __init__(self) -> None:
        self.saver = InMemorySaver()
        self.graph = build_graph(self.saver)
        self.conversations: set[uuid.UUID] = set()

    async def create(self, conversation_id: uuid.UUID) -> None:
        if conversation_id in self.conversations:
            raise InvalidValueError(f"conversation {conversation_id} is already created here")
        self.conversations.add(conversation_id)

    async def exists(self, conversation_id: uuid.UUID) -> bool:
        return conversation_id in self.conversations

    def stream(
        self,
        conversation_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        checkpoint_id: str | None,
    ) -> AsyncGenerator[Event, None]:
        return self._turn(conversation_id, agent, prompt, model, checkpoint_id)

    async def _turn(
        self,
        conversation_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        model: str,
        checkpoint_id: str | None,
    ) -> AsyncGenerator[Event, None]:
        # Nothing before this line runs until the stream is iterated: a refusal is a failure
        # of the turn, raised where the caller is reading, as the port asks.
        if conversation_id not in self.conversations:
            raise ConversationNotFoundError(f"no conversation {conversation_id} in this engine")
        thread: RunnableConfig = {"configurable": {"thread_id": str(conversation_id)}}
        if checkpoint_id is not None:
            await self._checkpoint(thread, checkpoint_id)
            thread = {"configurable": {**thread["configurable"], "checkpoint_id": checkpoint_id}}
        # The definition rides with the call and is stored nowhere; `model` is the run's.
        config: RunnableConfig = {
            "configurable": {**thread["configurable"], "system_prompt": agent.system_prompt},
            "metadata": {"model": model},
        }
        answer = ""
        async for update in self.graph.astream(
            {"messages": [HumanMessage(prompt)]}, config, stream_mode="updates"
        ):
            for _node, payload in update.items():
                for message in payload.get("messages", []):
                    if isinstance(message, AIMessage) and message.tool_calls:
                        for call in message.tool_calls:
                            yield ToolCall(
                                call_id=call["id"] or "",
                                name=call["name"],
                                arguments=dict(call["args"]),
                            )
                    elif isinstance(message, AIMessage):
                        answer = str(message.content)
                        yield TextDelta(text=answer)
                    elif isinstance(message, ToolMessage):
                        yield ToolResult(
                            call_id=message.tool_call_id,
                            name=message.name or "",
                            output=str(message.content),
                        )
        # The checkpoint the turn ended on: the thread's latest, with nothing pending. The
        # intra-turn ones exist too; a real adapter prunes them, the platform never sees them.
        ended = await self.graph.aget_state({"configurable": {"thread_id": str(conversation_id)}})
        assert not ended.next
        yield Done(text=answer, checkpoint_id=ended.config["configurable"]["checkpoint_id"])

    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        if source_id not in self.conversations:
            raise ConversationNotFoundError(f"no conversation {source_id} in this engine")
        if target_id in self.conversations:
            raise InvalidValueError(f"conversation {target_id} is already created here")
        source: RunnableConfig = {"configurable": {"thread_id": str(source_id)}}
        await self._checkpoint(source, checkpoint_id)
        # Every checkpoint of the source up to the one forked at, under the target's thread and
        # the same ids: the ids on the copied answers stay valid in the fork. Ids are
        # time-ordered, so "up to" is a comparison.
        async for stored in self.saver.alist(source):
            if stored.checkpoint["id"] <= checkpoint_id:
                await self.saver.aput(
                    {"configurable": {"thread_id": str(target_id), "checkpoint_ns": ""}},
                    stored.checkpoint,
                    stored.metadata,
                    stored.checkpoint["channel_versions"],
                )
        self.conversations.add(target_id)

    async def forget(self, conversation_id: uuid.UUID) -> None:
        self.conversations.discard(conversation_id)
        await self.saver.adelete_thread(str(conversation_id))

    async def _checkpoint(self, thread: RunnableConfig, checkpoint_id: str) -> None:
        """Refuse a checkpoint this engine does not hold for that thread, or one mid-turn."""
        at = {"configurable": {**thread["configurable"], "checkpoint_id": checkpoint_id}}
        if await self.saver.aget_tuple(at) is None:
            raise CheckpointNotFoundError(f"no checkpoint {checkpoint_id} in that conversation")
        if (await self.graph.aget_state(at)).next:
            raise CheckpointNotFoundError(f"checkpoint {checkpoint_id} is not the end of a turn")


# --- the caller: what the platform does with the port ---------------------------------------

AGENT = AgentDefinition(
    id="weather",
    title="Weather",
    system_prompt="Answer in upper case.",
    model="scripted",
    engine=Engine.LANGGRAPH,
)
MODEL = "scripted"
FIRST = "What's the warmer city right now of Tampa, Madrid, Montevideo? Answer in one word"
SECOND = "What's the coolest? One word"


async def turn(
    engine: AgentEngine, conversation_id: uuid.UUID, prompt: str, *, after: str | None
) -> str | None:
    """One turn as the application runs it: stream, show, and keep the answer's checkpoint id."""
    print(f"\n> {prompt}")
    checkpoint_id = None
    async for event in engine.stream(
        conversation_id, AGENT, prompt, model=MODEL, checkpoint_id=after
    ):
        match event:
            case TextDelta(text=text):
                print(text, end="", flush=True)
            case ToolCall(call_id=call_id, name=name, arguments=arguments):
                print(f"  [call {call_id}] {name}({arguments})")
            case ToolResult(call_id=call_id, name=name, output=output):
                print(f"  [result {call_id}] {name} -> {output}")
            case Done(text=text, checkpoint_id=checkpoint_id):
                print(f"\n  [done] {text!r}; the answer is stored with checkpoint {checkpoint_id}")
    return checkpoint_id


async def expect(
    kind: type[Exception], events: AsyncIterator[Event] | None = None, **call: Any
) -> None:
    """Run a call the port says is refused, and show which refusal it was."""
    try:
        if events is not None:
            async for _ in events:
                pass
        else:
            await call["do"]
    except kind as refused:
        print(f"  refused as {kind.__name__}: {refused}")
    else:
        raise AssertionError(f"expected {kind.__name__}")


async def main() -> None:
    engine = StandInEngine()

    print("=== a conversation, created on purpose")
    conversation = uuid.uuid4()
    await engine.create(conversation)
    print(f"created {conversation}; exists: {await engine.exists(conversation)}")

    first = await turn(engine, conversation, FIRST, after=None)
    second = await turn(engine, conversation, SECOND, after=first)
    print(f"\nthe transcript now carries two checkpoint ids: {first} and {second}")

    print("\n=== a fork at the first answer, asked the second question")
    fork = uuid.uuid4()
    assert first is not None
    await engine.fork(conversation, fork, checkpoint_id=first)
    print(f"forked into {fork}; exists: {await engine.exists(fork)}")
    forked = await turn(engine, fork, SECOND, after=first)
    print(f"\nthe fork's own answer has a checkpoint of its own: {forked}")
    print(f"the source is untouched: continuing it from {second} still works")
    await turn(
        engine, conversation, "Which is the warmer, Madrid or Montevideo? One word", after=second
    )

    print("\n=== the refusals the port promises")
    stranger = uuid.uuid4()
    print("a turn of a conversation that was never created:")
    await expect(
        ConversationNotFoundError,
        engine.stream(stranger, AGENT, FIRST, model=MODEL, checkpoint_id=None),
    )
    print("a checkpoint of one conversation handed in for another:")
    await expect(
        CheckpointNotFoundError,
        engine.stream(fork, AGENT, FIRST, model=MODEL, checkpoint_id=second),
    )
    print("creating a conversation twice:")
    await expect(InvalidValueError, do=engine.create(conversation))

    print("\n=== forgotten")
    await engine.forget(conversation)
    await engine.forget(fork)
    await engine.forget(fork)  # idempotent
    print(f"exists after forget: {await engine.exists(conversation)}, {await engine.exists(fork)}")


if __name__ == "__main__":
    asyncio.run(main())
