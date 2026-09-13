# schemagate on Spider and BIRD

Every other number in this repo is measured on schemas I invented. That is
fine for catching regressions and worth very little to anyone else: a schema
whose questions happen to share vocabulary with its own table names will
flatter any retriever, and you have no way to check that I did not do exactly
that.

So here are the same measurements on the two public text-to-SQL benchmarks.
The data is downloaded from the original sources, the scripts are in
[`benchmarks/`](benchmarks/), and the whole thing reruns in about three
minutes.

## What is being measured

schemagate does not write SQL, so it cannot appear on either leaderboard —
those score execution accuracy of generated queries. What it does is pick the
tables, so what is scored here is **table recall**: given a question, does the
selected set contain every table the benchmark's own reference SQL reads.

Two numbers per row:

- **all gold tables present** — the strict one. Every table the answer needs
  was selected. A query missing one table does not run.
- **per-table recall** — the fraction of gold tables found, averaged over
  questions. Kinder, and useful for seeing how near a miss was.

## Spider (dev): 1,034 questions, 20 databases

```
PER-DATABASE                    all gold present     per-table recall
  top_k=3                            98.3%                99.1%
  top_k=5                           100.0%               100.0%
```

**This row is a floor, not a result.** Spider databases have a median of
three tables and a maximum of eleven. Picking five out of three is not
retrieval, and any method that returns the whole schema scores 100% here. It
is reported so you can see nothing is broken, and for no other reason.

The interesting setting is the one Spider does not ship: put every database
in one catalog and stop telling it which one to look in.

```
POOLED — all 166 Spider databases, 876 tables, no database hint

                      all gold present     per-table recall
  top_k=5                  71.4%                76.3%
  top_k=10                 82.6%                86.5%
  top_k=20                 92.9%                94.7%
```

That is 876 tables from 166 unrelated domains, with heavy name collision —
dozens of `name`, `id`, `student`, `country` columns that mean different
things — and no hint about where to look. It is closer to a real warehouse
than anything in Spider itself.

## BIRD (dev): 1,534 questions, 11 databases, 75 tables

BIRD is the harder benchmark: the questions are phrased the way people ask
rather than the way the schema is named, and the databases carry real naming
instead of tidy benchmark naming.

```
PER-DATABASE, question only     all gold present     per-table recall
  top_k=3                            88.3%                94.7%
  top_k=5                            96.5%                98.5%

POOLED (75 tables, no hint)
  top_k=5                            83.0%                90.6%
  top_k=10                           91.1%                95.1%
```

BIRD ships an `evidence` string per question — a human hint like *"eligible
free rate = Free Meal Count / Enrollment"*. Retrieval is measured without it
above, because the honest question is what the user's words alone can find.
With it, which is what a real caller would pass through:

```
PER-DATABASE, question + evidence
  top_k=3                            90.7%                96.0%
  top_k=5                            97.9%                99.1%

POOLED, question + evidence
  top_k=5                            85.7%                92.8%
  top_k=10                           93.9%                97.0%
```

## The embedder, on data I did not write

`pip install schemagate` uses a hashed n-gram vectoriser; installing
`schemagate[huggingface]` switches it to a sentence model automatically. On my
own schemas that was worth +3 questions out of 98, which is thin evidence.
On Spider pooled:

```
876 tables, no hint        hashed      MiniLM
  top_k=5                   71.4%   →   75.3%
  top_k=10                  82.6%   →   88.1%
  top_k=20                  92.9%   →   95.8%
```

Consistent, and larger than my own benchmarks suggested. Indexing 876 tables
takes 0.8s hashed and 9.3s with the sentence model.

## Reproducing this

```bash
pip install schemagate pandas pyarrow

# Spider dev questions and the schema dump (two public files)
curl -L -o spider_dev.parquet \
  https://huggingface.co/datasets/xlangai/spider/resolve/main/spider/validation-00000-of-00001.parquet
curl -L -o spider_schema.json \
  https://huggingface.co/datasets/richardr1126/spider-schema/resolve/main/spider_schema_rows_v2.json
python benchmarks/spider.py

# BIRD dev (346 MB from the official mirror)
curl -L -o bird_dev.zip https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip
python -c "import zipfile; z=zipfile.ZipFile('bird_dev.zip'); z.extract('dev_20240627/dev.json'); z.extract('dev_20240627/dev_tables.json')"
python benchmarks/bird.py
```

`SCHEMAGATE_AUTO_EMBEDDER=0` forces the hashed embedder if you have the
sentence model installed and want the base-install numbers.

## What these numbers are not

They are not a leaderboard placing, and schemagate is not eligible for one:
both boards score generated SQL, and this writes none. They are not
end-to-end accuracy either — a perfect table selection still leaves the model
to write a correct query.

What they are is a claim you can check without trusting me, on data neither
of us controls.
