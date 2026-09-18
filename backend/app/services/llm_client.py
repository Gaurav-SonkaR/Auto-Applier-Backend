from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from enum import Enum
from typing import Any

import httpx
from groq import AsyncGroq, RateLimitError

from backend.app.config import get_settings

logger = logging.getLogger("llm")

# ── Task → model routing table ─────────────────────────────────────────────────
class LLMTask(str, Enum):
    JD_PARSING = "jd_parsing"
    RESUME_GENERATION = "resume_gen"
    QA_ANSWER = "qa_answer"
    CLASSIFICATION = "classification"


# Providers to try, in order, per task.
#
# Groq for everything, HuggingFace behind it. Ollama was the first choice for
# the two throwaway tasks and has been removed: it is a local daemon, and this
# deployment is online-only by decision. It also can't be replaced by Groq for
# embeddings, which is why the Q&A cache no longer uses any.
#
# The second entry is not decoration. Groq retires free-tier models routinely
# — llama-3.3-70b-versatile began returning 404 mid-project — and before a
# fallback existed that took every task down with it.
_TASK_CHAIN: dict[LLMTask, tuple[str, ...]] = {
    LLMTask.JD_PARSING: ("groq", "huggingface"),
    LLMTask.RESUME_GENERATION: ("groq", "huggingface"),
    LLMTask.QA_ANSWER: ("groq", "huggingface"),
    LLMTask.CLASSIFICATION: ("groq", "huggingface"),
}


class AllProvidersFailed(RuntimeError):
    """Every provider for a task failed. Carries what was tried and why."""

    def __init__(self, task: "LLMTask", failures: list[tuple[str, Exception]]) -> None:
        detail = "; ".join(
            f"{name}: {type(exc).__name__}: {str(exc)[:120]}" for name, exc in failures
        )
        super().__init__(f"All LLM providers failed for {task.value} — {detail}")
        self.task = task
        self.failures = failures

class RateLimitWindow:
    """Rolling 60-second token counter for Groq free tier (14,400 tokens/min)."""

    WINDOW_SECONDS = 60
    MAX_TOKENS = 14_000  # stay slightly under the 14,400 limit

    def __init__(self) -> None:
        self._events: deque[tuple[float, int]] = deque()

    def record(self, tokens: int) -> None:
        self._events.append((time.monotonic(), tokens))

    def tokens_in_window(self) -> int:
        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        return sum(t for _, t in self._events)

    def would_exceed(self, estimated_tokens: int) -> bool:
        return self.tokens_in_window() + estimated_tokens > self.MAX_TOKENS

    async def wait_if_needed(self, estimated_tokens: int) -> None:
        while self.would_exceed(estimated_tokens):
            oldest_ts = self._events[0][0] if self._events else time.monotonic()
            wait = max(0.5, self.WINDOW_SECONDS - (time.monotonic() - oldest_ts) + 1)
            logger.info("Groq rate limit approaching — waiting %.1fs", wait)
            await asyncio.sleep(wait)


class LLMRouter:
    """
    Routes every LLM call to Groq, with HuggingFace behind it.

    Nothing runs locally: no model is downloaded, loaded or unloaded, and local
    RAM is not a consideration. The RAM-juggling notes that used to live here
    described the Ollama setup, which has been removed.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._groq = AsyncGroq(api_key=self._settings.groq_api_key)
        self._rate_window = RateLimitWindow()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def complete(
        self,
        task: LLMTask,
        prompt: str,
        system: str = "",
        json_mode: bool = False,
    ) -> str:
        failures: list[tuple[str, Exception]] = []

        for provider in _TASK_CHAIN[task]:
            if not self._provider_configured(provider):
                logger.debug("Skipping %s for task=%s — not configured", provider, task.value)
                continue

            t0 = time.monotonic()
            try:
                result = await self._call_provider(provider, prompt, system, json_mode)
            except Exception as exc:
                failures.append((provider, exc))
                logger.warning(
                    "LLM provider %s failed for task=%s (%s) — %s",
                    provider, task.value, type(exc).__name__,
                    "trying the next provider" if provider != _TASK_CHAIN[task][-1]
                    else "no providers left",
                )
                continue

            elapsed = time.monotonic() - t0
            if failures:
                logger.info(
                    "LLM task=%s recovered on provider=%s after %d failure(s)",
                    task.value, provider, len(failures),
                )
            logger.info("LLM task=%s provider=%s elapsed=%.2fs", task.value, provider, elapsed)
            return result

        raise AllProvidersFailed(task, failures)

    async def _call_provider(
        self, provider: str, prompt: str, system: str, json_mode: bool
    ) -> str:
        if provider == "groq":
            return await self._groq_complete(prompt, system, json_mode)
        if provider == "huggingface":
            return await self._hf_complete(prompt, system)
        raise ValueError(f"Unknown LLM provider: {provider}")

    def _provider_configured(self, provider: str) -> bool:
        """Skip a provider that cannot possibly work.

        Trying an unconfigured one only buries the real failure under a second
        one, and the error the caller sees should name what was actually wrong.
        """
        if provider == "groq":
            return bool(self._settings.groq_api_key)
        if provider == "huggingface":
            return bool(self._settings.hf_api_token)
        return True

    async def complete_json(self, task: LLMTask, prompt: str, system: str = "") -> dict[str, Any]:
        raw = await self.complete(task, prompt, system=system, json_mode=True)
        return self._parse_json(raw)

    # ── Groq ───────────────────────────────────────────────────────────────────

    async def _groq_complete(self, prompt: str, system: str, json_mode: bool) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        estimated_tokens = len(prompt.split()) * 2
        await self._rate_window.wait_if_needed(estimated_tokens)

        last_rate_limit: RateLimitError | None = None
        for attempt in range(4):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._settings.groq_model_heavy,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 4096,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                response = await self._groq.chat.completions.create(**kwargs)
                content = response.choices[0].message.content or ""
                usage = response.usage
                if usage:
                    self._rate_window.record(usage.total_tokens)
                    logger.debug(
                        "Groq tokens: prompt=%d completion=%d total=%d",
                        usage.prompt_tokens,
                        usage.completion_tokens,
                        usage.total_tokens,
                    )
                return content

            except RateLimitError as exc:
                # Only a rate limit is worth waiting out. A 404 for a retired
                # model, or a bad key, will fail identically every time — those
                # propagate immediately so the chain can move on.
                last_rate_limit = exc
                backoff = 2 ** attempt
                logger.warning("Groq 429 — attempt %d, backing off %ds", attempt + 1, backoff)
                await asyncio.sleep(backoff)

        # Retries exhausted. Raising rather than calling HuggingFace here keeps
        # fallback in one place: `complete()` walks the chain.
        #
        # Re-raising the SDK's own exception rather than building one: these
        # carry the originating request, so a hand-made
        # `RateLimitError(response=None)` raises AttributeError instead of
        # signalling a rate limit at all.
        raise last_rate_limit or RuntimeError("Groq made no attempt")

    # ── HuggingFace fallback ───────────────────────────────────────────────────

    async def _hf_complete(self, prompt: str, system: str) -> str:
        token = self._settings.hf_api_token
        if not token:
            raise RuntimeError("HF_API_TOKEN is not set")
        model_id = "mistralai/Mistral-7B-Instruct-v0.3"
        url = f"https://api-inference.huggingface.co/models/{model_id}"
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"inputs": full_prompt, "parameters": {"max_new_tokens": 2048}},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0].get("generated_text", "")
            return str(data)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM returned invalid JSON: {exc}\nRaw: {raw[:500]}") from exc


# Module-level singleton (created lazily)
_router: LLMRouter | None = None


def get_llm_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router
