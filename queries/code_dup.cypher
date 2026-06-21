MATCH (f:Function)
OPTIONAL MATCH (f)-[c:CodeRelation]->(callee:Function|Method)
WHERE c.type = 'CALLS'
WITH f, list_sort(collect(DISTINCT callee.qualifiedName)) AS callSignature
WITH f.parameterCount AS arity, f.returnType AS retType, callSignature, collect(f) AS candidates
WHERE size(candidates) > 1
RETURN arity, retType, callSignature,
       [c IN candidates | c.qualifiedName] AS possibleDuplicates;
