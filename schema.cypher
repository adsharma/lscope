CALL disable_default_hash_index=true;

// Semantic code-graph schema.
//
// Every relationship is stored in the CodeRelation relationship-table group.
// The `type` property contains one of:
//   CONTAINS, DEFINES, CALLS, IMPORTS, EXTENDS, IMPLEMENTS, HAS_METHOD,
//   HAS_PROPERTY, ACCESSES, METHOD_OVERRIDES, METHOD_IMPLEMENTS, MEMBER_OF,
//   STEP_IN_PROCESS, HANDLES_ROUTE, FETCHES, HANDLES_TOOL, ENTRY_POINT_OF.
//
// For ACCESSES, `reason` records the access mode (for example "read" or
// "write"). For STEP_IN_PROCESS, `step` records the zero-based execution order.

CREATE NODE TABLE File (
    id          STRING,
    name        STRING,
    path        STRING,
    filePath    STRING,
    language    STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Folder (
    id          STRING,
    name        STRING,
    path        STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Function (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    signature       STRING,
    parameterCount  INT32,
    returnType      STRING,
    visibility      STRING,
    isAsync         BOOLEAN,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Class (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    visibility      STRING,
    isAbstract      BOOLEAN,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Interface (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    visibility      STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Method (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    signature       STRING,
    parameterCount  INT32,
    returnType      STRING,
    visibility      STRING,
    isAsync         BOOLEAN,
    isStatic        BOOLEAN,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Property (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    declaredType    STRING,
    visibility      STRING,
    isStatic        BOOLEAN,
    isMutable       BOOLEAN,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE CodeElement (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    kind            STRING,
    filePath        STRING,
    language        STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Community (
    id              STRING,
    heuristicLabel  STRING,
    cohesion        DOUBLE,
    symbolCount     INT32,
    keywords        STRING[],
    description     STRING,
    enrichedBy      STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Process (
    id              STRING,
    heuristicLabel  STRING,
    processType     STRING,
    stepCount       INT32,
    communities     STRING[],
    entryPointId    STRING,
    terminalId      STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Route (
    id              STRING,
    name            STRING,
    path            STRING,
    httpMethod      STRING,
    filePath        STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Tool (
    id              STRING,
    name            STRING,
    description     STRING,
    toolType        STRING,
    filePath        STRING,
    PRIMARY KEY (id)
);

// Language-specific semantic node types.
CREATE NODE TABLE Struct (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    visibility      STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Enum (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    visibility      STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Trait (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    visibility      STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE NODE TABLE Impl (
    id              STRING,
    name            STRING,
    qualifiedName   STRING,
    filePath        STRING,
    language        STRING,
    startLine       INT32,
    endLine         INT32,
    PRIMARY KEY (id)
);

CREATE REL TABLE GROUP CodeRelation (
    // Filesystem containment and definitions.
    FROM Folder TO Folder,
    FROM Folder TO File,
    FROM File TO Function,
    FROM File TO Method,
    FROM File TO Class,
    FROM File TO Interface,
    FROM File TO Property,
    FROM File TO CodeElement,
    FROM File TO Route,
    FROM File TO Tool,
    FROM File TO Struct,
    FROM File TO Enum,
    FROM File TO Trait,
    FROM File TO Impl,

    // Nested declarations and members.
    FROM Class TO Class,
    FROM Class TO Struct,
    FROM Class TO Impl,
    FROM Class TO Method,
    FROM Class TO Property,
    FROM Interface TO Class,
    FROM Interface TO Struct,
    FROM Interface TO Trait,
    FROM Interface TO Impl,
    FROM Interface TO Method,
    FROM Interface TO Property,
    FROM Struct TO Class,
    FROM Struct TO Struct,
    FROM Struct TO Impl,
    FROM Struct TO Method,
    FROM Struct TO Property,
    FROM Enum TO Method,
    FROM Enum TO Property,
    FROM Trait TO Class,
    FROM Trait TO Struct,
    FROM Trait TO Interface,
    FROM Trait TO Impl,
    FROM Trait TO Method,
    FROM Trait TO Property,
    FROM Impl TO Method,
    FROM Impl TO Property,
    FROM Impl TO Class,
    FROM Impl TO Struct,
    FROM Impl TO Enum,
    FROM Impl TO Trait,
    FROM Impl TO Interface,
    FROM Impl TO Impl,
    FROM Function TO Class,
    FROM Function TO Interface,
    FROM Function TO Struct,
    FROM Function TO Trait,
    FROM Function TO Impl,
    FROM Method TO Class,
    FROM Method TO Interface,
    FROM Method TO Struct,
    FROM Method TO Trait,
    FROM Method TO Impl,

    // Imports, inheritance, implementation, calls, and property access.
    FROM File TO File,
    FROM Function TO Function,
    FROM Function TO Method,
    FROM Function TO Property,
    FROM Method TO Function,
    FROM Method TO Method,
    FROM Method TO Property,
    FROM Class TO Interface,
    FROM Class TO Trait,
    FROM Struct TO Interface,
    FROM Struct TO Trait,
    FROM Enum TO Interface,
    FROM Enum TO Trait,
    FROM Interface TO Interface,
    FROM Trait TO Trait,

    // Communities and process traces.
    FROM File TO Community,
    FROM Function TO Community,
    FROM Class TO Community,
    FROM Interface TO Community,
    FROM Method TO Community,
    FROM Property TO Community,
    FROM CodeElement TO Community,
    FROM Route TO Community,
    FROM Tool TO Community,
    FROM Struct TO Community,
    FROM Enum TO Community,
    FROM Trait TO Community,
    FROM Impl TO Community,
    FROM File TO Process,
    FROM Function TO Process,
    FROM Class TO Process,
    FROM Interface TO Process,
    FROM Method TO Process,
    FROM Property TO Process,
    FROM CodeElement TO Process,
    FROM Route TO Process,
    FROM Tool TO Process,
    FROM Struct TO Process,
    FROM Enum TO Process,
    FROM Trait TO Process,
    FROM Impl TO Process,

    // Framework routes, external fetches, and tool handlers.
    FROM Function TO Route,
    FROM Method TO Route,
    FROM Class TO Route,
    FROM Route TO Route,
    FROM Function TO Tool,
    FROM Method TO Tool,
    FROM Class TO Tool,
    FROM Tool TO Function,
    FROM Tool TO Method,
    FROM Tool TO Class,

    type        STRING,
    confidence  DOUBLE,
    reason      STRING,
    step        INT32
);
