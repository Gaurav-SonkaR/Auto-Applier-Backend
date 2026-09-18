from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from enum import Enum
from typing import Any

import httpx
import ollama as ollama_sdk
from groq import AsyncGroq, RateLimitError

from backend.config import get_settings

logger = logging.getLogger("llm")

# ── Task → model routing table ─────────────────────────────────────────────────
class LLMTask(str, Enum):
    JD_PARSING = "jd_parsing"
    RESUME_GENERATION = "resume_gen"
    QA_ANSWER = "qa_answer"
    CLASSIFICATION = "classification"


_TASK_PROVIDER: dict[LLMTask, str] = {
    LLMTask.JD_PARSING: "groq",
    LLMTask.RESUME_GENERATION: "groq",
    LLMTask.QA_ANSWER: "ollama_light",
    LLMTask.CLASSIFICATION: "ollama_classify",
}

# External ATS domains — used by scraper / apply agents
EXTERNAL_ATS_DOMAINS = frozenset(
    ["greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com",
     "taleo.net", "icims.com", "smartrecruiters.com", "ashbyhq.com"]
)


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
    Routes LLM calls to Groq (heavy) / Ollama (lightweight) / HuggingFace (fallback).

    RAM strategy for 8GB machines:
    - Only one Ollama model loaded at a time.
    - Groq API calls use zero local RAM.
    - Models are unloaded from Ollama before loading another.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._groq = AsyncGroq(api_key=self._settings.groq_api_key)
        self._rate_window = RateLimitWindow()
        self._loaded_ollama_model: str | None = None
        self._ollama_lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def complete(
        self,
        task: LLMTask,
        prompt: str,
        system: str = "",
        json_mode: bool = False,
    ) -> str:
        provider = _TASK_PROVIDER[task]
        t0 = time.monotonic()
        try:
            if provider == "groq":
                result = await self._groq_complete(prompt, system, json_mode)
            elif provider == "ollama_light":
                result = await self._ollama_complete(
                    prompt, system, self._settings.ollama_model_light
                )
            elif provider == "ollama_classify":
                result = await self._ollama_complete(
                    prompt, system, self._settings.ollama_model_classify
                )
            else:
                result = await self._groq_complete(prompt, system, json_mode)
        except Exception as exc:
            logger.error("LLM call failed for task=%s: %s", task.value, exc)
            raise
        elapsed = time.monotonic() - t0
        logger.info("LLM task=%s provider=%s elapsed=%.2fs", task.value, provider, elapsed)
        return result

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

            except RateLimitError:
                backoff = 2 ** attempt
                logger.warning("Groq 429 — attempt %d, backing off %ds", attempt + 1, backoff)
                await asyncio.sleep(backoff)
                if attempt == 3:
                    logger.warning("Groq exhausted — falling back to HuggingFace")
                    return await self._hf_complete(prompt, system)

        return await self._hf_complete(prompt, system)

    # ── HuggingFace fallback ───────────────────────────────────────────────────

    async def _hf_complete(self, prompt: str, system: str) -> str:
        token = self._settings.hf_api_token
        if not token:
            raise RuntimeError(
                "Groq rate-limited and HF_API_TOKEN not set — cannot complete LLM call."
            )
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

    # ── Ollama ─────────────────────────────────────────────────────────────────

    async def _ollama_complete(self, prompt: str, system: str, model: str) -> str:
        async with self._ollama_lock:
            await self._ensure_model_loaded(model)
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: ollama_sdk.chat(
                    model=model,
                    messages=messages,
                    options={"temperature": 0.2},
                ),
            )
            return response["message"]["content"]

    async def _ensure_model_loaded(self, model: str) -> None:
        if self._loaded_ollama_model == model:
            return
        if self._loaded_ollama_model and self._loaded_ollama_model != model:
            # Unload current model to free RAM before loading next one
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: ollama_sdk.generate(
                        model=self._loaded_ollama_model,
                        prompt="",
                        keep_alive=0,  # 0 = unload immediately
                    ),
                )
                logger.info("Unloaded Ollama model: %s", self._loaded_ollama_model)
            except Exception as exc:
                logger.warning("Could not unload model %s: %s", self._loaded_ollama_model, exc)
        self._loaded_ollama_model = model
        logger.info("Ollama model active: %s", model)

    async def embed(self, text: str) -> list[float]:
        model = self._settings.qa_embedding_model
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: ollama_sdk.embeddings(model=model, prompt=text),
        )
        return response["embedding"]

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
