"""Actual timeline producer and Companion consumer with isolated synthetic sources."""

import asyncio
import os

import pytest
from test_actor_memory import actor as actor
from test_actor_memory import actor_storage as actor_storage
from test_actor_memory import execute
from test_actor_timeline import seed, timeline

from tianshu_memory.domain import canonical


@pytest.mark.skipif(
    not os.environ.get("TIANSHU_COMPANION_REPO"), reason="Companion joint checkout not selected"
)
def test_budget_pages_close_through_the_actual_companion_consumer(actor, monkeypatch):
    monkeypatch.syspath_prepend(os.environ["TIANSHU_COMPANION_REPO"] + "/src")
    from tianshu_companion.actor_memory import ActorMemory
    from tianshu_companion.contracts import Contracts

    seed(actor, ["2026-10-26T01:00:00Z", "2026-10-26T02:00:00Z"])
    expected = timeline(actor)["items"]
    budget = len(canonical(expected[:1]).encode())
    contracts = Contracts(os.environ["TIANSHU_CONTRACT_DIRECTORY"])
    owner, operations = object.__new__(ActorMemory), []

    async def version(actor_id):
        return 1

    async def memory(body):
        operations.append(body["operation"])
        contracts.check("actor-self-memory#request", body)
        value = execute(actor, body)
        contracts.check("actor-self-memory#response", value)
        return value

    owner.role_version, owner._memory = version, memory

    async def run():
        options = dict(
            kinds=["episode"],
            limit=32,
            budget_bytes=budget,
            window=dict(start="2026-10-26T00:00:00Z", end="2026-10-27T00:00:00Z"),
        )
        context = dict(purpose="diary", audience=dict(kind="actor_only"))
        first = await owner._query("actor:a", context, None, **options)
        second = await owner._query(
            "actor:a", context, None, cursor=first["next_cursor"], **options
        )
        assert first["items"] + second["items"] == expected
        assert not first["complete"] and second["complete"]
        assert not first["omitted"] and not second["omitted"]
        assert operations == ["query", "check_memory", "query", "check_memory"]

    asyncio.run(run())
