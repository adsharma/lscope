Ingest:


```
$ uv run python3 main.py --schema-only -s schema.cypher
Applied schema from schema.cypher to test.db

$ uv run python3 main.py  --index icebug-format --language python
[1/9] python: src/lscope/icebug-format/icebug-format.py (31 nodes)
[2/9] python: src/lscope/icebug-format/verify_edges.py (959 nodes)
[3/9] python: src/lscope/icebug-format/tests/test_cli.py (3928 nodes)
[4/9] python: src/lscope/icebug-format/tests/test_memory.py (2964 nodes)
[5/9] python: src/lscope/icebug-format/icebug_format/__init__.py (35 nodes)
[6/9] python: src/lscope/icebug-format/icebug_format/cli.py (5983 nodes)
[7/9] python: src/lscope/icebug-format/icebug_format/graphar.py (5036 nodes)
[8/9] python: src/lscope/icebug-format/icebug_format/memory.py (1013 nodes)
[9/9] python: src/lscope/icebug-format/icebug_format/test_csr_duckdb.py (1199 nodes)

Ingested 9 file(s) / 21148 AST node(s) into test.db
  python: 9 file(s)

$ du -sh test.db
7.1M    test.db
```

Search:

```
uv run python3 main.py --find-functions 'main'
Functions matching /main/:
shape: (2, 4)
┌───────────┬─────────────────────┬───────────┬───────────┐
│ name_text ┆ kind                ┆ start_row ┆ start_col │
│ ---       ┆ ---                 ┆ ---       ┆ ---       │
│ str       ┆ str                 ┆ i64       ┆ i64       │
╞═══════════╪═════════════════════╪═══════════╪═══════════╡
│ main      ┆ function_definition ┆ 87        ┆ 0         │
│ main      ┆ function_definition ┆ 131       ┆ 0         │
└───────────┴─────────────────────┴───────────┴───────────┘
```

```
uv run python3 main.py --find-callers 'format_gb'
Callers of 'format_gb':
shape: (2, 5)
┌─────────────────────────────────┬───────────┬───────────┬───────────┬─────────────────────────────────┐
│ call_id                         ┆ call_kind ┆ start_row ┆ start_col ┆ file                            │
│ ---                             ┆ ---       ┆ ---       ┆ ---       ┆ ---                             │
│ str                             ┆ str       ┆ i64       ┆ i64       ┆ str                             │
╞═════════════════════════════════╪═══════════╪═══════════╪═══════════╪═════════════════════════════════╡
│ /Users/arun/src/lscope/icebug-… ┆ call      ┆ 128       ┆ 14        ┆ /Users/arun/src/lscope/icebug-… │
│ /Users/arun/src/lscope/icebug-… ┆ call      ┆ 138       ┆ 18        ┆ /Users/arun/src/lscope/icebug-… │
└─────────────────────────────────┴───────────┴───────────┴───────────┴─────────────────────────────────┘
```
