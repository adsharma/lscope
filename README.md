Ingest:


```
$ uv run python3 main.py --schema-only -s schema.cypher
Applied schema from schema.cypher to test.db

$ uv run python3 main.py  --index icebug-format --language python
[1/9] python: .../icebug-format/icebug-format.py (0 symbols)
...
[9/9] python: .../icebug-format/icebug_format/test_csr_duckdb.py (3 symbols)

Ingested 9 file(s), 90 semantic node(s), and 149 resolved call(s) into test.db
  python: 9 file(s)

$ du -sh test.db
1.0M    test.db
```

Search:

```
uv run python3 main.py --find-functions 'main'
Functions matching /main/:
shape: (4, 5)
┌──────┬──────────┬──────────────────────┬────────────┬──────────┐
│ name ┆ kind     ┆ file_path            ┆ start_line ┆ end_line │
╞══════╪══════════╪══════════════════════╪════════════╪══════════╡
│ main ┆ Function ┆ .../verify_edges.py  ┆ 88         ┆ 114      │
│ ...  ┆ ...      ┆ ...                  ┆ ...        ┆ ...      │
└──────┴──────────┴──────────────────────┴────────────┴──────────┘
```

```
uv run python3 main.py --find-callers 'format_gb'
Callers of 'format_gb':
shape: (2, 6)
┌──────────────────────┬─────────────┬───────────┬───────────┬────────────┬──────────────────┐
│ caller               ┆ caller_kind ┆ file_path ┆ callee    ┆ confidence ┆ reason           │
╞══════════════════════╪═════════════╪═══════════╪═══════════╪════════════╪══════════════════╡
│ default_memory_limit ┆ Function    ┆ ...       ┆ format_gb ┆ 1.0        ┆ call at ...:129  │
│ parse_memory_limit   ┆ Function    ┆ ...       ┆ format_gb ┆ 1.0        ┆ call at ...:139  │
└──────────────────────┴─────────────┴───────────┴───────────┴────────────┴──────────────────┘
```
