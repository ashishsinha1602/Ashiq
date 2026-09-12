# Running schemagate with a local model

No API key, no network call, nothing about your schema leaving the machine.
This page is the whole story: what gets downloaded, what it costs in time and
memory, what it is good at, and where it is genuinely worse than a hosted
model.

## First, the part that is already local

`pip install schemagate` has one dependency (SQLAlchemy) and downloads no
model at all. Retrieval — reflection, BM25, the vectors, foreign-key
expansion, the inferred joins, the dimension grouping — is offline arithmetic
and always has been. The built-in `HashingEmbedder` is a hashed n-gram
vectoriser that gives byte-identical results on every machine, which is why
cached vectors stay valid across processes and Python versions.

So "local model" here means exactly one optional thing: **the model that
writes the one-sentence descriptions, and answers questions in the Studio.**
Everything else already runs on your laptop.

## What it actually downloads

Two separate downloads, and people are usually surprised by the second.

```bash
pip install 'schemagate[huggingface]'
```

installs `sentence-transformers`, `transformers` and `torch` — roughly 2 GB of
wheels, most of it torch. It does **not** install any weights.

The first time you actually use the local provider, `transformers` fetches the
model:

```
~/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct    3.10 GB
```

That is measured, not estimated. Budget about **5 GB on disk** for the pair,
and expect the first run to sit there downloading.

`huggingface` is deliberately **not** part of `schemagate[all]`. Someone who
wants a Postgres driver should not be made to pull torch. It stays one word
away:

```bash
pip install 'schemagate[all,huggingface]'
```

## Turning it on

**In the Studio** — *Settings → Model* → **Local (transformers)**, or the
**Local model — free** button on the first-run card. No key field appears,
because there is no key. The model box is prefilled with
`Qwen/Qwen2.5-1.5B-Instruct`. **Save model** persists it, so a restart does not
ask again.

**From the CLI:**

```bash
schemagate describe --url postgresql://localhost/app \
                    --provider local --model Qwen/Qwen2.5-1.5B-Instruct \
                    --cache .schemagate-cache.json
```

**From Python:**

```python
from schemagate import Catalog
from schemagate.ai import SchemaDescriber, LocalProvider

cat = Catalog().bootstrap("postgresql://localhost/app")
cat.describe(SchemaDescriber(LocalProvider(),            # Qwen2.5-1.5B by default
                             cache_path=".schemagate-cache.json"))
```

For a stack with no hosted component anywhere, pair it with the local
embedder:

```python
from schemagate import Catalog, SentenceTransformerEmbedder
cat = Catalog(embedder=SentenceTransformerEmbedder())    # all-MiniLM-L6-v2
```

Two warnings on that last one. Changing the embedder changes every vector, so
any cached or persisted index must be rebuilt. And on identifier-heavy schema
text a sentence model does **not** automatically beat the built-in one —
benchmark with `tests/bench.py` on your own schema before taking the
dependency.

## What it costs in time and memory

On a CPU laptop, measured:

| | |
|---|---|
| loading the model, before the first description | **91 s** |
| resident memory while running | **~6 GB** |
| per description | seconds, not milliseconds |
| second run over the same schema | free — see caching |

The load happens once per process. On a 1,200-object schema, cataloguing with
a local model is a coffee-break job, not an interactive one; with a hosted
model it is minutes.

Two things make that survivable:

- **Descriptions cache by content** (`--cache` / `cache_path=`). Re-running
  describes only objects that changed.
- **Objects that already have a database comment or a hint are skipped**, so a
  schema with real comments has far less to write.

Pass `device="cuda"` to `LocalProvider` if you have a GPU, and the numbers
above stop mattering.

## What it is good at, and what it is not

**Writing descriptions: yes.** One sentence per table is a small, bounded job,
and a 1.5B instruct model does it acceptably. This is the use it was added
for.

**Writing SQL: much weaker.** The Studio uses the same provider to answer
questions, so picking a local model also picks it for SQL generation. A 1.5B
model writes simple single-table SQL, and gets multi-table joins wrong far
more often than a frontier model does. If you have a key, use it for
answering, even if you catalogue locally.

**A bad description can no longer spoil retrieval.** This matters more than it
sounds, and it is the reason a weak local model is safe to point at your
schema. Scoring is fielded — names, prose (hints, comments, descriptions) and
the body are scored as separate channels and fused — so a vague or wrong
generated sentence can only degrade its own channel. It cannot bury the object
it describes. Before that change, cataloguing a 1,245-object schema made
retrieval *worse*: descriptions dropped the word "contact" to an IDF of 0.15
and `contacts` fell from rank 3 to below rank 40.

**Format is enforced harder for local models.** `SchemaDescriber.strict_format`
defaults to on when the provider name starts with `local:`, and off for
anything billed. A local call costs a second of laptop time, so re-asking a
1.5B model that ignored the format is worth it; the same retry against a paid
API is real money for a description that was already serviceable. Override it
explicitly if you disagree:

```python
SchemaDescriber(LocalProvider(), strict_format=False)
```

**Failures are skipped, not fatal.** The object is left undescribed,
cataloguing continues, and `describer.failures` lists what was missed.
`describer.truncated` lists replies that came back cut off even after a retry.
Pass `strict=True` if you would rather it raise.

## Where the weights live, and moving them

```
HF_HOME=/some/big/disk    # moves the whole cache
```

To pre-download on a machine with a network and carry it to one without, fetch
the model once, then copy `~/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct`
and set `HF_HOME` on the target. `transformers` will not reach the network if
the files are there; `HF_HUB_OFFLINE=1` makes that a guarantee rather than a
hope.

## Two alternatives worth knowing

**Ollama, LM Studio, vLLM — anything with an OpenAI-shaped endpoint.** Often
the better local path: the server manages the weights, you skip the torch
dependency entirely, and you can run a much larger model than 1.5B.

```python
from schemagate.ai import OpenAIProvider
OpenAIProvider(model="qwen2.5:14b", base_url="http://localhost:11434/v1")
```

**No model on the machine at all.** `describe` will write the prompt out and
take a reply back, so any chat window you already have can do the work:

```bash
schemagate describe --url postgresql://localhost/app --out prompt.txt
# paste prompt.txt into ChatGPT, Claude, Gemini, whatever; save the JSON reply
schemagate describe --url postgresql://localhost/app --apply reply.json --config catalog.json
```

The prompt is metadata only — names, types, comments, foreign keys, never a
row of data — and one paste covers every undescribed object.

## What leaves your machine

With `LocalProvider`: nothing. The Studio binds to localhost, reflection is a
database connection you already have, and the model runs in-process.

With any hosted provider, for comparison: table names, column names, types,
nullability, existing comments and foreign keys. Not one row of data —
`ObjectDoc` has no field that could hold one, and there are tests asserting
both halves of that. Nothing is sent unless you call `describe()`.
