import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx

from agent_runtime import CallOutcome, build_deps, run_call
from agent_runtime.api_client import ApiClient
from agent_runtime.config import FakesConfig, load_config

CALL_ID = '6f1c2b1e-0000-4000-8000-000000000001'

def fake_api(reported: list[dict[str, object]]) -> ApiClient:
    """The calls API in memory: one call, one tenant, and every reported result kept."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/context'):
            return httpx.Response(
                200,
                json={
                    'call_id': CALL_ID,
                    'tenant': {'id': 1},
                    'contact': {'name': 'Lucia Garcia', 'phone': '+34600000001'},
                    'script': [{'order': 1, 'text': 'Greet the contact.'}],
                    'history': [],
                },
            )
        if request.url.path == '/tenants/1/config':
            return httpx.Response(200, json={'language': 'es-ES', 'system_prompt': 'Be brief.'})
        reported.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'ok'})

    return ApiClient('http://api.test', client_timeout_s=1, transport=httpx.MockTransport(handle))


def config(**changes: Any) -> FakesConfig:
    calm: dict[str, Any] = {
        'llm_hang_rate': 0,
        'llm_error_rate': 0,
        'no_answer_rate': 0,
        'hangup_rate': 0,
    }
    return replace(load_config(), **{**calm, **changes})

async def test_a_call_runs_to_an_outcome_and_reports_it() -> None:
    config = replace(load_config(), llm_hang_rate=0, llm_error_rate=0, no_answer_rate=0)
    reported: list[dict[str, object]] = []

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/context'):
            return httpx.Response(
                200,
                json={
                    'call_id': CALL_ID,
                    'tenant': {'id': 1},
                    'contact': {'name': 'Lucia Garcia', 'phone': '+34600000001'},
                    'script': [{'order': 1, 'text': 'Greet the contact.'}],
                    'history': [],
                },
            )
        if request.url.path == '/tenants/1/config':
            return httpx.Response(200, json={'language': 'es-ES', 'system_prompt': 'Be brief.'})
        reported.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'ok'})

    client = ApiClient('http://api.test', client_timeout_s=1, transport=httpx.MockTransport(api))

    outcome = await run_call(CALL_ID, build_deps(config, api=client))

    assert outcome in {CallOutcome.COMPLETED, CallOutcome.USER_HANGUP}
    assert [r['outcome'] for r in reported] == [outcome]

async def test_an_unanswered_call_is_reported_as_no_answer() -> None:
    reported: list[dict[str, object]] = []

    outcome = await run_call(CALL_ID, build_deps(config(no_answer_rate=1), api=fake_api(reported)))

    assert outcome == CallOutcome.NO_ANSWER
    assert [r['outcome'] for r in reported] == ['no_answer']


async def test_a_stalled_llm_ends_the_call_as_a_timeout() -> None:
    reported: list[dict[str, object]] = []
    deps = build_deps(config(llm_hang_rate=1, call_timeout_s=10), api=fake_api(reported))

    # Not awaited directly: before the fix the call never ends, and the test would hang too.
    task = asyncio.create_task(run_call(CALL_ID, deps))
    done, _ = await asyncio.wait({task}, timeout=5)

    assert done, 'the call never finished'
    assert task.result() == CallOutcome.TIMEOUT
    assert [r['outcome'] for r in reported] == ['timeout']

async def test_a_failing_llm_ends_the_call_as_an_error() -> None:
    reported: list[dict[str, object]] = []

    outcome = await run_call(CALL_ID, build_deps(config(llm_error_rate=1), api=fake_api(reported)))

    assert outcome == CallOutcome.ERROR
    assert [r['outcome'] for r in reported] == ['error']

