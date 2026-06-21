MATCH (base:Method {qualifiedName: $baseMethod})
MATCH p = (base)<-[hops:CodeRelation*0..6
            (rel, node | WHERE rel.type IN ['METHOD_OVERRIDES','METHOD_IMPLEMENTS'])]-(impl:Method)
WITH collect(DISTINCT impl) AS implementations
UNWIND implementations AS impl
MATCH (site:Function|Method)-[c:CodeRelation]->(impl)
WHERE c.type = 'CALLS'
RETURN impl.qualifiedName AS implementation, impl.filePath AS file,
       collect(DISTINCT site.qualifiedName) AS callers;
