"""Adapters for hosted AI models. All optional; none is ever required.

schemagate works with no AI provider at all -- the default embedder is offline and
deterministic. A provider buys you two things: better catalog descriptions
(``schemagate.ai.SchemaDescriber``) and semantic embeddings
(``schemagate.ai.APIEmbedder``). Both are opt-in and both degrade to the offline
path if the provider is unavailable.

Bring your own key. Nothing here reads a key from anywhere except the
environment variable of the provider you chose, or the value you pass.

``model`` is a required argument on every provider. That is deliberate:
model identifiers change often, and a library that hardcodes one eventually
ships a default that 404s for everyone. Pass the model you actually have
access to.

    from schemagate.ai import AnthropicProvider, SchemaDescriber

    provider = AnthropicProvider(model="claude-sonnet-4-5")
    cat.describe(SchemaDescriber(provider))

If your provider is not one of the three below, or its SDK changes shape,
use ``CallableProvider`` and keep control of the call yourself::

    CallableProvider(lambda system, prompt: my_llm(system, prompt))
"""
from __future__ import annotations

import os
from typing import Any, Callable, List, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Provider(Protocol):
    """What schemagate needs from a model. Implement either method, or both."""

    name: str

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str: ...


class ProviderError(RuntimeError):
    """A provider call failed. Callers decide whether that is fatal."""


# --------------------------------------------------------------------------
# Bring your own callable -- the escape hatch that never breaks on an SDK bump
# --------------------------------------------------------------------------

class CallableProvider:
    """Wrap any function ``f(system, prompt) -> str``.

    Use this for a provider schemagate does not ship, a gateway, a local model,
    or when an SDK changes and you do not want to wait for a release.
    """

    def __init__(self, fn: Callable[[str, str], str], name: str = "callable",
                 embed_fn: Optional[Callable[[Sequence[str]], List[List[float]]]] = None):
        self._fn = fn
        self._embed_fn = embed_fn
        self.name = name

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        return self._fn(system, prompt)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if self._embed_fn is None:
            raise ProviderError(f"{self.name} was built without an embed_fn")
        return self._embed_fn(texts)


# --------------------------------------------------------------------------
# Anthropic (Claude)
# --------------------------------------------------------------------------

class AnthropicProvider:
    """Claude via the official ``anthropic`` SDK.

    ``pip install anthropic``. Key from ``api_key=`` or ``ANTHROPIC_API_KEY``.
    """

    env_var = "ANTHROPIC_API_KEY"

    def __init__(self, model: str, api_key: Optional[str] = None,
                 client: Any = None, timeout: float = 60.0):
        if not model:
            raise ValueError("model is required, e.g. model='claude-sonnet-4-5'")
        self.model = model
        self.name = f"anthropic:{model}"
        if client is not None:
            self._client = client
            return
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("pip install 'schemagate[anthropic]' to use "
                              "AnthropicProvider") from e
        key = api_key or os.environ.get(self.env_var)
        if not key:
            raise ValueError(f"no API key: pass api_key= or set {self.env_var}")
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout)

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


# --------------------------------------------------------------------------
# OpenAI (GPT) -- also covers Azure OpenAI and any OpenAI-compatible gateway
# --------------------------------------------------------------------------

class OpenAIProvider:
    """GPT via the official ``openai`` SDK.

    ``pip install openai``. Key from ``api_key=`` or ``OPENAI_API_KEY``.
    Point ``base_url`` at any OpenAI-compatible endpoint (Azure, vLLM,
    OpenRouter, Ollama) to use this adapter with something else.
    """

    env_var = "OPENAI_API_KEY"

    def __init__(self, model: str, api_key: Optional[str] = None,
                 client: Any = None, base_url: Optional[str] = None,
                 embed_model: Optional[str] = None, timeout: float = 60.0):
        if not model:
            raise ValueError("model is required, e.g. model='gpt-4.1-mini'")
        self.model = model
        self.embed_model = embed_model
        self.name = f"openai:{model}"
        if client is not None:
            self._client = client
            return
        try:
            import openai
        except ImportError as e:
            raise ImportError("pip install 'schemagate[openai]' to use "
                              "OpenAIProvider") from e
        key = api_key or os.environ.get(self.env_var)
        if not key:
            raise ValueError(f"no API key: pass api_key= or set {self.env_var}")
        self._client = openai.OpenAI(api_key=key, base_url=base_url,
                                     timeout=timeout)

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return resp.choices[0].message.content or ""

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.embed_model:
            raise ProviderError(
                "pass embed_model= to use OpenAIProvider for embeddings, "
                "e.g. embed_model='text-embedding-3-small'")
        try:
            resp = self._client.embeddings.create(model=self.embed_model,
                                                  input=list(texts))
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return [list(d.embedding) for d in resp.data]


# --------------------------------------------------------------------------
# Google (Gemini)
# --------------------------------------------------------------------------

class GeminiProvider:
    """Gemini via the ``google-genai`` SDK.

    ``pip install google-genai``. Key from ``api_key=``, ``GEMINI_API_KEY``
    or ``GOOGLE_API_KEY``.
    """

    env_var = "GEMINI_API_KEY"
    alt_env_var = "GOOGLE_API_KEY"

    def __init__(self, model: str, api_key: Optional[str] = None,
                 client: Any = None, embed_model: Optional[str] = None):
        if not model:
            raise ValueError("model is required, e.g. model='gemini-2.5-flash'")
        self.model = model
        self.embed_model = embed_model
        self.name = f"gemini:{model}"
        if client is not None:
            self._client = client
            return
        try:
            from google import genai
        except ImportError as e:
            raise ImportError("pip install 'schemagate[gemini]' to use "
                              "GeminiProvider") from e
        key = (api_key or os.environ.get(self.env_var)
               or os.environ.get(self.alt_env_var))
        if not key:
            raise ValueError(
                f"no API key: pass api_key= or set {self.env_var}")
        self._client = genai.Client(api_key=key)

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=f"{system}\n\n{prompt}")
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return resp.text or ""

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.embed_model:
            raise ProviderError(
                "pass embed_model= to use GeminiProvider for embeddings")
        try:
            resp = self._client.models.embed_content(model=self.embed_model,
                                                     contents=list(texts))
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return [list(e.values) for e in resp.embeddings]


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

#: checked in order; first key present wins
_AUTO_ORDER = [
    ("ANTHROPIC_API_KEY", AnthropicProvider),
    ("OPENAI_API_KEY", OpenAIProvider),
    ("GEMINI_API_KEY", GeminiProvider),
    ("GOOGLE_API_KEY", GeminiProvider),
]


def available_providers(env: Optional[dict] = None) -> List[str]:
    """Names of providers whose API key is present. Never returns a key."""
    env = os.environ if env is None else env
    seen, out = set(), []
    for var, cls in _AUTO_ORDER:
        if env.get(var) and cls.__name__ not in seen:
            seen.add(cls.__name__)
            out.append(cls.__name__)
    return out


def auto_provider(model: str, env: Optional[dict] = None, **kwargs):
    """Build a provider from whichever API key is in the environment.

    Convenience only. Construct the provider directly when you care which
    one you get -- this picks by key presence, not by capability.
    """
    env = os.environ if env is None else env
    for var, cls in _AUTO_ORDER:
        if env.get(var):
            return cls(model=model, api_key=env[var], **kwargs)
    raise ValueError(
        "no provider API key found; set one of "
        + ", ".join(v for v, _ in _AUTO_ORDER)
        + " or construct a provider directly. schemagate works without one."
    )
