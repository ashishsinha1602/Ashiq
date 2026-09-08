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
                 client: Any = None, timeout: float = 60.0, max_retries: int = 2):
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
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)

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
# Oracle Cloud (OCI Generative AI) -- keyless; data stays in your tenancy
# --------------------------------------------------------------------------

class OCIGenAIProvider:
    """OCI Generative AI via the ``oci`` SDK. No API key: authenticates the
    way every other OCI tool does -- a profile in ``~/.oci/config``, or a
    resource / instance principal when running inside OCI.

    ``pip install oci``. Every model in the OCI Generative AI catalogue works
    (Cohere, Meta Llama, Google Gemini, xAI Grok ...); pass the model id
    shown in the console or an endpoint OCID. ``embed_model`` enables
    :meth:`embed` with an OCI embedding model such as
    ``cohere.embed-multilingual-v3.0``.

    Prompts and schema metadata are sent to the OCI region you name and
    nowhere else, which is the point for an Autonomous Database shop.
    """

    def __init__(self, model: str, compartment_id: Optional[str] = None,
                 region: Optional[str] = None, profile: Optional[str] = None,
                 auth: str = "config", client: Any = None,
                 embed_model: Optional[str] = None, timeout: float = 60.0):
        if not model:
            raise ValueError("model is required, e.g. model='cohere.command-r-plus-08-2024'")
        self.model = model
        self.embed_model = embed_model
        self.name = f"oci:{model}"
        self.compartment_id = compartment_id or os.environ.get("OCI_COMPARTMENT_ID")
        if not self.compartment_id:
            raise ValueError("compartment_id is required: pass it or set OCI_COMPARTMENT_ID")
        if client is not None:
            self._client = client
            return
        try:
            import oci
        except ImportError as e:
            raise ImportError("pip install 'schemagate[oci]' to use OCIGenAIProvider") from e
        region = region or os.environ.get("OCI_REGION")
        endpoint = (f"https://inference.generativeai.{region}.oci.oraclecloud.com"
                    if region else None)
        kwargs: dict = {"timeout": timeout}
        if endpoint:
            kwargs["service_endpoint"] = endpoint
        if auth == "config":
            cfg = oci.config.from_file(
                profile_name=profile or os.environ.get("OCI_CLI_PROFILE", "DEFAULT"))
            if not endpoint and cfg.get("region"):
                kwargs["service_endpoint"] = (
                    f"https://inference.generativeai.{cfg['region']}.oci.oraclecloud.com")
            self._client = oci.generative_ai_inference.GenerativeAiInferenceClient(cfg, **kwargs)
        elif auth == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            self._client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                {}, signer=signer, **kwargs)
        elif auth == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self._client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                {}, signer=signer, **kwargs)
        else:
            raise ValueError("auth must be 'config', 'resource_principal' or 'instance_principal'")

    def _serving(self):
        import oci
        m = oci.generative_ai_inference.models
        if self.model.startswith("ocid1."):
            return m.DedicatedServingMode(endpoint_id=self.model)
        return m.OnDemandServingMode(model_id=self.model)

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        try:
            import oci
            m = oci.generative_ai_inference.models
            if self.model.startswith("cohere."):
                req = m.CohereChatRequest(message=prompt, preamble_override=system,
                                          max_tokens=max_tokens, temperature=0)
            else:
                req = m.GenericChatRequest(
                    messages=[m.SystemMessage(content=[m.TextContent(text=system)]),
                              m.UserMessage(content=[m.TextContent(text=prompt)])],
                    max_tokens=max_tokens, temperature=0)
            details = m.ChatDetails(compartment_id=self.compartment_id,
                                    serving_mode=self._serving(), chat_request=req)
            resp = self._client.chat(details)
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return _oci_text(resp.data.chat_response)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.embed_model:
            raise ProviderError("pass embed_model= to use OCIGenAIProvider for embeddings, "
                                "e.g. embed_model='cohere.embed-multilingual-v3.0'")
        try:
            import oci
            m = oci.generative_ai_inference.models
            out: List[List[float]] = []
            texts = list(texts)
            for i in range(0, len(texts), 96):            # service batch limit
                details = m.EmbedTextDetails(
                    inputs=texts[i:i + 96], compartment_id=self.compartment_id,
                    serving_mode=m.OnDemandServingMode(model_id=self.embed_model),
                    truncate="END")
                resp = self._client.embed_text(details)
                out.extend([list(map(float, v)) for v in resp.data.embeddings])
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        return out


def _oci_text(chat_response: Any) -> str:
    """Pull the text out of either OCI chat response shape."""
    text = getattr(chat_response, "text", None)            # Cohere
    if text:
        return text
    choices = getattr(chat_response, "choices", None) or []  # Generic
    parts: List[str] = []
    for ch in choices:
        content = getattr(getattr(ch, "message", None), "content", None) or []
        for c in content:
            t = getattr(c, "text", None)
            if t:
                parts.append(t)
    return "".join(parts)


# --------------------------------------------------------------------------
# Local (Hugging Face transformers) -- free, offline, no key
# --------------------------------------------------------------------------

class LocalProvider:
    """A small instruct model on your own machine, via ``transformers``.

    ``pip install 'schemagate[huggingface]'``. The default model writes
    one-sentence table descriptions well enough on a CPU and costs nothing
    per call; the model download happens once. Nothing leaves the machine.
    Pair with ``SentenceTransformerEmbedder`` for a fully local stack.
    """

    def __init__(self, model: str = "Qwen/Qwen2.5-1.5B-Instruct", device: Optional[str] = None,
                 pipeline: Any = None, max_new_tokens: int = 256):
        self.model = model
        self.name = f"local:{model}"
        self.max_new_tokens = max_new_tokens
        if pipeline is not None:
            self._pipe = pipeline
            return
        try:
            from transformers import pipeline as _pipeline
        except ImportError as e:
            raise ImportError("pip install 'schemagate[huggingface]' to use LocalProvider") from e
        kwargs = {"device": device} if device is not None else {}
        self._pipe = _pipeline("text-generation", model=model, **kwargs)

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": prompt}]
        try:
            out = self._pipe(messages, max_new_tokens=min(max_tokens, self.max_new_tokens),
                             do_sample=False, return_full_text=False)
        except Exception as e:
            raise ProviderError(f"{self.name}: {e}") from e
        gen = out[0]["generated_text"] if out else ""
        if isinstance(gen, list):                # chat-format return
            gen = "".join(m.get("content", "") for m in gen if m.get("role") == "assistant")
        return (gen or "").strip()


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

#: checked in order; first key present wins
_AUTO_ORDER = [
    ("ANTHROPIC_API_KEY", AnthropicProvider),
    ("OPENAI_API_KEY", OpenAIProvider),
    ("GEMINI_API_KEY", GeminiProvider),
    ("GOOGLE_API_KEY", GeminiProvider),
    ("OCI_COMPARTMENT_ID", OCIGenAIProvider),   # keyless: ~/.oci/config or a principal
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
            if cls is OCIGenAIProvider:
                return cls(model=model, compartment_id=env[var], **kwargs)
            return cls(model=model, api_key=env[var], **kwargs)
    raise ValueError(
        "no provider API key found; set one of "
        + ", ".join(v for v, _ in _AUTO_ORDER)
        + " or construct a provider directly. schemagate works without one."
    )
