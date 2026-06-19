"""
lscope — ingest source code ASTs (via tree-sitter) into a ladybug graph DB
and run lightweight code-intelligence queries (find functions / callers).

A single invocation does exactly one of these jobs:

* **index**  — parse one file, several files, or a whole project tree and store
  the resulting ASTs in a ladybug database.
* **query**  — run a find-functions or find-callers search against an existing
  database.

These two jobs are mutually exclusive: you either write to the DB or you read
from it, never both in one invocation.  `argparse` is tightened so that
non-sensical flag combinations are rejected up-front with a clear message.
"""

import argparse
import os
import sys
from collections.abc import Iterable, Iterator

import ladybug
import polars as pl
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


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def ingest_file(conn, path: str, language: str, source: str, parser: Parser) -> int:
    """
    Parse one file with ``parser`` and write its AST into ``conn``.

    Returns the number of AST nodes written.

    The node id scheme is ``"<path>#<preorder_index>"``.  Re-ingesting the same
    file re-uses the same ids (via MERGE), so a single DB can be incrementally
    updated.
    """
    tree = parser.parse(source.encode())

    conn.execute("BEGIN TRANSACTION")
    conn.execute(
        "MERGE (f:File {path: $path}) SET f.language = $lang, f.source = $src",
        {"path": path, "lang": language, "src": source},
    )

    nodes = []
    edges = []
    counter = 0

    def visit(node, parent_id=None, field=None, child_index=0, named_index=-1):
        nonlocal counter
        node_id = f"{path}#{counter}"
        counter += 1
        is_leaf = node.child_count == 0
        nodes.append(
            {
                "id": node_id,
                "kind": node.type,
                "is_named": bool(getattr(node, "is_named", True)),
                "is_leaf": is_leaf,
                "is_error": bool(getattr(node, "is_error", False)),
                "is_missing": bool(getattr(node, "is_missing", False)),
                "is_extra": bool(getattr(node, "is_extra", False)),
                "start_byte": int(getattr(node, "start_byte", 0)),
                "end_byte": int(getattr(node, "end_byte", 0)),
                "start_row": int(getattr(node, "start_point", (0, 0))[0]),
                "start_col": int(getattr(node, "start_point", (0, 0))[1]),
                "end_row": int(getattr(node, "end_point", (0, 0))[0]),
                "end_col": int(getattr(node, "end_point", (0, 0))[1]),
                "child_count": int(getattr(node, "child_count", 0)),
                "named_child_count": int(getattr(node, "named_child_count", 0)),
                "text": (
                    source[
                        getattr(node, "start_byte", 0) : getattr(node, "end_byte", 0)
                    ]
                    if is_leaf
                    else None
                ),
            }
        )
        if parent_id is not None:
            edges.append(
                {
                    "from": parent_id,
                    "to": node_id,
                    "field": field or "",
                    "child_index": child_index,
                    "named_child_index": named_index,
                }
            )
        named_i = 0
        for i, child in enumerate(getattr(node, "children", [])):
            try:
                f_name = node.field_name_for_child(i)
            except Exception:
                f_name = None
            visit(
                child,
                node_id,
                f_name,
                i,
                named_i if getattr(child, "is_named", False) else -1,
            )
            if getattr(child, "is_named", False):
                named_i += 1
        return node_id

    root_id = visit(tree.root_node)

    for n in nodes:
        params = {
            "id": n["id"],
            "kind": n["kind"],
            "is_named": n["is_named"],
            "is_leaf": n["is_leaf"],
            "is_error": n["is_error"],
            "is_missing": n["is_missing"],
            "is_extra": n["is_extra"],
            "start_byte": n["start_byte"],
            "end_byte": n["end_byte"],
            "start_row": n["start_row"],
            "start_col": n["start_col"],
            "end_row": n["end_row"],
            "end_col": n["end_col"],
            "child_count": n["child_count"],
            "named_child_count": n["named_child_count"],
            "text": n["text"],
        }
        conn.execute(
            "MERGE (nd:AstNode {id: $id}) SET nd.kind = $kind, nd.is_named = $is_named, nd.is_leaf = $is_leaf, "
            "nd.is_error = $is_error, nd.is_missing = $is_missing, nd.is_extra = $is_extra, "
            "nd.start_byte = $start_byte, nd.end_byte = $end_byte, nd.start_row = $start_row, nd.start_col = $start_col, "
            "nd.end_row = $end_row, nd.end_col = $end_col, nd.child_count = $child_count, nd.named_child_count = $named_child_count, "
            "nd.text = $text",
            params,
        )

    for e in edges:
        conn.execute(
            "MATCH (a:AstNode {id: $from}), (b:AstNode {id: $to}) "
            "CREATE (a)-[:CHILD {field: $field, child_index: $child_index, "
            "named_child_index: $named_child_index}]->(b)",
            e,
        )

    conn.execute(
        "MATCH (f:File {path: $path}), (r:AstNode {id: $rid}) CREATE (f)-[:ROOT_OF]->(r)",
        {"path": path, "rid": root_id},
    )
    conn.execute("COMMIT")

    # clean up any stale edges if this file was previously ingested
    _prune_stale_file_edges(conn, path, root_id)
    return len(nodes)


def _prune_stale_file_edges(conn, path: str, current_root_id: str) -> None:
    """Best-effort: when re-indexing, drop dangling ROOT_OF edges for old roots."""
    try:
        conn.execute(
            "MATCH (f:File {path: $path})-[r:ROOT_OF]->(old) "
            "WHERE old.id <> $rid DELETE r",
            {"path": path, "rid": current_root_id},
        )
    except Exception:
        # not all cypher dialects support this; failure is non-fatal
        pass


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
# Node kinds that represent a function/method declaration, kept broad so a
# single query works across languages:
#   python: function_definition
#   rust:   function_item
#   javascript/typescript/go: function_declaration / method_declaration
_FUNCTION_KINDS = [
    "function_definition",
    "function_item",
    "function_declaration",
    "method_definition",
    "method_declaration",
]
# Node kinds that represent a call.  Both python's `call` and rust's
# `call_expression` / `macro_invocation` (for macro-style calls) are covered.
_CALL_KINDS = [
    "call",
    "call_expression",
    "function_call",
    "method_call",
    "macro_invocation",
]


def _run_query(conn, query: str, params: dict | None = None):
    qr = conn.execute(query, params or {})
    return qr.get_as_pl()


def find_functions(conn, pattern: str, languages: list[str] | None = None):
    """
    Return function/method definitions whose name matches ``pattern``.

    The function name is the named child whose grammar field is ``name``;
    we additionally fall back to any leaf ``identifier`` child so this is
    robust across grammars.  ``pattern`` is a regex applied via Cypher ``=~``.
    """
    del languages  # reserved for future per-language filtering
    cy = f"""
    MATCH (fn)-[:CHILD]->(name:AstNode)
    WHERE name.text =~ $pattern
      AND fn.kind IN {_FUNCTION_KINDS!s}
    RETURN name.text AS name_text,
           fn.kind AS kind,
           fn.start_row AS start_row,
           fn.start_col AS start_col;
    """
    return _run_query(conn, cy, {"pattern": pattern})


def find_callers(conn, func_name: str):
    """
    Return call sites of ``func_name``.

    Matches a call whose directly-called expression is either:

    * an ``identifier`` whose text equals ``func_name``  (e.g. ``foo()``), or
    * an ``attribute`` whose name child equals ``func_name`` (e.g.
      ``obj.method()`` or ``Cls.method()``).

    Macro invocations (rust) are matched when the macro name equals
    ``func_name``.
    """
    call_kinds = _CALL_KINDS
    cy = """
    MATCH (call:AstNode)-[:CHILD]->(callee)
    WHERE call.kind IN """ + f"{call_kinds!s}" + """
      AND (
        callee.text = $func_name
        OR (
          callee.kind = 'attribute'
          AND EXISTS {
            MATCH (callee)-[:CHILD]->(an:AstNode)
            WHERE an.text = $func_name
          }
        )
      )
    RETURN call.id AS call_id,
           call.kind AS call_kind,
           call.start_row AS start_row,
           call.start_col AS start_col;
    """
    df = _run_query(conn, cy, {"func_name": func_name})
    if df.height:
        df = df.with_columns(
            [pl.col("call_id").str.split("#").list.get(0).alias("file")]
        )
    return df


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    installed = supported_languages()
    p = argparse.ArgumentParser(
        prog="lscope",
        description=(
            "Index source code ASTs into a ladybug DB and run code queries. "
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
        help="Schema cypher file to execute (index) before ingest",
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
def _open_db(db_path: str, *, read_only: bool = False):
    db = ladybug.Database(db_path, read_only=read_only)
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

    db, conn = _open_db(args.db)
    try:
        apply_schema(conn, _load_schema_text(args.schema))
        total_nodes = 0
        per_lang: dict[str, int] = {}
        for i, path in enumerate(files, 1):
            lang = registry.language_for_path(path)
            assert lang is not None  # filtered by iter_source_files
            parser = registry.parser_for(lang)
            with open(path, "r", encoding="utf8") as fh:
                source = fh.read()
            n = ingest_file(conn, path, lang, source, parser)
            total_nodes += n
            per_lang[lang] = per_lang.get(lang, 0) + 1
            print(f"[{i}/{len(files)}] {lang}: {path} ({n} nodes)")
        print(
            f"\nIngested {len(files)} file(s) / {total_nodes} AST node(s) "
            f"into {args.db}"
        )
        for lang, count in sorted(per_lang.items()):
            print(f"  {lang}: {count} file(s)")
    finally:
        conn.close()
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
