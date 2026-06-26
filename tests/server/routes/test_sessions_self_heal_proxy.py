"""Self-heal wiring for the shared runner-proxy helpers (BDP-2579).

The eager-open read paths (``list_session_resources`` + stream) already
self-heal a dead runner. These tests cover the ~17 runner-backed resource
endpoints that route through ``_proxy_get_to_runner`` /
``_proxy_post_to_runner`` instead — exercised here via
``GET /resources/terminals``:

* a dead runner triggers the heal pipeline (single choke point), and on
  heal-success the proxied call retries and returns 200; and
* on heal-failure the request returns a clean ``RUNNER_UNAVAILABLE`` (503),
  never an unhandled 500 / raw traceback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import _globals, set_runner_client, set_runner_router
from omnigent.server.routes import sessions as S
from omnigent.server.routes.sessions import create_sessions_router

pytestmark = pytest.mark.asyncio


class _ConvStore:
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return Conversation(
            id=conversation_id,
            created_at=1,
            updated_at=1,
            root_conversation_id=conversation_id,
            agent_id="ag_test",
            runner_id="runner_dead",
            host_id="host_a",
        )


class _StubAgentStore:
    def get(self, _agent_id: str) -> None:
        return None


class _LiveClient:
    """Healthy runner client returning a valid (empty) terminals page."""

    def __init__(self) -> None:
        self.get_calls = 0

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, timeout: float | None = None
    ) -> httpx.Response:
        del params, timeout
        self.get_calls += 1
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [],
                "first_id": None,
                "last_id": None,
                "has_more": False,
            },
            request=httpx.Request("GET", url),
        )


class _RoutedRunner:
    def __init__(self, client: _LiveClient) -> None:
        self.runner_id = "runner_new"
        self.client = client


class _HealingRouter:
    """Resolve raises RUNNER_UNAVAILABLE while ``dead``; live once flipped."""

    def __init__(self, live_client: _LiveClient) -> None:
        self.live_client = live_client
        self.dead = True
        self.resolve_calls = 0

    async def aclient_for_session_resources(self, session_id: str) -> _RoutedRunner:
        del session_id
        self.resolve_calls += 1
        if self.dead:
            raise OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE)
        return _RoutedRunner(self.live_client)


@pytest.fixture
def runner_globals_reset() -> Iterator[None]:
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    set_runner_client(None)
    set_runner_router(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)


@pytest.fixture
def app(runner_globals_reset: None) -> FastAPI:
    del runner_globals_reset
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(_ConvStore(), _StubAgentStore()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


async def test_proxy_get_heals_and_retries_on_dead_runner(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _LiveClient()
    router = _HealingRouter(live)
    set_runner_router(router)  # type: ignore[arg-type]

    heal_calls: list[str] = []

    async def _fake_heal(session_id: str, _request: Request) -> bool:
        heal_calls.append(session_id)
        router.dead = False  # relaunch succeeded — runner is live again
        return True

    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)

    resp = await client.get("/v1/sessions/conv_heal/resources/terminals")

    assert resp.status_code == 200
    assert resp.json()["object"] == "list"
    assert heal_calls == ["conv_heal"]  # heal pipeline invoked exactly once
    assert live.get_calls == 1  # retried against the healed runner


async def test_proxy_get_returns_clean_runner_unavailable_when_heal_fails(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _HealingRouter(_LiveClient())
    set_runner_router(router)  # type: ignore[arg-type]

    heal_calls: list[str] = []

    async def _fake_heal(session_id: str, _request: Request) -> bool:
        heal_calls.append(session_id)
        return False  # heal exhausted every rung; graceful reconnecting state

    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)

    resp = await client.get("/v1/sessions/conv_heal/resources/terminals")

    # Clean RUNNER_UNAVAILABLE (503) — NOT a 500 / unhandled OmnigentError.
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == ErrorCode.RUNNER_UNAVAILABLE
    assert heal_calls == ["conv_heal"]
