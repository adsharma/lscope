-- cycles
MATCH p = (f:File)-[hops:CodeRelation*2..10 (rel, node | WHERE rel.type = 'IMPORTS')]->(f)
RETURN [n IN nodes(p) | n.path] AS cycle, length(p) AS cycleLength
LIMIT 50;

-- layer-skip violations (ui -> db without going through services)
MATCH p = (a:File)-[hops:CodeRelation*1..8 (rel, node | WHERE rel.type = 'IMPORTS')]->(b:File)
WHERE a.path STARTS WITH 'src/ui/'
  AND b.path STARTS WITH 'src/db/'
  AND NONE(n IN nodes(p)[1..-1] WHERE n.path STARTS WITH 'src/services/')
RETURN [n IN nodes(p) | n.path] AS violatingChain;
