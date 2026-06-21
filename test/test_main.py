import ladybug

from main import LanguageRegistry, analyze_file, ensure_schema, ingest_analysis


NESTED_TYPES = """\
def outer():
    class Inner:
        pass

class Outer:
    def method(self):
        class Local:
            pass
"""


def _analyze_nested_types():
    registry = LanguageRegistry()
    return analyze_file(
        "/tmp/nested.py",
        "python",
        NESTED_TYPES,
        registry.parser_for("python"),
    )


def test_ingests_classes_nested_in_functions_and_methods(tmp_path):
    db = ladybug.Database(str(tmp_path / "nested.lbug"))
    conn = ladybug.Connection(db)
    try:
        ensure_schema(conn)
        analysis = _analyze_nested_types()

        assert ingest_analysis(conn, analysis) == 6
        rows = conn.execute(
            """
            MATCH (owner)-[r:CodeRelation]->(nested:Class)
            WHERE nested.name IN ['Inner', 'Local']
            RETURN label(owner) AS owner_label, nested.name AS nested_name
            ORDER BY nested_name
            """
        ).get_as_pl()
        assert rows.to_dicts() == [
            {"owner_label": "Function", "nested_name": "Inner"},
            {"owner_label": "Method", "nested_name": "Local"},
        ]
    finally:
        conn.close()
        db.close()


def test_ensure_schema_migrates_existing_relation_group(tmp_path):
    db = ladybug.Database(str(tmp_path / "migration.lbug"))
    conn = ladybug.Connection(db)
    try:
        ensure_schema(conn)
        conn.execute(
            "ALTER TABLE CodeRelation DROP FROM Function TO Class; "
            "ALTER TABLE CodeRelation DROP FROM Method TO Class"
        )

        ensure_schema(conn)

        assert ingest_analysis(conn, _analyze_nested_types()) == 6
    finally:
        conn.close()
        db.close()
