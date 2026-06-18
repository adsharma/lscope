CALL disable_default_hash_index=true;
CREATE NODE TABLE File (
    path        STRING,
    language    STRING,
    source      STRING,     // full file contents, used to recover text by offset
    PRIMARY KEY (path)
);

CREATE NODE TABLE AstNode (
    id                 STRING,   // "<file_path>#<preorder_index>", see below
    kind               STRING,   // tree-sitter node type, e.g. "call_expression"
    is_named           BOOLEAN,  // tree-sitter's named vs anonymous (punctuation/keywords)
    is_leaf            BOOLEAN,  // child_count == 0
    is_error           BOOLEAN,  // ERROR node
    is_missing         BOOLEAN,  // MISSING node (error recovery)
    is_extra           BOOLEAN,  // tree-sitter "extra" node (e.g. comments)
    start_byte         INT64,
    end_byte           INT64,
    start_row          INT64,
    start_col          INT64,
    end_row            INT64,
    end_col            INT64,
    child_count        INT64,
    named_child_count  INT64,
    text               STRING,  // populated for leaves only; NULL for internal nodes
    PRIMARY KEY (id)
);

CREATE REL TABLE CHILD (
    FROM AstNode TO AstNode,
    field              STRING,  // grammar field name ("name", "body", ...) or '' if none
    child_index        INT64,   // 0-based position among ALL children
    named_child_index  INT64    // 0-based position among NAMED children, -1 if not named
);

CREATE REL TABLE ROOT_OF (
    FROM File TO AstNode        // points File -> its translation_unit/program root
);
