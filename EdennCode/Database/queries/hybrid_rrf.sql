-- Hybrid retrieval: BM25 (tsvector) + vector (pgvector) via Reciprocal Rank Fusion.
-- Spec: §3.5.
--
-- Parameters:
--   $1 :: VECTOR(1536)  query embedding (from embed-standard)
--   $2 :: TEXT          query text (for plainto_tsquery)
--   $3 :: TEXT          workflow_type filter (NULL = all)
--   $4 :: INT           top_k

WITH q AS (
  SELECT $1::vector AS qvec,
         plainto_tsquery('english', $2) AS qtxt
),
semantic AS (
  SELECT pr.request_id,
         1.0 / (60 + ROW_NUMBER() OVER (
           ORDER BY pr.video_summary_embedding <=> q.qvec
         )) AS rrf
  FROM pipeline_runs pr, q
  WHERE pr.video_summary_embedding IS NOT NULL
    AND pr.status = 'succeeded'
    AND ($3::TEXT IS NULL OR pr.workflow_type = $3)
  ORDER BY pr.video_summary_embedding <=> q.qvec
  LIMIT 50
),
lexical AS (
  SELECT pr.request_id,
         1.0 / (60 + ROW_NUMBER() OVER (
           ORDER BY ts_rank(pr.summary_tsv, q.qtxt) DESC
         )) AS rrf
  FROM pipeline_runs pr, q
  WHERE pr.summary_tsv @@ q.qtxt
    AND pr.status = 'succeeded'
    AND ($3::TEXT IS NULL OR pr.workflow_type = $3)
  LIMIT 50
)
SELECT r.request_id,
       r.user_prompt,
       pr.video_summary,
       pr.music_prompt,
       pr.modelspec,
       pr.music_provider,
       r.output_url,
       SUM(rrf) AS score
FROM (SELECT * FROM semantic UNION ALL SELECT * FROM lexical) ranked
JOIN requests r       ON r.request_id  = ranked.request_id
JOIN pipeline_runs pr ON pr.request_id = ranked.request_id
GROUP BY r.request_id, r.user_prompt, pr.video_summary, pr.music_prompt,
         pr.modelspec, pr.music_provider, r.output_url
ORDER BY score DESC
LIMIT $4;
