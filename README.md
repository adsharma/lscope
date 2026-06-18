Ingest:


```
uv run python3 main.py --file bubble_sort.rs --db test.db --schema schema.cypher
Ingested bubble_sort.rs into test.db
```

Search:

```
uv run python3 main.py --find-function 'bubble_.*'
Functions matching pattern:
shape: (7, 3)
┌─────────────────────────────────┬─────────────┬─────────────────┐
│ fn                              ┆ name_text   ┆ body_start_byte │
│ ---                             ┆ ---         ┆ ---             │
│ struct[21]                      ┆ str         ┆ i64             │
╞═════════════════════════════════╪═════════════╪═════════════════╡
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1031            │
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1035            │
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1038            │
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1049            │
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1070            │
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1073            │
│ {{341,1},"AstNode",null,null,n… ┆ bubble_sort ┆ 1082            │
└─────────────────────────────────┴─────────────┴─────────────────┘
```
