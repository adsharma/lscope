# lscope — semantic code graph (via tree-sitter) on Ladybug

lscope parses source files with tree-sitter, extracts a semantic graph
(files, classes, functions, methods, calls), and stores it in a
[Ladybug](https://github.com/thatguyfrombb/ladybug) database.  It then
supports lightweight code-intelligence queries: find functions by name
pattern, or find callers of a given function.

---

## Pipeline phases

When invoked with `--index`, the tool runs through several phases.
Some are parallel, some are single-threaded.  The diagram below shows
the data flow and concurrency model:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. File Discovery (single-threaded)                            │
│     Walk targets, match extensions, collect file list           │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. Analysis / Parsing (parallel — ThreadPoolExecutor)          │
│     Each file is read + tree-sitter parsed in a worker thread.  │
│     Every thread has its own thread-local LanguageRegistry      │
│     (parser instances).                                         │
│     Progress: tqdm "Analyzing" bar across all files.            │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. Node Ingestion (parallel when workers > 1)                  │
│     Analyses are split round-robin into worker chunks.          │
│     Each chunk runs in its own thread, opening its own          │
│     ladybug Connection with enable_multi_writes=True.           │
│     Nodes are bulk-inserted via UNWIND + MERGE per label        │
│     in a single transaction per chunk.                          │
│     Progress: tqdm "Ingesting" bar across chunks.               │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. Definition Edges (single-threaded)                          │
│     CONTAINS / DEFINES / HAS_METHOD edges inserted via          │
│     COPY CodeRelation (DDL — cannot run concurrently with       │
│     active write transactions from step 3).                     │
│     Runs on the main connection after all workers finish.       │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  5. Call Resolution & Edges (single-threaded)                   │
│     Build a global name index across all analyzed files.        │
│     Resolve each call expression to a declaration by name,      │
│     preferring same-file matches.  Bulk-insert CALLS edges      │
│     via COPY CodeRelation on the main connection.               │
└─────────────────────────────────────────────────────────────────┘
```

When running a **query** (`--find-functions` or `--find-callers`), the
pipeline is trivial: a single read-only connection is opened and the
query executes on the main thread.

---

### Phase details

#### 1. File discovery — single-threaded

`iter_source_files()` walks the given paths (files or directories).
Directories are recursed with `os.walk`, skipping common ignorable
directories (`.git`, `__pycache__`, `node_modules`, `.venv`, …).
Files are matched against the known extension map from the installed
tree-sitter grammars.  If `--language` is given, only files of that
language are kept.

This runs entirely in the main thread — it is I/O-bound on `os.walk`
and has no heavy computation.

#### 2. Analysis / parsing — parallel (ThreadPoolExecutor)

Each source file is read from disk and parsed by a tree-sitter `Parser`.
The parser is obtained from a **thread-local** `LanguageRegistry`
(`_worker_state.registry`) so each thread creates its own parser
instances — tree-sitter parsers are not thread-safe for concurrent
use from multiple threads.

The `analyze_path()` function:
1. Determines the language from the file extension.
2. Reads the full file into a string.
3. Parses it with the appropriate tree-sitter grammar.
4. Walks the CST to extract:
   - **Containers** — classes, structs, interfaces, traits, impl blocks.
   - **Functions / methods** — with their owning container.
   - **Call expressions** — recording caller, callee name, and source location.

No database calls happen in this phase.  All extracted data is returned
as plain dicts for the next phase.

#### 3. Node ingestion — parallel (ThreadPoolExecutor, multi-write DB)

`ingest_analyses_parallel()` splits the list of analyses into
`workers` chunks (round-robin).  Each chunk is processed by a worker
thread that:

1. Opens its own `ladybug.Connection` to the shared database (which was
   opened with `enable_multi_writes=True`).
2. Calls `_collect_file_data()` to organise extracted symbols by label.
3. Bulk-inserts all nodes for that chunk:
   - **Files** — `UNWIND … MERGE (f:File …)`.
   - **Symbols** — `UNWIND … MERGE (n:{label} …)` per label
     (Function, Method, Class, Struct, …).
   - All wrapped in a single `BEGIN TRANSACTION … COMMIT` per chunk.
4. Returns definition edges for deferred insertion.

**Why parallel?** Node insertion is the bulk of the write work and
Ladybug's multi-write mode permits concurrent write transactions from
different connections.  This gives a significant speedup for large
codebases.

**Why not parallel for everything?** Step 4 (definition edges) and
step 5 (call edges) use `COPY CodeRelation`, which is a DDL statement
and cannot run concurrently with other write transactions.

#### 4. Definition edges — single-threaded

After all worker threads finish, the main thread inserts edges that
represent:

- **CONTAINS** — file contains a function/class/etc.
- **DEFINES** — a file defines a top-level symbol.
- **HAS_METHOD** — a container (class/struct/trait/impl) has a method.

Edges are grouped by `(source_label, target_label)`, written to a
temporary Parquet file per group, and bulk-loaded with
`COPY CodeRelation FROM '…' (FROM='…', TO='…')`.

This runs single-threaded because `COPY` on the `CodeRelation`
relationship table group is a DDL-level operation that requires
exclusive write access.

#### 5. Call resolution & edges — single-threaded

`ingest_calls()` builds a global dictionary mapping each declared
function/method name to its symbol dict(s), then iterates over every
call expression from every file:

1. Looks up the callee name in the name index.
2. Prefers a declaration in the **same file** (for local disambiguation).
3. Falls back to the first candidate if there is no same-file match.
4. Assigns `confidence = 1.0` for unique matches, `0.7` when multiple
   candidates exist (ambiguous name).
5. Bulk-inserts all `CALLS` edges via `_ingest_chunk_edges()` — again
   using the DDL `COPY` path.

This must be single-threaded because:
- The name index is a global data structure built across **all** files.
- `COPY CodeRelation` is DDL and cannot run concurrently.
- Call resolution is lightweight (dictionary lookups) so parallelism
   would add overhead without benefit.

#### Schema setup — single-threaded

When the database is empty, `ensure_schema()` runs the schema file
(`schema.cypher`) to create all node tables (`File`, `Function`,
`Method`, `Class`, `Struct`, `Interface`, `Trait`, `Impl`, …) and
the relationship table group `CodeRelation` with all permitted
`FROM→TO` pairs.

This runs once at the start of `--index`, before any other phases.

---

## Concurrency model summary

| Phase | Threads | DB mode | Notes |
|---|---|---|---|
| File discovery | 1 (main) | — | I/O walk, no DB |
| Analysis / parsing | `--workers` (ThreadPoolExecutor) | — | Thread-local parsers, no DB |
| Node ingestion | `--workers` (ThreadPoolExecutor) | `enable_multi_writes=true` | Each worker opens its own `Connection` |
| Definition edges | 1 (main) | Regular | `COPY CodeRelation` is DDL |
| Call resolution + edges | 1 (main) | Regular | Single-pass name index, then DDL COPY |
| Queries | 1 (main) | Read-only | Single `MATCH` queries |

`--workers` defaults to `min(32, cpu_count() + 4)`.  It controls both
the analysis threads (phase 2) and the ingest threads (phase 3).  When
`--workers=1`, phases 2 and 3 also run single-threaded (the
`ThreadPoolExecutor` is bypassed and the tqdm progress shows chunks
instead of per-file).

The entire tool is **single-process** — it uses Python threads only,
never `multiprocessing`.  Threads are appropriate because tree-sitter
parsing is CPU-bound C extension work (the GIL is released) and the
ladybug client library is also I/O-bound on IPC with the database
process.

---

## Usage

### Indexing

```
uv run python3 main.py --index [PATH ...] [--language LANG] [--workers N] [--db DB] [--schema SCHEMA]
```

- `PATH` — one or more files or directories (default: current directory).
- `--language` / `-l` — restrict to a single language (default: all installed).
- `--workers` — thread count for analysis and node ingestion (default: CPU-based).
- `--db` / `-d` — Ladybug database file (default: `test.db`).
- `--schema` / `-s` — schema `.cypher` file (default: `schema.cypher` alongside `main.py`).

Example:

```
$ uv run python3 main.py --index icebug-format --language python --workers 4
Created schema from .../lscope/schema.cypher
Analyzing: 100%|████████████| 9/9 [00:00<00:00, 10.12 file/s]
Ingesting: 100%|████████████| 4/4 [00:00<00:00,  8.54 chunk/s]

Ingested 9 file(s), 90 semantic node(s), and 149 resolved call(s) into test.db using 4 analysis thread(s)
  python: 9 file(s)

$ du -sh test.db
832K    test.db
```

### Searching

```
uv run python3 main.py --find-functions REGEX [--db DB]
uv run python3 main.py --find-callers NAME [--db DB]
```

Example:

```
$ uv run python3 main.py --find-functions 'main'
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
$ uv run python3 main.py --find-callers 'format_gb'
Callers of 'format_gb':
shape: (2, 6)
┌──────────────────────┬─────────────┬───────────┬───────────┬────────────┬──────────────────┐
│ caller               ┆ caller_kind ┆ file_path ┆ callee    ┆ confidence ┆ reason           │
╞══════════════════════╪═════════════╪═══════════╪═══════════╪════════════╪══════════════════╡
│ default_memory_limit ┆ Function    ┆ ...       ┆ format_gb ┆ 1.0        ┆ call at ...:129  │
│ parse_memory_limit   ┆ Function    ┆ ...       ┆ format_gb ┆ 1.0        ┆ call at ...:139  │
└──────────────────────┴─────────────┴───────────┴───────────┴────────────┴──────────────────┘
```

### Schema only

```
uv run python3 main.py --schema-only --schema SCHEMA --db DB
```

Applies the schema file to the database without indexing any files.
Useful for preparing an empty database ahead of time.

---

## Supported languages

Python, JavaScript, TypeScript, Java, C#, C++, Go, Rust, Swift, and
Kotlin — whenever the corresponding tree-sitter grammar package is
installed.

---

## Schema

The full schema is defined in [`schema.cypher`](./schema.cypher).  It
defines node tables for:

- **File**, **Folder** — filesystem hierarchy.
- **Function**, **Method** — callable declarations.
- **Class**, **Struct**, **Interface**, **Trait**, **Impl** — type/container
  declarations.
- **Enum**, **Property**, **CodeElement** — additional code entities.
- **Route**, **Tool** — framework-level metadata (routes, external tools).
- **Community**, **Process** — higher-level grouping nodes (for future
  analysis passes).

All relationships are stored in a single `CodeRelation` relationship
table group, with a `type` property indicating the relationship kind
(e.g. `CALLS`, `DEFINES`, `HAS_METHOD`, `CONTAINS`, `IMPORTS`).
