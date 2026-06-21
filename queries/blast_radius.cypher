MATCH (target:Function|Method {qualifiedName: $fqn})
MATCH p = (target)<-[hops:CodeRelation*1..6
            (rel, node | WHERE rel.type = 'CALLS' AND rel.confidence >= 0.7)]-(caller:Function|Method)
WITH caller, min(length(p)) AS distance
RETURN caller.qualifiedName AS caller, caller.filePath AS file, distance
ORDER BY distance ASC;
