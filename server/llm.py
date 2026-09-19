"""LLM providers for the narrative layer: configuration, transport, health checks.

Every provider speaks plain HTTP JSON through the standard library, keeping the
project's zero-new-dependencies posture. `configure_narration()` is the single
startup entry point: it reads the `OSR_WEB_NARRATOR*` environment, builds the
provider, and health-checks it once — any problem logs one warning and leaves
narration off, because a misconfigured narrator must never keep the game from
serving. Every path through it logs exactly one line, on or off, so the operator
never has to guess which one they got.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

_log = logging.getLogger("osr_web.narration")


class ProviderError(Exception):
    """The one exception the narration pipeline catches: any provider failure."""

    def __init__(self, message: str, status: int | None = None):
        """Carry `message` and, when the failure came from an HTTP response, its `status`."""
        super().__init__(message)
        self.status = status


class NarrativeProvider(Protocol):
    """One LLM backend. Implementations are stateless and thread-safe."""

    name: str

    def generate(self, system: str, prompt: str, *, timeout: float) -> str:
        """One completion. Raises ProviderError on any failure; never returns None."""
        ...

    def generate_stream(self, system: str, prompt: str, *, timeout: float) -> Iterator[str]:
        """The same completion as text deltas, in order. Raises ProviderError.

        Yielding nothing is a valid (empty) completion; the caller decides
        whether an empty result is usable. Any transport failure — including
        one mid-stream, after some deltas have already been yielded — raises
        [`ProviderError`][server.llm.ProviderError].
        """
        ...

    def health_check(self) -> str | None:
        """None when ready; otherwise a human-readable reason narration is disabled."""
        ...


class OllamaProvider:
    """Local models through Ollama's native chat API."""

    name = "ollama"
    default_base_url = "http://localhost:11434"

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None):
        """Target `model` at `base_url` (or the local default); `api_key` is unused."""
        self.model = model
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        # api_key is part of the uniform provider constructor; ollama has no auth.

    def generate(self, system: str, prompt: str, *, timeout: float) -> str:
        """Collect the streamed completion into one string; raises on an empty result."""
        content = "".join(self.generate_stream(system, prompt, timeout=timeout))
        if not content.strip():
            raise ProviderError("ollama returned an empty completion")
        return content

    def generate_stream(self, system: str, prompt: str, *, timeout: float) -> Iterator[str]:
        """Stream content deltas from Ollama's chat API, retrying once without `think`."""
        body = self._chat_body(system, prompt)
        # Reasoning models otherwise spend the whole num_predict budget in
        # message.thinking and return an empty content.
        body["think"] = False
        try:
            yield from self._stream_chat(body, timeout)
        except ProviderError as error:
            if error.status != 400:
                raise
            # Older Ollama rejects `think` for models without the capability.
            # The 400 lands on the request itself, before any delta, so
            # retrying can never double up already-yielded text.
            del body["think"]
            yield from self._stream_chat(body, timeout)

    def _chat_body(self, system: str, prompt: str) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "options": {"temperature": 0.8, "num_predict": 200},
            # Keep the model resident between beats: only the first passage of
            # a session pays the model-load cost.
            "keep_alive": "15m",
        }

    def _stream_chat(self, body: dict, timeout: float) -> Iterator[str]:
        """Yield `message.content` deltas from Ollama's NDJSON chat stream."""
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise ProviderError(f"{self.name}: {error}", status=error.code) from error
        except (OSError, ValueError) as error:
            raise ProviderError(f"{self.name}: {error}") from error
        with response:
            try:
                for raw_line in response:
                    line = raw_line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ProviderError(f"{self.name}: malformed stream line")
                    if payload.get("error"):
                        raise ProviderError(f"{self.name}: {payload['error']}")
                    delta = (payload.get("message") or {}).get("content")
                    if isinstance(delta, str) and delta:
                        yield delta
                    if payload.get("done"):
                        break
            except (OSError, ValueError) as error:
                # A drop or timeout mid-stream; the beat fails open upstream.
                raise ProviderError(f"{self.name}: {error}") from error

    def health_check(self) -> str | None:
        """None when `model` is present on the Ollama server; otherwise why it isn't."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as response:
                payload = json.load(response)
        except (OSError, ValueError) as error:
            return f"ollama unreachable at {self.base_url} ({error})"
        models = [entry.get("name", "") for entry in payload.get("models", [])]
        if self.model in models:
            return None
        # A configured name with no :tag matches any tag of that model.
        if ":" not in self.model and any(name.split(":", 1)[0] == self.model for name in models):
            return None
        return f"model {self.model!r} not found — try 'ollama pull {self.model}'"


_PROVIDERS: dict[str, type] = {
    "ollama": OllamaProvider,
    # later: "anthropic", "openai", "openai_compatible"
}


@dataclass(frozen=True)
class NarrationConfig:
    """The `OSR_WEB_NARRATOR*` environment, read once at startup."""

    provider_id: str
    model: str
    base_url: str | None
    api_key: str | None
    timeout: float


@dataclass(frozen=True)
class NarrationSetup:
    """A health-checked provider plus the per-generation timeout."""

    provider: NarrativeProvider
    timeout: float


def load_narration_config() -> NarrationConfig | None:
    """Read the narrator environment; None means the feature is off."""
    provider_id = os.environ.get("OSR_WEB_NARRATOR", "").strip()
    if not provider_id:
        return None
    raw_timeout = os.environ.get("OSR_WEB_NARRATOR_TIMEOUT", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else 30.0
    except ValueError:
        _log.warning("ignoring OSR_WEB_NARRATOR_TIMEOUT=%r; using 30", raw_timeout)
        timeout = 30.0
    return NarrationConfig(
        provider_id=provider_id,
        model=os.environ.get("OSR_WEB_NARRATOR_MODEL", "").strip(),
        base_url=os.environ.get("OSR_WEB_NARRATOR_URL", "").strip() or None,
        api_key=os.environ.get("OSR_WEB_NARRATOR_API_KEY", "").strip() or None,
        timeout=timeout,
    )


_DOTENV_NOTE = " (OSR_WEB_NARRATOR came from .env, not your environment; blank it to turn narration off)"
"""Suffix for the startup line when `.env`, not the shell, supplied the provider."""


def configure_narration(*, from_dotenv: bool = False) -> NarrationSetup | None:
    """Build and health-check the configured provider once, at startup.

    Args:
        from_dotenv: True when `.env` supplied `OSR_WEB_NARRATOR` and the process
            environment did not — only the caller that loaded the file can know
            that. It changes nothing about whether narration runs; it is named in
            the startup line, because a narrator the operator never asked for is
            the surprise worth explaining.

    Returns:
        The health-checked provider and its timeout, or None with one logged line
        explaining why narration is off.
    """
    source = _DOTENV_NOTE if from_dotenv else ""
    config = load_narration_config()
    if config is None:
        _log.info(
            "narration off: OSR_WEB_NARRATOR is %s",
            "empty" if "OSR_WEB_NARRATOR" in os.environ else "not set",
        )
        return None
    provider_cls = _PROVIDERS.get(config.provider_id)
    if provider_cls is None:
        _log.warning(
            "narration disabled: unknown provider %r (known: %s)%s",
            config.provider_id,
            ", ".join(sorted(_PROVIDERS)),
            source,
        )
        return None
    if not config.model:
        _log.warning(
            "narration disabled: OSR_WEB_NARRATOR_MODEL is required with OSR_WEB_NARRATOR=%s%s",
            config.provider_id,
            source,
        )
        return None
    provider = provider_cls(model=config.model, base_url=config.base_url, api_key=config.api_key)
    reason = provider.health_check()
    if reason is not None:
        _log.warning("narration disabled: %s%s", reason, source)
        return None
    _log.info("narration enabled: %s, model %s%s", provider.name, config.model, source)
    return NarrationSetup(provider=provider, timeout=config.timeout)
