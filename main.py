import argparse
import os

import ladybug
import pyarrow
from tree_sitter import Language, Parser
import tree_sitter_rust as ts_rust
import polars as pl


def ingest_file(conn, path, language, source, parser):
    tree = parser.parse(source.encode())

    # create/merge File node
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
            f_name = None
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

    # insert ast nodes (include file property for ease of querying)
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

    # insert child edges
    for e in edges:
        conn.execute(
            "MATCH (a:AstNode {id: $from}), (b:AstNode {id: $to}) CREATE (a)-[:CHILD {field: $field, child_index: $child_index, named_child_index: $named_child_index}]->(b)",
            e,
        )

    # create root relationship
    conn.execute(
        "MATCH (f:File {path: $path}), (r:AstNode {id: $rid}) CREATE (f)-[:ROOT_OF]->(r)",
        {"path": path, "rid": root_id},
    )


def main():
    p = argparse.ArgumentParser(
        description="Parse a file and ingest AST into a ladybug DB"
    )
    p.add_argument("--file", "-f", default=None, help="Source file to parse")
    p.add_argument(
        "--db", "-d", default="test.db", help="Ladybug database file to write to"
    )
    p.add_argument(
        "--schema",
        "-s",
        default=None,
        help="Schema cypher file to execute before ingest",
    )
    p.add_argument(
        "--find-functions",
        dest="find_functions",
        help="Regex pattern (Cypher =~) to find function names",
        default=None,
    )
    p.add_argument(
        "--find-callers",
        dest="find_callers",
        help="Function name to search for call sites (exact match on func.text)",
        default=None,
    )
    args = p.parse_args()

    src_path = args.file
    db_path = args.db
    schema_path = args.schema

    if src_path:
        if not os.path.exists(src_path):
            raise SystemExit(f"Source file not found: {src_path}")

        with open(src_path, "r", encoding="utf8") as fh:
            source = fh.read()

    # create/open ladybug database
    db = ladybug.Database(db_path)
    conn = ladybug.Connection(db)

    # apply schema if provided
    if schema_path and os.path.exists(schema_path):
        with open(schema_path, "r", encoding="utf8") as fh:
            schema = fh.read()
        # Some schema files may attempt to call an unsupported option like:
        #   CALL disable_default_hash_index=true;
        # The correct supported setting is to set enable_default_hash_index = false.
        # Replace such lines with a SET statement before executing.
        lines = []
        for L in schema.splitlines():
            if "disable_default_hash_index" in L:
                # replace the unsupported option with the supported CALL to set the flag false
                lines.append("CALL enable_default_hash_index = false;")
            else:
                lines.append(L)
        schema = "\n".join(lines)

        # execute schema statements; ladybug accepts a cypher-like script
        try:
            conn.execute(schema)
        except Exception as e:
            # ignore errors like "File already exists in catalog" when re-running
            print("Warning: executing schema raised an error; continuing. Error:", e)

    # create parser using tree_sitter_rust
    RUST = Language(ts_rust.language())
    parser = Parser()
    # assign language object to parser (some tree_sitter distributions expect attribute assignment)
    parser.language = RUST

    if src_path:
        ingest_file(conn, src_path, "rust", source, parser)
        print(f"Ingested {src_path} into {db_path}")

    # helper to run cypher and return polars DataFrame
    def run_query_as_pl(query: str, params: dict | None = None):
        params = params or {}
        qr = conn.execute(query, params)
        return qr.get_as_pl()

    # optional simple queries
    if args.find_functions:
        pattern = args.find_functions
        cy = """
        MATCH (fn)-[r_name:CHILD]->(name:AstNode), (fn)-[r_body:CHILD]->(body:AstNode)
        WHERE name.text =~ $pattern AND fn.kind IN ['function_declaration', 'function_item']
        RETURN fn, name.text AS name_text, body.start_byte AS body_start_byte;
        """
        df = run_query_as_pl(cy, {"pattern": pattern})
        print("Functions matching pattern:")
        print(df)

    if args.find_callers:
        func = args.find_callers
        cy = """
        MATCH (call:AstNode)-[r:CHILD]->(func)
        WHERE func.text = $func_name
        AND call.kind IN ['call_expression', 'function_call', 'method_call']
        RETURN call.id AS call_id, call.start_row AS start_row, call.start_col AS start_col;
        """
        df = run_query_as_pl(cy, {"func_name": func})
        # add file column from call_id "<path>#<index>"
        df = df.with_columns(
            [pl.col("call_id").str.split("#").list.get(0).alias("file")]
        )
        print("Callers of function:", func)
        print(df)


if __name__ == "__main__":
    main()
