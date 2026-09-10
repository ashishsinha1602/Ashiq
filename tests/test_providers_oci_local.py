"""OCIGenAIProvider and LocalProvider, offline. The SDKs are faked at the
seam schemagate uses, so these run in CI with no key, no tenancy and no
model download; a live run is a one-liner in TESTING.md."""
from __future__ import annotations

import sys
import types

import pytest

from schemagate.ai import (LocalProvider, OCIGenAIProvider, ProviderError,
                           SchemaDescriber, auto_provider, available_providers)
from schemagate.ai.providers import _oci_text


# ---------------------------------------------------------------- OCI ------

class _Resp:
    def __init__(self, data): self.data = data

class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)


def _fake_oci_module():
    """Just enough of the ``oci`` SDK surface for the provider to build requests."""
    m = types.ModuleType("oci")
    models = types.SimpleNamespace()
    for name in ("OnDemandServingMode", "DedicatedServingMode", "CohereChatRequest",
                 "GenericChatRequest", "SystemMessage", "UserMessage", "TextContent",
                 "ChatDetails", "EmbedTextDetails"):
        setattr(models, name, type(name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}))
    m.generative_ai_inference = types.SimpleNamespace(models=models)
    return m


class _FakeClient:
    def __init__(self, cohere=False):
        self.calls = []
        self.cohere = cohere

    def chat(self, details):
        self.calls.append(details)
        if self.cohere:
            return _Resp(_Obj(chat_response=_Obj(text="Cohere says hello.")))
        msg = _Obj(content=[_Obj(text="Generic "), _Obj(text="says hello.")])
        return _Resp(_Obj(chat_response=_Obj(choices=[_Obj(message=msg)])))

    def embed_text(self, details):
        self.calls.append(details)
        n = len(details.inputs)
        return _Resp(_Obj(embeddings=[[0.1, 0.2, 0.3]] * n))


@pytest.fixture
def fake_oci(monkeypatch):
    monkeypatch.setitem(sys.modules, "oci", _fake_oci_module())


def test_oci_requires_model_and_compartment(monkeypatch):
    monkeypatch.delenv("OCI_COMPARTMENT_ID", raising=False)
    with pytest.raises(ValueError, match="model is required"):
        OCIGenAIProvider(model="", compartment_id="ocid1.compartment.x")
    with pytest.raises(ValueError, match="compartment_id"):
        OCIGenAIProvider(model="meta.llama-3.3-70b-instruct", client=object())


def test_oci_generic_chat_shape(fake_oci):
    client = _FakeClient()
    p = OCIGenAIProvider(model="meta.llama-3.3-70b-instruct",
                         compartment_id="ocid1.compartment.x", client=client)
    assert p.complete("sys", "hi") == "Generic says hello."
    details = client.calls[0]
    assert details.compartment_id == "ocid1.compartment.x"
    assert details.serving_mode.model_id == "meta.llama-3.3-70b-instruct"
    assert type(details.chat_request).__name__ == "GenericChatRequest"
    assert details.chat_request.temperature == 0


def test_oci_cohere_chat_shape(fake_oci):
    client = _FakeClient(cohere=True)
    p = OCIGenAIProvider(model="cohere.command-r-plus-08-2024",
                         compartment_id="c", client=client)
    assert p.complete("sys", "hi") == "Cohere says hello."
    req = client.calls[0].chat_request
    assert type(req).__name__ == "CohereChatRequest"
    assert req.preamble_override == "sys" and req.message == "hi"


def test_oci_dedicated_endpoint_ocid_uses_dedicated_serving(fake_oci):
    client = _FakeClient()
    p = OCIGenAIProvider(model="ocid1.generativeaiendpoint.oc1..abc",
                         compartment_id="c", client=client)
    p.complete("s", "p")
    assert type(client.calls[0].serving_mode).__name__ == "DedicatedServingMode"
    assert client.calls[0].serving_mode.endpoint_id.startswith("ocid1.generativeaiendpoint")


def test_oci_embed_batches_and_requires_embed_model(fake_oci):
    client = _FakeClient()
    p = OCIGenAIProvider(model="m", compartment_id="c", client=client)
    with pytest.raises(ProviderError, match="embed_model"):
        p.embed(["a"])
    p = OCIGenAIProvider(model="m", compartment_id="c", client=client,
                         embed_model="cohere.embed-multilingual-v3.0")
    vecs = p.embed([f"t{i}" for i in range(200)])
    assert len(vecs) == 200 and vecs[0] == [0.1, 0.2, 0.3]
    assert len(client.calls) == 3          # 96 + 96 + 8
    assert client.calls[0].serving_mode.model_id == "cohere.embed-multilingual-v3.0"


def test_oci_sdk_errors_become_provider_errors(fake_oci):
    class Boom:
        def chat(self, d): raise RuntimeError("429 throttled")
    p = OCIGenAIProvider(model="m", compartment_id="c", client=Boom())
    with pytest.raises(ProviderError, match="429"):
        p.complete("s", "p")


def test_oci_text_handles_both_shapes():
    assert _oci_text(_Obj(text="x")) == "x"
    assert _oci_text(_Obj(text=None, choices=[])) == ""
    msg = _Obj(content=[_Obj(text="a"), _Obj(text=None), _Obj(text="b")])
    assert _oci_text(_Obj(text=None, choices=[_Obj(message=msg)])) == "ab"


def test_oci_describer_end_to_end(fake_oci):
    """Descriptions land on the catalog through the same path as every provider."""
    from schemagate import Catalog
    from schemagate.demo_schema import create_demo_db
    class Client:
        def chat(self, d):
            return _Resp(_Obj(chat_response=_Obj(text="Holds things; answers questions.")))
    cat = Catalog().bootstrap(create_demo_db())
    n = cat.describe(SchemaDescriber(OCIGenAIProvider(model="cohere.x", compartment_id="c",
                                                      client=Client())), only_missing=False)
    assert n == len(cat)
    assert all(d.description == "Holds things; answers questions." for d in cat.objects())


def test_oci_is_auto_detected_from_compartment_env(fake_oci, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OCI_COMPARTMENT_ID", "ocid1.compartment.x")
    assert available_providers() == ["OCIGenAIProvider"]
    p = auto_provider("meta.llama-3.3-70b-instruct", client=_FakeClient())
    assert isinstance(p, OCIGenAIProvider) and p.compartment_id == "ocid1.compartment.x"


def test_oci_key_holders_still_win_auto_detection(fake_oci, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OCI_COMPARTMENT_ID", "c")
    assert available_providers()[0] == "AnthropicProvider"


def test_oci_never_reads_a_key_from_anywhere(fake_oci):
    """There is no API key to leak: the provider has no api_key attribute."""
    p = OCIGenAIProvider(model="m", compartment_id="c", client=_FakeClient())
    assert not hasattr(p, "api_key") and not hasattr(p, "env_var")


# -------------------------------------------------------------- Local ------

def test_local_provider_uses_chat_pipeline_and_strips():
    seen = {}
    def pipe(messages, **kw):
        seen["messages"] = messages; seen["kw"] = kw
        return [{"generated_text": "  One sentence.  "}]
    p = LocalProvider(model="tiny", pipeline=pipe)
    assert p.complete("SYS", "PROMPT", max_tokens=50) == "One sentence."
    assert seen["messages"][0] == {"role": "system", "content": "SYS"}
    assert seen["kw"]["do_sample"] is False and seen["kw"]["max_new_tokens"] == 50


def test_local_provider_accepts_chat_format_return():
    def pipe(messages, **kw):
        return [{"generated_text": messages + [{"role": "assistant", "content": "reply"}]}]
    assert LocalProvider(pipeline=pipe).complete("s", "p") == "reply"


def test_local_provider_errors_become_provider_errors():
    def pipe(messages, **kw): raise RuntimeError("OOM")
    with pytest.raises(ProviderError, match="OOM"):
        LocalProvider(pipeline=pipe).complete("s", "p")


def test_local_provider_import_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ImportError, match="huggingface"):
        LocalProvider()


def test_cli_knows_the_new_providers():
    import inspect
    from schemagate import cli
    src = inspect.getsource(cli)
    assert '"oci": _p.OCIGenAIProvider' in src and '"local": _p.LocalProvider' in src


# --- found live: Gemini through OCI returned only the first content part ----
# Every description arrived cut off at ~10 tokens. Recall fell 20 points and
# nothing raised. These pin the shapes and the detection.

def test_oci_text_joins_every_content_part():
    """The live bug: only the first part was read, so the sentence was cut."""
    msg = _Obj(content=[_Obj(text="This view shows which devices are "),
                        _Obj(text="running low on battery. "),
                        _Obj(text="| flat, dying, dead")])
    got = _oci_text(_Obj(text=None, choices=[_Obj(message=msg)]))
    assert got == "This view shows which devices are running low on battery. | flat, dying, dead"


def test_oci_text_handles_a_bare_string_content():
    assert _oci_text(_Obj(text=None, choices=[_Obj(message=_Obj(content="plain reply."))])) == "plain reply."


def test_oci_text_handles_string_parts_and_nesting():
    msg = _Obj(content=["a ", _Obj(text="b "), _Obj(content=[_Obj(text="c")])])
    assert _oci_text(_Obj(text=None, choices=[_Obj(message=msg)])) == "a b c"


def test_oci_text_prefers_cohere_text_when_present():
    assert _oci_text(_Obj(text="cohere wins", choices=[])) == "cohere wins"


def test_oci_text_survives_an_unknown_shape():
    assert _oci_text(_Obj(text=None, choices=[_Obj(message=None)])) == ""


def test_truncation_is_detected():
    from schemagate.ai.describe import looks_truncated
    # the exact strings the live run produced
    assert looks_truncated("This view shows which devices are")
    assert looks_truncated("This view shows which customers are")
    assert looks_truncated("")
    # complete replies are not flagged
    assert not looks_truncated("Devices whose battery is below threshold.")
    assert not looks_truncated("Devices below threshold | flat, dying, dead, charge")
    # long but unpunctuated is left alone -- do not cry wolf
    assert not looks_truncated(" ".join(["word"] * 14))


def test_describer_retries_a_truncated_reply_then_warns_once():
    """A provider that caps output must not silently degrade the catalog."""
    import warnings
    from schemagate.ai import SchemaDescriber
    from schemagate.models import Column, ObjectDoc

    class Capped:
        name = "capped"
        def __init__(self): self.calls = []
        def complete(self, system, prompt, max_tokens=1024):
            self.calls.append(max_tokens)
            return "This view shows which devices are"   # always cut

    class Recovers:
        name = "recovers"
        def __init__(self): self.calls = []
        def complete(self, system, prompt, max_tokens=1024):
            self.calls.append(max_tokens)
            return ("This view shows which devices are" if len(self.calls) == 1
                    else "Devices whose battery is low. | flat, dying, dead")

    docs = [ObjectDoc(name="v_low_battery", schema=None, kind="VIEW",
                      columns=[Column(name="device_id", type="INTEGER")])]

    p = Recovers()
    d = SchemaDescriber(p, max_tokens=220, workers=1)
    out = d.describe(docs)
    assert out["v_low_battery"].startswith("Devices whose battery")
    assert p.calls == [220, 512], "the retry must ask for more room"
    assert d.truncated == []

    p2 = Capped()
    d2 = SchemaDescriber(p2, max_tokens=220, workers=1)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out2 = d2.describe(docs)
    assert out2["v_low_battery"] == "This view shows which devices are"  # still usable
    assert d2.truncated == ["v_low_battery"]
    assert len(w) == 1 and "truncated" in str(w[0].message)


def test_cli_passes_instance_principal_auth_for_oci(monkeypatch):
    """On an OCI VM there is no ~/.oci/config -- the machine authenticates as
    itself. Without this the stack's first-boot cataloguing failed silently
    with ConfigFileNotFound and every table went undescribed."""
    import types
    from schemagate import cli
    seen = {}

    class FakeOCI:
        def __init__(self, **kw): seen.update(kw)
        name = "oci:fake"
        def complete(self, system, prompt, max_tokens=1024): return "x."

    monkeypatch.setattr(cli, "_open", lambda a: __import__("schemagate").Catalog())
    monkeypatch.setenv("OCI_CLI_AUTH", "instance_principal")
    from schemagate.ai import providers as _p
    monkeypatch.setattr(_p, "OCIGenAIProvider", FakeOCI)

    args = types.SimpleNamespace(provider="oci", model="google.gemini-2.5-pro",
                                 cache=None, config=None, all=True, apply=None,
                                 url="sqlite://", schemas=None, include=None, exclude=None)
    try:
        cli.cmd_describe(args)
    except Exception:
        pass
    assert seen.get("auth") == "instance_principal", seen
    assert seen.get("model") == "google.gemini-2.5-pro"


def test_cli_omits_auth_when_env_is_unset(monkeypatch):
    import types
    from schemagate import cli
    from schemagate.ai import providers as _p
    seen = {}

    class FakeOCI:
        def __init__(self, **kw): seen.update(kw)
        name = "oci:fake"
        def complete(self, s, p, max_tokens=1024): return "x."

    monkeypatch.delenv("OCI_CLI_AUTH", raising=False)
    monkeypatch.setattr(cli, "_open", lambda a: __import__("schemagate").Catalog())
    monkeypatch.setattr(_p, "OCIGenAIProvider", FakeOCI)
    args = types.SimpleNamespace(provider="oci", model="m", cache=None, config=None,
                                 all=True, apply=None, url="sqlite://", schemas=None,
                                 include=None, exclude=None)
    try:
        cli.cmd_describe(args)
    except Exception:
        pass
    assert "auth" not in seen, seen
