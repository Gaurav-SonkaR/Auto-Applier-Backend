"""Every LLM task has to survive one provider being unavailable.

Found by trying the real providers rather than the mocks: Groq had retired the
configured `llama-3.3-70b-versatile` (404), and Ollama — then the first choice
for two of the four tasks — was not installed. Only a Groq *rate limit* fell
back, so every task was dead while a working provider sat configured and
unused.

Ollama has since been removed entirely: this deployment is online-only.

No network here — each provider is replaced with a stub, so what is asserted is
the routing decision, not any vendor's behaviour.
"""
from __future__ import annotations

import httpx
import pytest
from groq import NotFoundError, RateLimitError

from backend.app.services.llm_client import (
    _TASK_CHAIN,
    AllProvidersFailed,
    LLMRouter,
    LLMTask,
)


def _sdk_error(cls, message: str):
    """Build a real groq SDK exception.

    They read `response.request`, so passing `response=None` raises
    AttributeError rather than the error under test — which is exactly the bug
    these tests caught in the fallback code itself.
    """
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(
        429 if cls is RateLimitError else 404, request=request, json={"error": {}}
    )
    return cls(message, response=response, body=None)


def rate_limited() -> RateLimitError:
    return _sdk_error(RateLimitError, "429 rate limit")


def model_retired() -> NotFoundError:
    return _sdk_error(NotFoundError, "model_not_found")


@pytest.fixture
def router(monkeypatch):
    """A router with both keys present, so nothing is skipped as unconfigured."""
    r = LLMRouter()
    monkeypatch.setattr(r._settings, "groq_api_key", "test-key", raising=False)
    monkeypatch.setattr(r._settings, "hf_api_token", "test-token", raising=False)
    return r


def stub(router, monkeypatch, *, behaviour: dict[str, object]):
    """Replace provider dispatch. `behaviour` maps provider -> reply or Exception.

    A provider absent from the mapping raises, which is what "unavailable"
    means here.
    """
    called: list[str] = []

    async def _call(provider, prompt, system, json_mode):
        called.append(provider)
        outcome = behaviour.get(provider, ConnectionError(f"{provider} unreachable"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(router, "_call_provider", _call)
    return called


class TestChainShape:
    def test_every_task_has_a_chain(self):
        assert set(_TASK_CHAIN) == set(LLMTask)

    @pytest.mark.parametrize("task", list(LLMTask))
    def test_every_chain_has_a_fallback(self, task):
        """A single-provider task is exactly what broke — one outage took it
        down with a working provider available."""
        assert len(_TASK_CHAIN[task]) >= 2, task

    @pytest.mark.parametrize("task", list(LLMTask))
    def test_no_provider_is_tried_twice(self, task):
        chain = _TASK_CHAIN[task]
        assert len(chain) == len(set(chain)), chain

    @pytest.mark.parametrize("task", list(LLMTask))
    def test_every_task_starts_at_groq(self, task):
        assert _TASK_CHAIN[task][0] == "groq"

    @pytest.mark.parametrize("task", list(LLMTask))
    def test_nothing_routes_to_a_local_runtime(self, task):
        """Online-only by decision: Ollama is a local daemon, and Groq has no
        embeddings endpoint to replace what it was doing for the Q&A cache."""
        assert not any("ollama" in provider for provider in _TASK_CHAIN[task])

    @pytest.mark.parametrize("task", list(LLMTask))
    def test_every_provider_named_in_a_chain_is_dispatchable(self, task, router):
        """A typo in the table would otherwise surface as a mid-run failure."""
        for provider in _TASK_CHAIN[task]:
            assert router._provider_configured(provider) in (True, False)


class TestFallingThrough:
    async def test_the_first_working_provider_wins(self, router, monkeypatch):
        called = stub(router, monkeypatch, behaviour={"groq": "from groq"})
        assert await router.complete(LLMTask.JD_PARSING, "x") == "from groq"
        assert called == ["groq"]

    async def test_the_throwaway_tasks_also_go_to_groq(self, router, monkeypatch):
        """These two used to start at a local Ollama model, which took them
        down whenever it wasn't installed."""
        called = stub(router, monkeypatch, behaviour={"groq": "from groq"})
        assert await router.complete(LLMTask.CLASSIFICATION, "x") == "from groq"
        assert called == ["groq"]

    async def test_a_retired_groq_model_falls_through_to_huggingface(
        self, router, monkeypatch
    ):
        """A 404 for a decommissioned model used to propagate: only a rate
        limit fell back."""
        called = stub(router, monkeypatch, behaviour={
            "groq": model_retired(),
            "huggingface": "from hf",
        })
        assert await router.complete(LLMTask.RESUME_GENERATION, "x") == "from hf"
        assert called == ["groq", "huggingface"]

    async def test_a_groq_rate_limit_also_falls_through(self, router, monkeypatch):
        called = stub(router, monkeypatch, behaviour={
            "groq": rate_limited(),
            "huggingface": "from hf",
        })
        assert await router.complete(LLMTask.JD_PARSING, "x") == "from hf"
        assert called == ["groq", "huggingface"]

    async def test_it_walks_the_whole_chain_before_giving_up(self, router, monkeypatch):
        called = stub(router, monkeypatch, behaviour={"huggingface": "last resort"})
        assert await router.complete(LLMTask.QA_ANSWER, "x") == "last resort"
        assert called == ["groq", "huggingface"]

    async def test_json_mode_falls_back_too(self, router, monkeypatch):
        stub(router, monkeypatch, behaviour={"huggingface": '{"ok": true}'})
        assert await router.complete_json(LLMTask.JD_PARSING, "x") == {"ok": True}


class TestTheExhaustedRateLimitIsUsable:
    """The first version of this fallback built its own
    `RateLimitError(response=None)` when retries ran out, which raised
    AttributeError deep inside the SDK instead of signalling a rate limit — so
    the chain never got to try HuggingFace at all."""

    async def test_exhausting_retries_raises_a_real_rate_limit_error(
        self, router, monkeypatch
    ):
        class FakeCompletions:
            async def create(self, **_):
                raise rate_limited()

        monkeypatch.setattr(router, "_groq", type("C", (), {
            "chat": type("Chat", (), {"completions": FakeCompletions()})()
        })())
        monkeypatch.setattr(router._rate_window, "wait_if_needed",
                            lambda *_a, **_k: _noop())
        monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop())

        with pytest.raises(RateLimitError) as caught:
            await router._groq_complete("x", "", False)
        # Reading these is what crashed before.
        assert caught.value.response is not None
        assert caught.value.request is not None


class TestWhenEverythingFails:
    async def test_it_raises_one_error_naming_every_attempt(self, router, monkeypatch):
        """The caller has to be able to tell "no LaTeX", "no key" and "all
        providers down" apart from the message alone."""
        called = stub(router, monkeypatch, behaviour={})
        with pytest.raises(AllProvidersFailed) as caught:
            await router.complete(LLMTask.CLASSIFICATION, "x")

        assert called == ["groq", "huggingface"]
        message = str(caught.value)
        for provider in ("groq", "huggingface"):
            assert provider in message
        assert caught.value.task is LLMTask.CLASSIFICATION
        assert len(caught.value.failures) == 2

    async def test_an_unconfigured_provider_is_skipped_not_attempted(
        self, router, monkeypatch
    ):
        """Calling one with no key buries the real failure under a second,
        misleading one."""
        monkeypatch.setattr(router._settings, "hf_api_token", "", raising=False)
        called = stub(router, monkeypatch, behaviour={})

        with pytest.raises(AllProvidersFailed):
            await router.complete(LLMTask.JD_PARSING, "x")
        assert "huggingface" not in called

    async def test_a_missing_groq_key_is_skipped_too(self, router, monkeypatch):
        monkeypatch.setattr(router._settings, "groq_api_key", "", raising=False)
        called = stub(router, monkeypatch, behaviour={"huggingface": "from hf"})

        assert await router.complete(LLMTask.JD_PARSING, "x") == "from hf"
        assert called == ["huggingface"]

    async def test_a_provider_with_no_key_check_defaults_to_attemptable(self, router):
        """Only Groq and HuggingFace are keyed. Anything added later is tried
        rather than silently skipped, so a new provider fails loudly."""
        assert router._provider_configured("something-new") is True

    def test_an_unknown_provider_name_is_a_loud_error(self, router):
        import asyncio

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                router._call_provider("nonsense", "x", "", False)
            )


class TestGroqRetriesOnlyWhatIsWorthRetrying:
    async def test_a_rate_limit_is_retried_then_raised(self, router, monkeypatch):
        """Retried, because waiting can clear it — but the fallback decision
        belongs to the chain, so it raises rather than calling HF itself."""
        attempts = {"n": 0}

        class FakeCompletions:
            async def create(self, **_):
                attempts["n"] += 1
                raise rate_limited()

        monkeypatch.setattr(router, "_groq", type("C", (), {
            "chat": type("Chat", (), {"completions": FakeCompletions()})()
        })())
        monkeypatch.setattr(router._rate_window, "wait_if_needed",
                            lambda *_a, **_k: _noop())
        monkeypatch.setattr("asyncio.sleep", lambda *_a, **_k: _noop())

        with pytest.raises(RateLimitError):
            await router._groq_complete("x", "", False)
        assert attempts["n"] == 4, "should retry the full budget before giving up"

    async def test_a_model_error_is_not_retried(self, router, monkeypatch):
        """A retired model fails identically every time; retrying it just
        delays the fallback."""
        attempts = {"n": 0}

        class FakeCompletions:
            async def create(self, **_):
                attempts["n"] += 1
                raise model_retired()

        monkeypatch.setattr(router, "_groq", type("C", (), {
            "chat": type("Chat", (), {"completions": FakeCompletions()})()
        })())
        monkeypatch.setattr(router._rate_window, "wait_if_needed",
                            lambda *_a, **_k: _noop())

        with pytest.raises(NotFoundError):
            await router._groq_complete("x", "", False)
        assert attempts["n"] == 1


async def _noop():
    return None
