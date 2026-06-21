MATCH (elem {id: $elementId})
MATCH p = (container:Function|Method|Class|Struct|Trait|Impl|File)-[hops:CodeRelation*1..6
        (rel, node | WHERE rel.type IN ['CONTAINS','DEFINES','HAS_METHOD','HAS_PROPERTY','MEMBER_OF'])]->(elem)
WITH container, min(length(p)) AS distance
RETURN container.qualifiedName AS nearestContainer, distance
ORDER BY distance ASC
LIMIT 1;
