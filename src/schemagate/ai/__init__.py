"""Optional AI features. schemagate works fully without importing this package.

Nothing here is required. The default catalog uses an offline, deterministic
embedder and no network. Import from ``schemagate.ai`` only when you want a model
to write catalog descriptions or produce embeddings, and bring your own key.

    from schemagate.ai import SchemaDescriber, AnthropicProvider
    cat.describe(SchemaDescriber(AnthropicProvider(model="claude-sonnet-4-5")))
"""
from .describe import SchemaDescriber
from .embedder import APIEmbedder
from .providers import (
    AnthropicProvider,
    CallableProvider,
    GeminiProvider,
    LocalProvider,
    OCIGenAIProvider,
    OpenAIProvider,
    Provider,
    ProviderError,
    auto_provider,
    available_providers,
)

__all__ = [
    "SchemaDescriber", "APIEmbedder",
    "Provider", "ProviderError", "CallableProvider",
    "AnthropicProvider", "OpenAIProvider", "GeminiProvider",
    "OCIGenAIProvider", "LocalProvider",
    "auto_provider", "available_providers",
]
