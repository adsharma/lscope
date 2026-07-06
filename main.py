"""
lscope — extract a semantic code graph (via tree-sitter) into a Ladybug DB
and run lightweight code-intelligence queries (find functions / callers).

A single invocation does exactly one of these jobs:

* **index**  — parse one file, several files, or a whole project tree and store
  the resulting semantic graph in a Ladybug database.
* **query**  — run a find-functions or find-callers search against an existing
  database.

These two jobs are mutually exclusive: you either write to the DB or you read
from it, never both in one invocation.  `argparse` is tightened so that
non-sensical flag combinations are rejected up-front with a clear message.
"""

import argparse
import os
import sys
import tempfile
import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

import ladybug
import pyarrow as pa
import pyarrow.parquet as pq
from tree_sitter import Language, Parser


# --------------------------------------------------------------------------- #
# Language registry
# --------------------------------------------------------------------------- #
# A language is supported when its `tree_sitter_<lang>` package is importable.
# Each entry maps the public name -> (import path, callable returning the
# language capsule, file extensions).  Extensions are lower-cased and compared
# without the leading dot.
_LANGUAGE_SPECS = {
    "rust": ("tree_sitter_rust", "language", ("rs",)),
    "python": ("tree_sitter_python", "language", ("py", "pyi")),
    "javascript": (
        "tree_sitter_javascript",
        "language",
        ("js", "jsx", "mjs", "cjs"),
    ),
    "typescript": (
        "tree_sitter_typescript",
        "language_typescript",
        ("ts", "tsx", "mts", "cts"),
    ),
    "java": ("tree_sitter_java", "language", ("java",)),
    "csharp": ("tree_sitter_c_sharp", "language", ("cs",)),
    "cpp": (
        "tree_sitter_cpp",
        "language",
        ("cc", "cpp", "cxx", "h", "hh", "hpp", "hxx"),
    ),
    "go": ("tree_sitter_go", "language", ("go",)),
    "swift": ("tree_sitter_swift", "language", ("swift",)),
    "kotlin": ("tree_sitter_kotlin", "language", ("kt", "kts")),
}


def supported_languages() -> list[str]:
    """Names of languages whose tree-sitter grammar is installed."""
    import importlib

    out = []
    for name, (module_name, _func, _exts) in _LANGUAGE_SPECS.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            continue
        out.append(name)
    return out


class LanguageRegistry:
    """Lazily build tree-sitter ``Parser`` objects per language on demand."""

    def __init__(self) -> None:
        import importlib

        self._parsers: dict[str, Parser] = {}
        self._ext_map: dict[str, str] = {}
        for name, (module_name, func, exts) in _LANGUAGE_SPECS.items():
            try:
                mod = importlib.import_module(module_name)
            except ImportError:
                continue
            lang = Language(getattr(mod, func)())
            parser = Parser()
            parser.language = lang
            self._parsers[name] = parser
            for ext in exts:
                self._ext_map[ext.lower().lstrip(".")] = name

    @property
    def languages(self) -> list[str]:
        return sorted(self._parsers)

    def language_for_path(self, path: str) -> str | None:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        return self._ext_map.get(ext)

    def parser_for(self, language: str) -> Parser:
        try:
            return self._parsers[language]
        except KeyError:
            available = ", ".join(self.languages) or "<none installed>"
            raise SystemExit(
                f"Unsupported language {language!r}. "
                f"Installed grammars: {available}."
            )

    def parser_for_path(self, path: str) -> Parser | None:
        lang = self.language_for_path(path)
        return self._parsers[lang] if lang else None


_worker_state = threading.local()


def _worker_registry() -> LanguageRegistry:
    """Return a parser registry owned by the current analysis thread."""
    registry = getattr(_worker_state, "registry", None)
    if registry is None:
        registry = LanguageRegistry()
        _worker_state.registry = registry
    return registry


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #
# Directory subtrees that are never interesting source trees to index.
_DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".pixi",
    ".tox",
    "dist",
    "build",
    ".eggs",
    ".cache",
}


def iter_source_files(
    targets: Iterable[str],
    *,
    language: str | None,
    registry: LanguageRegistry,
    ignore_dirs: set[str] = _DEFAULT_IGNORE_DIRS,
) -> Iterator[str]:
    """
    Yield absolute paths of source files to index.

    ``targets`` may be individual files or directories.  Directories are
    walked recursively, skipping ``ignore_dirs`` and files whose extension is
    not mapped to a known language (unless ``language`` is given, in which case
    only that language's extensions match).

    Non-existent paths raise SystemExit.
    """
    seen: set[str] = set()
    for target in targets:
        if not os.path.exists(target):
            raise SystemExit(f"Path not found: {target}")
        target = os.path.abspath(target)
        if os.path.isfile(target):
            # An explicit file is always indexed, but if --language was given
            # it must match: indexing a .rs file under --language python is a
            # non-sensical request.
            file_lang = registry.language_for_path(target)
            if file_lang is None:
                known = ", ".join(sorted(registry._ext_map)) or "<none>"
                raise SystemExit(
                    f"Cannot determine language for {target} "
                    f"(unknown extension). Known extensions: {known}."
                )
            if language is not None and file_lang != language:
                raise SystemExit(
                    f"File {target} is {file_lang!r} but --language is "
                    f"{language!r}."
                )
            files = [target]
        else:
            files = _walk_dir(target, language, registry, ignore_dirs)

        for f in files:
            if f in seen:
                continue
            seen.add(f)
            yield f


def _walk_dir(
    root: str,
    language: str | None,
    registry: LanguageRegistry,
    ignore_dirs: set[str],
) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored dirs in place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            ext = os.path.splitext(fname)[1].lstrip(".").lower()
            lang = registry._ext_map.get(ext)  # noqa: SLF001
            if lang is None:
                continue
            if language is not None and lang != language:
                continue
            yield full


# --------------------------------------------------------------------------- #
# Schema / DB helpers
# --------------------------------------------------------------------------- #
_REQUIRED_SCHEMA_TABLES = {"File", "Function", "Method", "Class", "CodeRelation"}
_REQUIRED_REL_CONNECTIONS = (
    ("Function", "Class"),
    ("Function", "Interface"),
    ("Function", "Struct"),
    ("Function", "Trait"),
    ("Function", "Impl"),
    ("Method", "Class"),
    ("Method", "Interface"),
    ("Method", "Struct"),
    ("Method", "Trait"),
    ("Method", "Impl"),
    ("Class", "Struct"),
    ("Class", "Impl"),
    ("Class", "Function"),
    ("Interface", "Class"),
    ("Interface", "Struct"),
    ("Interface", "Trait"),
    ("Interface", "Impl"),
    ("Struct", "Class"),
    ("Struct", "Struct"),
    ("Struct", "Impl"),
    ("Trait", "Class"),
    ("Trait", "Struct"),
    ("Trait", "Interface"),
    ("Trait", "Impl"),
    ("Impl", "Interface"),
    ("Impl", "Impl"),
)


def _default_schema_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.cypher")


def _load_schema_text(schema_path: str | None) -> str | None:
    """Read schema.cypher and normalise the unsupported hash-index directive."""
    if not schema_path:
        return None
    if not os.path.exists(schema_path):
        raise SystemExit(f"Schema file not found: {schema_path}")
    with open(schema_path, "r", encoding="utf8") as fh:
        text = fh.read()
    # Some schema files use the unsupported `CALL disable_default_hash_index=...`.
    # The supported equivalent is `CALL enable_default_hash_index = false`.
    lines = []
    for line in text.splitlines():
        if "disable_default_hash_index" in line:
            lines.append("CALL enable_default_hash_index = false;")
        else:
            lines.append(line)
    return "\n".join(lines)


def apply_schema(conn, schema_text: str | None) -> None:
    if schema_text is None:
        return
    try:
        conn.execute(schema_text)
    except Exception as e:  # noqa: BLE001
        # tolerate "table already exists" when re-indexing into an existing DB
        print("Warning: executing schema raised an error; continuing. Error:", e)


def ensure_schema(conn, schema_path: str | None = None) -> None:
    """Create the semantic schema when the database has no graph tables."""
    tables = conn.execute("CALL show_tables() RETURN name").get_as_pl()
    existing = set(tables["name"].to_list()) if tables.height else set()
    if _REQUIRED_SCHEMA_TABLES <= existing:
        for source, target in _REQUIRED_REL_CONNECTIONS:
            if source in existing and target in existing:
                conn.execute(
                    "ALTER TABLE CodeRelation ADD IF NOT EXISTS "
                    f"FROM {source} TO {target}"
                )
        return
    if existing:
        missing = ", ".join(sorted(_REQUIRED_SCHEMA_TABLES - existing))
        raise SystemExit(
            "Database contains a partial or incompatible schema; "
            f"missing required tables: {missing}."
        )

    selected_path = schema_path or _default_schema_path()
    schema_text = _load_schema_text(selected_path)
    assert schema_text is not None
    conn.execute(schema_text)
    print(f"Created schema from {selected_path}")


# --------------------------------------------------------------------------- #
# Semantic ingestion
# --------------------------------------------------------------------------- #
_CONTAINER_KINDS = {
    "class_definition": "Class",
    "class_declaration": "Class",
    "class_specifier": "Class",
    "object_declaration": "Class",
    "struct_declaration": "Struct",
    "struct_item": "Struct",
    "protocol_declaration": "Interface",
    "interface_declaration": "Interface",
    "trait_item": "Trait",
    "impl_item": "Impl",
}
_FUNCTION_KINDS = {
    "function_definition",
    "function_item",
    "function_declaration",
    "local_function_statement",
    "method_definition",
    "method_declaration",
    "constructor_declaration",
    "init_declaration",
}
_CALL_KINDS = {
    "call",
    "call_expression",
    "function_call",
    "invocation_expression",
    "method_invocation",
    "method_call",
    "macro_invocation",
}
_METHOD_KINDS = {
    "constructor_declaration",
    "init_declaration",
    "local_function_statement",
    "method_declaration",
    "method_definition",
}


def _node_text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf8")


def _field_text(node, field: str, source_bytes: bytes) -> str | None:
    child = node.child_by_field_name(field)
    return _node_text(child, source_bytes) if child is not None else None


def _declaration_name(node, source_bytes: bytes) -> str | None:
    """Extract names, following C/C++ nested declarators when necessary."""
    name = _field_text(node, "name", source_bytes)
    if name:
        return name
    declarator = node.child_by_field_name("declarator")
    while declarator is not None:
        nested = declarator.child_by_field_name("declarator")
        if nested is None:
            text = _node_text(declarator, source_bytes)
            return text.split("::")[-1]
        declarator = nested
    return None


def _called_name(node, source_bytes: bytes) -> str | None:
    """Return the terminal name of a Python/Rust call expression."""
    callee = (
        node.child_by_field_name("function")
        or node.child_by_field_name("name")
        or node.child_by_field_name("macro")
    )
    if callee is None and node.named_child_count:
        callee = node.named_children[0]
    if callee is None:
        return None

    for field in ("attribute", "field", "name"):
        terminal = callee.child_by_field_name(field)
        if terminal is not None:
            return _node_text(terminal, source_bytes).rstrip("!")
    return _node_text(callee, source_bytes).split("::")[-1].rstrip("!")


def analyze_file(path: str, language: str, source: str, parser: Parser) -> dict:
    """Extract semantic declarations and calls from one source file."""
    source_bytes = source.encode()
    tree = parser.parse(source_bytes)
    file_id = f"file:{path}"
    symbols: list[dict] = []
    calls: list[dict] = []

    def visit(node, owner: dict, type_owner: dict | None = None) -> None:
        next_owner = owner
        next_type = type_owner

        if node.type in _CONTAINER_KINDS:
            label = _CONTAINER_KINDS[node.type]
            if node.type == "class_specifier" and node.children:
                label = "Struct" if node.children[0].type == "struct" else "Class"
            name = (
                _field_text(node, "type", source_bytes)
                if label == "Impl"
                else _declaration_name(node, source_bytes)
            )
            if name:
                symbol = {
                    "id": f"{label.lower()}:{path}#{node.start_byte}",
                    "label": label,
                    "name": f"impl {name}" if label == "Impl" else name,
                    "qualified_name": name,
                    "file_path": path,
                    "language": language,
                    "start_line": node.start_point.row + 1,
                    "end_line": node.end_point.row + 1,
                    "owner": owner,
                    "relation": "DEFINES",
                }
                symbols.append(symbol)
                next_owner = symbol
                next_type = symbol

        elif node.type in _FUNCTION_KINDS:
            name = _declaration_name(node, source_bytes)
            if name:
                is_method = type_owner is not None or node.type in _METHOD_KINDS
                if node.child_by_field_name("receiver") is not None:
                    is_method = True
                label = "Method" if is_method else "Function"
                symbol = {
                    "id": f"{label.lower()}:{path}#{node.start_byte}",
                    "label": label,
                    "name": name,
                    "qualified_name": (
                        f"{type_owner['qualified_name']}.{name}"
                        if type_owner is not None
                        else name
                    ),
                    "file_path": path,
                    "language": language,
                    "start_line": node.start_point.row + 1,
                    "end_line": node.end_point.row + 1,
                    "owner": type_owner or owner,
                    "relation": "HAS_METHOD" if type_owner else "DEFINES",
                }
                symbols.append(symbol)
                next_owner = symbol
                next_type = None

        if node.type in _CALL_KINDS:
            name = _called_name(node, source_bytes)
            if name:
                calls.append(
                    {
                        "caller": owner,
                        "callee_name": name,
                        "file_path": path,
                        "line": node.start_point.row + 1,
                    }
                )

        for child in node.named_children:
            visit(child, next_owner, next_type)

    file_owner = {"id": file_id, "label": "File"}
    visit(tree.root_node, file_owner)
    return {
        "file": {
            "id": file_id,
            "name": os.path.basename(path),
            "path": path,
            "language": language,
        },
        "symbols": symbols,
        "calls": calls,
    }


def analyze_path(path: str) -> dict:
    """Read and analyze a file using a parser local to the worker thread."""
    registry = _worker_registry()
    language = registry.language_for_path(path)
    if language is None:
        raise ValueError(f"Cannot determine language for {path}")
    with open(path, "r", encoding="utf8") as fh:
        source = fh.read()
    return analyze_file(path, language, source, registry.parser_for(language))


def _collect_file_data(
    analysis: dict,
) -> tuple[
    dict,
    dict[str, list[dict]],
    int,
    list[tuple[dict, dict, str, float, str, int]],
]:
    """
    Extract node data and edges from one analysis without touching the DB.

    Returns ``(file_dict, symbols_by_label, node_count, edges)``.
    """
    file = analysis["file"]
    file_ref = {"id": file["id"], "label": "File"}

    by_label: dict[str, list[dict]] = {}
    for symbol in analysis["symbols"]:
        by_label.setdefault(symbol["label"], []).append(
            {
                "id": symbol["id"],
                "name": symbol["name"],
                "qualified_name": symbol["qualified_name"],
                "file_path": symbol["file_path"],
                "language": symbol["language"],
                "start_line": symbol["start_line"],
                "end_line": symbol["end_line"],
            }
        )

    edges: list[tuple[dict, dict, str, float, str, int]] = [
        (
            symbol.get("owner") or file_ref,
            symbol,
            symbol["relation"],
            1.0,
            "",
            0,
        )
        for symbol in analysis["symbols"]
    ]
    return file, by_label, 1 + len(analysis["symbols"]), edges


def ingest_calls(conn, analyses: list[dict]) -> int:
    """Resolve calls by name, preferring a declaration in the same file.

    Resolution stays a single global pass; only the writes are batched into one
    ``CREATE`` per ``(caller_label, target_label)`` pair via ``_ingest_chunk_edges``.
    """
    by_name: dict[str, list[dict]] = {}
    for analysis in analyses:
        for symbol in analysis["symbols"]:
            if symbol["label"] in {"Function", "Method"}:
                by_name.setdefault(symbol["name"], []).append(symbol)

    edges: list[tuple[dict, dict, str, float, str, int]] = []
    for analysis in analyses:
        for call in analysis["calls"]:
            candidates = by_name.get(call["callee_name"], [])
            if not candidates:
                continue
            target = next(
                (s for s in candidates if s["file_path"] == call["file_path"]),
                candidates[0],
            )
            edges.append(
                (
                    call["caller"],
                    target,
                    "CALLS",
                    1.0 if len(candidates) == 1 else 0.7,
                    f"call at {call['file_path']}:{call['line']}",
                    0,
                )
            )
    _ingest_chunk_edges(conn, edges)
    return len(edges)


def _ingest_chunk_edges(
    conn,
    all_edges: list[tuple[dict, dict, str, float, str, int]],
) -> None:
    """Bulk-insert all edges collected from a chunk of files.

    Edges are grouped by ``(source.label, target.label)``; each group
    is written to a single temporary Parquet file and loaded with
    ``COPY FROM`` — one bulk operation per label pair for the
    entire chunk instead of one per file.
    """
    if not all_edges:
        return

    by_pair: dict[tuple[str, str], list[dict]] = {}
    for source, target, rel_type, confidence, reason, step in all_edges:
        key = (source["label"], target["label"])
        by_pair.setdefault(key, []).append(
            {
                "src": source["id"],
                "dst": target["id"],
                "type": rel_type,
                "confidence": confidence,
                "reason": reason,
                "step": step,
            }
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        for (src_label, dst_label), rows in by_pair.items():
            path = os.path.join(tmpdir, f"{src_label}_{dst_label}.parquet")
            pq.write_table(
                pa.table(
                    {
                        "from": [r["src"] for r in rows],
                        "to": [r["dst"] for r in rows],
                        "type": [r["type"] for r in rows],
                        "confidence": [r["confidence"] for r in rows],
                        "reason": [r["reason"] for r in rows],
                        "step": [r["step"] for r in rows],
                    }
                ),
                path,
            )
            conn.execute(
                f"COPY CodeRelation FROM '{path}' "
                f"(FROM='{src_label}', TO='{dst_label}')"
            )


def _ingest_chunk_nodes(
    conn,
    all_files: list[dict],
    all_by_label: dict[str, list[dict]],
) -> None:
    """Bulk-insert every node (files + symbols) for a whole chunk.

    One ``UNWIND`` + ``MERGE`` per label across every file in the
    chunk, wrapped in a single transaction — instead of one
    transaction + one query per label **per file**.
    """
    conn.execute("BEGIN TRANSACTION")
    if all_files:
        conn.execute(
            "UNWIND $rows AS row "
            "MERGE (f:File {id: row.id}) "
            "SET f.name = row.name, f.path = row.path, "
            "f.filePath = row.filePath, f.language = row.language",
            {"rows": all_files},
        )
    for label, rows in all_by_label.items():
        conn.execute(
            f"UNWIND $rows AS row "
            f"MERGE (n:{label} {{id: row.id}}) "
            "SET n.name = row.name, "
            "n.qualifiedName = row.qualified_name, "
            "n.filePath = row.file_path, "
            "n.language = row.language, "
            "n.startLine = row.start_line, "
            "n.endLine = row.end_line",
            {"rows": rows},
        )
    conn.execute("COMMIT")


def ingest_analyses_parallel(
    db, analyses: list[dict], workers: int
) -> int:
    """
    Ingest nodes + DEFINES/HAS_METHOD edges for many files in parallel.

    Each worker thread owns its own ``Connection`` to the shared ``Database``
    (opened with ``enable_multi_writes=True`` so concurrent write transactions
    are permitted).  Analyses are split round-robin into ``workers`` chunks so
    per-file node/edge batches stay independent and need no cross-thread
    coordination.  Returns the total number of nodes written.
    """
    if not analyses:
        return 0
    workers = max(1, min(workers, len(analyses)))
    total = 0

    def _run(chunk: list[dict]) -> int:
        """Collect node data from every file in *chunk*, bulk-insert
        all nodes, then bulk-insert all definition edges."""
        conn = ladybug.Connection(db)
        try:
            all_files: list[dict] = []
            all_by_label: dict[str, list[dict]] = {}
            all_edges: list[
                tuple[dict, dict, str, float, str, int]
            ] = []
            chunk_total = 0
            for a in chunk:
                file_ref, by_label, n, edges = _collect_file_data(a)
                all_files.append(file_ref)
                chunk_total += n
                all_edges.extend(edges)
                for label, rows in by_label.items():
                    all_by_label.setdefault(label, []).extend(rows)
            _ingest_chunk_nodes(conn, all_files, all_by_label)
            _ingest_chunk_edges(conn, all_edges)
            return chunk_total
        finally:
            conn.close()

    chunks: list[list[dict]] = [[] for _ in range(workers)]
    for i, analysis in enumerate(analyses):
        chunks[i % workers].append(analysis)

    if workers == 1:
        for c in tqdm(chunks, desc="Ingesting", unit="chunk"):
            total += _run(c)
        return total

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lscope-ingest"
    ) as executor:
        for n in executor.map(_run, chunks):
            total += n
    return total


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def _run_query(conn, query: str, params: dict | None = None):
    qr = conn.execute(query, params or {})
    return qr.get_as_pl()


def find_functions(conn, pattern: str, languages: list[str] | None = None):
    """Return semantic Function and Method nodes matching ``pattern``."""
    del languages  # reserved for future per-language filtering
    frames = []
    for label in ("Function", "Method"):
        frames.append(
            _run_query(
                conn,
                f"""
                MATCH (fn:{label})
                WHERE fn.name =~ $pattern
                RETURN fn.name AS name,
                       '{label}' AS kind,
                       fn.filePath AS file_path,
                       fn.startLine AS start_line,
                       fn.endLine AS end_line
                """,
                {"pattern": pattern},
            )
        )
    return pl.concat(frames, how="vertical")


def find_callers(conn, func_name: str):
    """Return semantic nodes with a CALLS edge to a named function or method."""
    frames = []
    for caller_label in ("File", "Function", "Method"):
        for target_label in ("Function", "Method"):
            frames.append(
                _run_query(
                    conn,
                    f"""
                    MATCH (caller:{caller_label})-[r:CodeRelation]->(
                        target:{target_label}
                    )
                    WHERE r.type = 'CALLS' AND target.name = $func_name
                    RETURN caller.name AS caller,
                           '{caller_label}' AS caller_kind,
                           caller.filePath AS file_path,
                           target.name AS callee,
                           r.confidence AS confidence,
                           r.reason AS reason
                    """,
                    {"func_name": func_name},
                )
            )
    return pl.concat(frames, how="vertical")


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    installed = supported_languages()
    p = argparse.ArgumentParser(
        prog="lscope",
        description=(
            "Index a semantic code graph into a Ladybug DB and run code queries. "
            "Exactly one of --index / --find-functions / --find-callers is "
            "required."
        ),
    )
    p.add_argument(
        "--db",
        "-d",
        default="test.db",
        help="Ladybug database file (default: %(default)s)",
    )
    p.add_argument(
        "--schema",
        "-s",
        default=None,
        help="Schema file used to initialize an empty database",
    )

    index_grp = p.add_argument_group("indexing")
    index_grp.add_argument(
        "--index",
        "--ingest",
        dest="index_targets",
        nargs="*",
        metavar="PATH",
        default=None,
        help=(
            "Index one or more files or directories. "
            "Use without a value to index the current directory."
        ),
    )
    index_grp.add_argument(
        "--language",
        "-l",
        choices=installed or ["rust", "python"],
        default=None,
        help="Restrict indexing to a single language (default: all installed).",
    )
    index_grp.add_argument(
        "--workers",
        type=int,
        default=min(32, (os.cpu_count() or 1) + 4),
        help=(
            "Threads for both file analysis and DB ingestion "
            "(default: %(default)s)."
        ),
    )

    query_grp = p.add_argument_group("queries")
    query_grp.add_argument(
        "--find-functions",
        dest="find_functions",
        default=None,
        metavar="REGEX",
        help="Regex (Cypher =~) to find function/method names",
    )
    query_grp.add_argument(
        "--find-callers",
        dest="find_callers",
        default=None,
        metavar="NAME",
        help="Function name to find call sites for (exact match on identifier text)",
    )
    p.add_argument(
        "--schema-only",
        action="store_true",
        help="Apply the schema file to the DB and exit (no index, no query).",
    )
    return p


def _resolve_actions(args: argparse.Namespace) -> str:
    """
    Decide which job this invocation runs.  Returns one of:
    'index', 'find-functions', 'find-callers', 'schema-only'.

    Raises SystemExit for empty / conflicting combinations.
    """
    requested = []
    if args.index_targets is not None:
        requested.append("index")
    if args.find_functions is not None:
        requested.append("find-functions")
    if args.find_callers is not None:
        requested.append("find-callers")
    if args.schema_only:
        requested.append("schema-only")

    if len(requested) == 0:
        raise SystemExit(
            "Nothing to do. Specify exactly one of "
            "--index, --find-functions, --find-callers, or --schema-only."
        )
    if len(requested) > 1:
        raise SystemExit(
            "Conflicting flags: at most one of "
            "--index, --find-functions, --find-callers, --schema-only "
            "may be given (got: " + ", ".join(requested) + ")."
        )
    return requested[0]


# --------------------------------------------------------------------------- #
# Command runners
# --------------------------------------------------------------------------- #
def _open_db(db_path: str, *, read_only: bool = False, multi_writes: bool = False):
    db = ladybug.Database(db_path, read_only=read_only, enable_multi_writes=multi_writes)
    conn = ladybug.Connection(db)
    return db, conn


def run_index(args: argparse.Namespace) -> int:
    targets = args.index_targets or ["."]
    registry = LanguageRegistry()
    if not registry.languages:
        raise SystemExit(
            "No tree-sitter grammar is installed. "
            "Install one of: " + ", ".join(_LANGUAGE_SPECS) + "."
        )

    files = list(
        iter_source_files(
            targets, language=args.language, registry=registry
        )
    )
    if not files:
        where = "the given paths" if args.language is None else f"{args.language} files"
        print(f"No indexable source files found in {where}.")
        return 0
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1.")

    db, conn = _open_db(args.db, multi_writes=True)
    try:
        ensure_schema(conn, args.schema)
        worker_count = min(args.workers, len(files))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="lscope-analyze",
        ) as executor:
            analyses = list(executor.map(analyze_path, files))

        per_lang: dict[str, int] = {}
        for i, analysis in enumerate(analyses, 1):
            path = analysis["file"]["path"]
            lang = analysis["file"]["language"]
            per_lang[lang] = per_lang.get(lang, 0) + 1
            print(
                f"[{i}/{len(files)}] {lang}: {path} "
                f"({len(analysis['symbols'])} symbols)"
            )

        print("analysis starting")
        # Nodes + DEFINES/HAS_METHOD edges: parallelized across worker threads,
        # each with its own connection to the shared multi-write database.
        total_nodes = ingest_analyses_parallel(db, analyses, worker_count)
        print("analysis done")
        # Calls need every node already written and a global name index, so they
        # stay single-threaded on the main connection.
        call_count = ingest_calls(conn, analyses)
        print(
            f"\nIngested {len(files)} file(s), {total_nodes} semantic node(s), "
            f"and {call_count} resolved call(s) into {args.db} "
            f"using {worker_count} analysis thread(s)"
        )
        for lang, count in sorted(per_lang.items()):
            print(f"  {lang}: {count} file(s)")
    finally:
        print("executing finally")
        conn.close()
        print("close done finally")
        db.close()
    return 0


def run_find_functions(args: argparse.Namespace) -> int:
    db, conn = _open_db(args.db, read_only=True)
    try:
        df = find_functions(conn, args.find_functions)
        print(f"Functions matching /{args.find_functions}/:")
        print(df)
    finally:
        conn.close()
        db.close()
    return 0


def run_find_callers(args: argparse.Namespace) -> int:
    db, conn = _open_db(args.db, read_only=True)
    try:
        df = find_callers(conn, args.find_callers)
        print(f"Callers of {args.find_callers!r}:")
        print(df)
    finally:
        conn.close()
        db.close()
    return 0


def run_schema_only(args: argparse.Namespace) -> int:
    if args.schema is None:
        raise SystemExit("--schema-only requires --schema/-s.")
    schema_text = _load_schema_text(args.schema)
    db, conn = _open_db(args.db)
    try:
        apply_schema(conn, schema_text)
        print(f"Applied schema from {args.schema} to {args.db}")
    finally:
        conn.close()
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    action = _resolve_actions(args)

    if action == "index":
        return run_index(args)
    if action == "find-functions":
        return run_find_functions(args)
    if action == "find-callers":
        return run_find_callers(args)
    if action == "schema-only":
        return run_schema_only(args)
    parser.error(f"Unhandled action {action!r}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
