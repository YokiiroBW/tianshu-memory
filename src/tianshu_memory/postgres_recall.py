"""Set-based BM25 over the complete, currently authorized in-memory corpus.

The payload is sent once, and vectors, term frequencies and corpus statistics are
materialized once per call. It never ranks against hidden/persistent search rows.
The formula matches the original product PostgreSQL memory_bm25 function.
"""

from .domain import canonical

SQL = r"""
WITH documents AS MATERIALIZED (
  SELECT group_id,text,to_tsvector('simple',text) AS vector
  FROM jsonb_to_recordset(?::jsonb) AS corpus(group_id text,text text)
), frequencies AS MATERIALIZED (
  SELECT d.group_id,t.lexeme,cardinality(t.positions)::double precision AS frequency
  FROM documents d CROSS JOIN LATERAL unnest(d.vector) t
), lengths AS MATERIALIZED (
  SELECT d.group_id,coalesce(sum(f.frequency),0)::double precision AS length
  FROM documents d LEFT JOIN frequencies f USING(group_id) GROUP BY d.group_id
), corpus AS MATERIALIZED (
  SELECT count(*)::double precision AS total,avg(length) AS average_length FROM lengths
), query AS MATERIALIZED (
  SELECT websearch_to_tsquery('simple',?) AS expression,
    tsvector_to_array(to_tsvector('simple',regexp_replace(?,'"|\mOR\M',' ','g'))) AS terms
), terms AS MATERIALIZED (
  SELECT unnest(terms) AS term FROM query
), hits AS MATERIALIZED (
  SELECT d.* FROM documents d CROSS JOIN query q WHERE d.vector @@ q.expression
), document_frequencies AS MATERIALIZED (
  SELECT t.term,count(d.group_id)::double precision AS documents
  FROM terms t LEFT JOIN documents d
    ON d.vector @@ plainto_tsquery('simple',t.term)
  GROUP BY t.term
)
SELECT h.group_id,h.text,-coalesce(sum(
  greatest(0.000001,ln((c.total-df.documents+0.5)/(df.documents+0.5)))
    * f.frequency*2.2/(f.frequency+1.2*(0.25+0.75*l.length/c.average_length))
  ) FILTER (WHERE df.term IS NOT NULL AND f.frequency>0 AND c.average_length>0),0) AS relevance
FROM hits h JOIN lengths l USING(group_id) CROSS JOIN corpus c
LEFT JOIN frequencies f ON f.group_id=h.group_id
LEFT JOIN document_frequencies df ON df.term=f.lexeme
GROUP BY h.group_id,h.text
"""


def rank(db, documents, expression):
    payload = canonical([{"group_id": identity, "text": text} for identity, text in documents])
    return db.native(SQL, (payload, expression, expression))
