"""Generate Recommendation Agent documentation as a Word document."""

from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import datetime

doc = Document()

# ── Page margins ──────────────────────────────────────────────────────────────
section = doc.sections[0]
section.page_width  = Inches(8.5)
section.page_height = Inches(11)
section.left_margin   = Inches(1)
section.right_margin  = Inches(1)
section.top_margin    = Inches(1)
section.bottom_margin = Inches(1)

# ── Colour palette ────────────────────────────────────────────────────────────
BRAND_DARK   = RGBColor(0x1A, 0x1A, 0x2E)   # navy
BRAND_ACCENT = RGBColor(0x16, 0x21, 0x3E)   # dark-blue
BRAND_BLUE   = RGBColor(0x0F, 0x3F, 0x7F)   # mid-blue
CODE_BG      = RGBColor(0xF3, 0xF4, 0xF6)   # light grey
GREEN        = RGBColor(0x16, 0x7F, 0x39)   # frontend-section accent
ORANGE       = RGBColor(0xC0, 0x5C, 0x00)   # warning / note

# ── Style helpers ─────────────────────────────────────────────────────────────
def set_font(run, name="Calibri", size=11, bold=False, color=None, italic=False):
    run.font.name  = name
    run.font.size  = Pt(size)
    run.bold       = bold
    run.italic     = italic
    if color:
        run.font.color.rgb = color

def heading1(text, color=BRAND_DARK):
    p = doc.add_heading(level=1)
    p.clear()
    run = p.add_run(text)
    set_font(run, size=18, bold=True, color=color)
    p.paragraph_format.space_before = Pt(18)
    p.paragraph_format.space_after  = Pt(6)
    return p

def heading2(text, color=BRAND_BLUE):
    p = doc.add_heading(level=2)
    p.clear()
    run = p.add_run(text)
    set_font(run, size=14, bold=True, color=color)
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after  = Pt(4)
    return p

def heading3(text, color=BRAND_ACCENT):
    p = doc.add_heading(level=3)
    p.clear()
    run = p.add_run(text)
    set_font(run, size=12, bold=True, color=color)
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after  = Pt(2)
    return p

def body(text, bold_parts=None):
    """Add a body paragraph, optionally with inline bold segments."""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    set_font(run, size=11)
    return p

def bullet(text, level=0):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.left_indent  = Inches(0.25 * (level + 1))
    p.paragraph_format.space_after  = Pt(3)
    run = p.add_run(text)
    set_font(run, size=11)
    return p

def code_block(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent  = Inches(0.4)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)
    run = p.add_run(text)
    set_font(run, name="Courier New", size=9, color=BRAND_ACCENT)
    # grey shading
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), "F3F4F6")
    pPr.append(shd)
    return p

def note_block(text, color=ORANGE):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent  = Inches(0.4)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)
    run = p.add_run("NOTE  ")
    set_font(run, size=10, bold=True, color=color)
    run2 = p.add_run(text)
    set_font(run2, size=10, italic=True)
    return p

def add_table(headers, rows, col_widths=None):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    # header row
    hdr = table.rows[0]
    for i, h in enumerate(headers):
        cell = hdr.cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(h)
        set_font(run, size=10, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
        # dark background
        tc  = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd  = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "1A1A2E")
        tcPr.append(shd)
    # data rows
    for ri, row_data in enumerate(rows):
        row = table.rows[ri + 1]
        fill = "FFFFFF" if ri % 2 == 0 else "F3F4F6"
        for ci, cell_text in enumerate(row_data):
            cell = row.cells[ci]
            cell.text = ""
            run = cell.paragraphs[0].add_run(str(cell_text))
            set_font(run, size=10)
            tc   = cell._tc
            tcPr = tc.get_or_add_tcPr()
            shd  = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), fill)
            tcPr.append(shd)
    if col_widths:
        for i, w in enumerate(col_widths):
            for row in table.rows:
                row.cells[i].width = Inches(w)
    doc.add_paragraph()
    return table

def divider():
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CCCCCC")
    pBdr.append(bottom)
    pPr.append(pBdr)

# ══════════════════════════════════════════════════════════════════════════════
# COVER PAGE
# ══════════════════════════════════════════════════════════════════════════════
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(60)
run = p.add_run("EDENN")
set_font(run, size=36, bold=True, color=BRAND_DARK)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("Recommendation Agent")
set_font(run, size=28, bold=True, color=BRAND_BLUE)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("Data Schema, System Flow & Frontend Integration Guide")
set_font(run, size=14, italic=True, color=RGBColor(0x55, 0x55, 0x55))

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(40)
run = p.add_run(f"Generated: {datetime.date.today().strftime('%B %d, %Y')}")
set_font(run, size=11, color=RGBColor(0x88, 0x88, 0x88))

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
# TABLE OF CONTENTS (manual)
# ══════════════════════════════════════════════════════════════════════════════
heading1("Table of Contents")
toc_items = [
    ("1", "System Overview"),
    ("2", "API Endpoint Reference"),
    ("3", "Workflow Stages"),
    ("    3.1", "Stage 1 — Build User Profile"),
    ("    3.2", "Stage 2 — Retrieve & Rank Candidates"),
    ("    3.3", "Stage 3 — Hydrate Candidates"),
    ("4", "Database Schemas"),
    ("5", "Vector Embedding Pipeline"),
    ("6", "Event Telemetry"),
    ("7", "Full Request → Response Flow"),
    ("8", "Key Algorithm Parameters"),
    ("9", "Frontend Integration Guide"),
    ("    9.1", "Data Fields the Frontend Must Capture"),
    ("    9.2", "User Identity & Session"),
    ("    9.3", "Recommendation Display"),
    ("    9.4", "Cold-Start Handling"),
    ("    9.5", "Debug Mode"),
    ("    9.6", "Interaction Events to Track"),
    ("    9.7", "State Management Recommendations"),
    ("    9.8", "Error Handling"),
]
for num, title in toc_items:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(f"{num}  {title}")
    set_font(run, size=11)

doc.add_page_break()

# ══════════════════════════════════════════════════════════════════════════════
# 1. SYSTEM OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════
heading1("1. System Overview")
body(
    "The Edenn Recommendation Agent is a deterministic, SQL-backed RAG (Retrieval-Augmented Generation) "
    "pipeline that serves personalised music creatives to users. It operates in two distinct modes:"
)
bullet("User Mode — ranks candidates using a hybrid score that combines music-video alignment quality with "
       "cosine similarity to the user's historical prompt embedding centroid.")
bullet("Global Mode — ranks all candidates purely by alignment score with no personalisation.")

body(
    "The system is async-first, database-driven via PostgreSQL with pgvector, and designed for "
    "high-throughput concurrent requests. All recommendation logic is stateless at the API layer; "
    "all state lives in the database."
)

add_table(
    ["Property", "Value"],
    [
        ["Embedding Model", "hosted text-embedding model (1536 dimensions)"],
        ["Vector Store", "PostgreSQL + pgvector extension"],
        ["Alignment Weight", "0.60"],
        ["Cosine Similarity Weight", "0.40"],
        ["Default Response Limit", "10 items"],
        ["Maximum Response Limit", "50 items"],
        ["Cold-Start Behaviour", "Returns HTTP 200 with empty recommendations list"],
    ],
    col_widths=[2.5, 4.0],
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 2. API ENDPOINT REFERENCE
# ══════════════════════════════════════════════════════════════════════════════
heading1("2. API Endpoint Reference")

heading2("GET  /api/v1/recommendations")

heading3("Query Parameters")
add_table(
    ["Parameter", "Type", "Required", "Default", "Description"],
    [
        ["user_id",  "string",  "Conditional", "—",    "Authenticated user ID. Mutually exclusive with global."],
        ["global",   "boolean", "Conditional", "false","Return global top-K by alignment only. Mutually exclusive with user_id."],
        ["limit",    "integer", "No",          "10",   "Number of results. Range: 1–50."],
        ["debug",    "boolean", "No",          "false","Expand each result with score breakdown and metadata."],
    ],
    col_widths=[1.2, 0.9, 1.1, 0.8, 2.5],
)
note_block("Exactly one of user_id or global must be supplied. Omitting both or supplying both returns HTTP 422.")

heading3("Response Schema — RecommendationResponse")
code_block(
"""{
  "mode": "user" | "global",
  "is_cold_start": boolean,
  "profile_history_count": integer,
  "version": "dev-<IMAGE_TAG>",
  "recommendations": [ ...RecommendationItem ]
}"""
)

heading3("RecommendationItem — Slim (default)")
code_block(
"""{
  "creative_id":    string,   // Unique creative identifier
  "music_id":       string,   // Selected music asset identifier
  "full_audio_url": string,   // CDN URL for the full generated audio
  "title":          string,   // Display title of the creative
  "thumbnail_url":  string,   // CDN URL for thumbnail image
  "score":          float     // Final ranking score (0.0 – 1.0)
}"""
)

heading3("RecommendationItem — Debug (when ?debug=true)")
code_block(
"""{
  ...slim fields...,
  "description":       string,
  "result_video_url":  string,   // CDN URL for the remixed video
  "lyrics_text":       string | null,
  "alignment_score":   float,    // Music-video alignment component
  "cosine_similarity": float | null,  // Prompt similarity component (null in global mode)
  "music_prompt_json": object,   // Full music prompt used during generation
  "created_at":        string    // ISO-8601 timestamp
}"""
)

heading3("HTTP Status Codes")
add_table(
    ["Status", "Condition"],
    [
        ["200 OK",             "Successful response. recommendations may be an empty array (cold-start)."],
        ["422 Unprocessable",  "Validation failure — missing or conflicting query params, limit out of range."],
        ["500 Internal Error", "Database connection failure or unhandled exception."],
    ],
    col_widths=[1.5, 5.0],
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 3. WORKFLOW STAGES
# ══════════════════════════════════════════════════════════════════════════════
heading1("3. Workflow Stages")

body("The RecommendationWorkflow orchestrates three sequential stages. Each stage is a self-contained "
     "async class that accepts typed input and returns typed output.")

code_block(
"""RecommendationWorkflowInput
    │
    ▼
[Stage 1] BuildUserProfileStage
    │   Queries generation_job for user history
    │   Computes prompt embedding centroid
    │   ── Cold-start exit if history_count == 0 ──►  empty result
    ▼
[Stage 2] RetrieveCandidatesStage
    │   pgvector cosine ANN query
    │   Hybrid score: 0.6 × alignment + 0.4 × cosine
    ▼
[Stage 3] HydrateStage
    │   3-table JOIN: creative + music_asset + snapshot
    ▼
RecommendationWorkflowResult"""
)

# Stage 1
heading2("3.1  Stage 1 — Build User Profile")
body("File: EdennCode/WorkflowFactory/RecommendationWorkflow/build_user_profile_stage.py")

body("Queries the generation_job table to aggregate a user's generation history into a profile object.")

heading3("SQL Query")
code_block(
"""SELECT user_requested_language,
       include_vocals,
       user_prompt_embedding
FROM   generation_job
WHERE  creator_user_id = $1
  AND  user_prompt_embedding IS NOT NULL"""
)

heading3("Computed Fields")
add_table(
    ["Field", "Computation", "Type"],
    [
        ["preferred_language",       "Mode (most frequent) of user_requested_language", "string"],
        ["preferred_include_vocals", "Mode of include_vocals boolean",                  "boolean | null"],
        ["prompt_centroid",          "numpy.mean(all user_prompt_embedding vectors)",    "float[1536] | null"],
        ["history_count",            "Total row count",                                 "integer"],
    ],
    col_widths=[2.0, 3.0, 1.5],
)

heading3("UserProfile Dataclass")
code_block(
"""@dataclass(frozen=True)
class UserProfile:
    user_id:                  str
    history_count:            int
    preferred_language:       str             = ""
    preferred_include_vocals: Optional[bool]  = None
    prompt_centroid:          Optional[list[float]] = None  # 1536-dim"""
)

note_block("If history_count == 0 the workflow returns immediately with is_cold_start=True and an empty recommendations list.")

# Stage 2
heading2("3.2  Stage 2 — Retrieve & Rank Candidates")
body("File: EdennCode/WorkflowFactory/RecommendationWorkflow/retrieve_candidates_stage.py")

heading3("Scoring Formula (User Mode)")
code_block("final_score = 0.6 × alignment_score + 0.4 × (1 - cosine_distance)")

heading3("Global Mode SQL")
code_block(
"""SELECT creative_id,
       alignment_score,
       alignment_score AS final_score
FROM   creative_feature_snapshot
ORDER  BY alignment_score DESC, refreshed_at DESC
LIMIT  $1"""
)

heading3("User-RAG Mode SQL")
code_block(
"""SELECT creative_id,
       alignment_score,
       1 - (music_embedding <=> $1::vector)           AS cosine_similarity,
       0.6 * alignment_score
       + 0.4 * (1 - (music_embedding <=> $1::vector)) AS final_score
FROM   creative_feature_snapshot
WHERE  music_embedding IS NOT NULL
  AND  LOWER(SPLIT_PART(language, '_', 1))
       = LOWER(SPLIT_PART($2, '_', 1))          -- language filter
ORDER  BY final_score DESC
LIMIT  $3"""
)

note_block("Language normalisation: ENGLISH_US, ENGLISH, English all collapse to 'english'.")

heading3("Candidate Dataclass")
code_block(
"""@dataclass(frozen=True)
class Candidate:
    creative_id:       str
    final_score:       float
    alignment_score:   float
    cosine_similarity: Optional[float]   # None in global mode"""
)

# Stage 3
heading2("3.3  Stage 3 — Hydrate Candidates")
body("File: EdennCode/WorkflowFactory/RecommendationWorkflow/hydrate_stage.py")

body("Performs a single batch JOIN for all candidate IDs to minimise round-trips.")

heading3("SQL Query")
code_block(
"""SELECT c.creative_id,
       c.title, c.description,
       c.thumbnail_url, c.result_video_url, c.created_at,
       c.selected_music_id AS music_id,
       m.full_audio_url, m.matched_audio_url, m.lyrics_text,
       s.alignment_score, s.music_prompt_json
FROM   creative c
LEFT JOIN music_asset              m ON m.music_id    = c.selected_music_id
LEFT JOIN creative_feature_snapshot s ON s.creative_id = c.creative_id
WHERE  c.creative_id = ANY($1::text[])"""
)

note_block("full_audio_url falls back to matched_audio_url when the primary URL is NULL.")

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 4. DATABASE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════
heading1("4. Database Schemas")
body("All schemas are defined as frozen Python dataclasses in "
     "EdennCode/Deployment/recommendation_persistence.py and map 1-to-1 to PostgreSQL tables.")

heading2("generation_job")
add_table(
    ["Column", "Type", "Notes"],
    [
        ["job_id",                   "TEXT PK",            "Primary key for the generation request"],
        ["creator_user_id",          "TEXT | NULL",        "Links request to authenticated user"],
        ["user_prompt",              "TEXT",               "Raw natural-language input from user"],
        ["user_prompt_embedding",    "vector(1536) | NULL","RAG query vector — computed from user_prompt"],
        ["user_requested_language",  "TEXT",               "e.g. ENGLISH_US, KOREAN"],
        ["include_vocals",           "BOOLEAN",            "User preference for vocals"],
        ["model_spec",               "TEXT",               "edenn_basic | edenn_enhanced | edenn_studio"],
        ["alignment_id",             "TEXT FK",            "Links to music_video_alignment"],
        ["creative_id",              "TEXT FK",            "Links to creative"],
        ["selected_music_id",        "TEXT FK",            "Links to music_asset"],
        ["token_usage_json",         "JSONB",              "LLM token accounting"],
        ["status",                   "TEXT",               "Default: completed"],
        ["job_received_timestamp",   "BIGINT | NULL",      "Unix ms when job was received"],
        ["job_finished_timestamp",   "BIGINT | NULL",      "Unix ms when job finished"],
    ],
    col_widths=[2.2, 1.8, 2.5],
)

heading2("creative_feature_snapshot")
body("Denormalised recommendation table — one row per creative. The primary document-side vector store.")
add_table(
    ["Column", "Type", "Notes"],
    [
        ["creative_id",         "TEXT PK",            "Primary key"],
        ["music_embedding",     "vector(1536) | NULL","RAG document vector — from music_prompt_json"],
        ["visual_embedding",    "vector(1536) | NULL","Reserved; not used in ranking yet"],
        ["alignment_score",     "FLOAT",              "Core quality signal"],
        ["language",            "TEXT",               "Language of the generation e.g. ENGLISH_US"],
        ["genre_level1",        "TEXT | NULL",        "Top-level genre tag"],
        ["content_type",        "TEXT | NULL",        "e.g. dance, cooking, travel"],
        ["energy_level",        "TEXT | NULL",        "low | medium | high"],
        ["overall_mood",        "TEXT | NULL",        "e.g. happy, melancholic, epic"],
        ["tempo_bpm",           "FLOAT | NULL",       "Estimated BPM of generated music"],
        ["include_vocals",      "BOOLEAN",            "Whether vocals are present"],
        ["vocal_gender",        "TEXT",               "e.g. female, male, none"],
        ["platform_hint",       "TEXT | NULL",        "e.g. tiktok, instagram, youtube"],
        ["music_prompt_json",   "JSONB",              "Full music prompt — source for music_embedding"],
        ["visual_feature_json", "JSONB",              "Scene-level visual analysis"],
        ["music_feature_json",  "JSONB",              "Generated music metadata"],
        ["scene_summary_json",  "JSONB[]",            "Per-scene summaries"],
        ["refreshed_at",        "TIMESTAMPTZ",        "Last upsert timestamp — used as tiebreaker"],
    ],
    col_widths=[2.0, 1.8, 2.7],
)

heading3("Indexes")
code_block(
"""-- Coarse categorical filter (supports future genre/mood filtering)
CREATE INDEX idx_creative_feature_snapshot_coarse
  ON creative_feature_snapshot (language, genre_level1, content_type, energy_level, include_vocals)

-- Alignment-score sort (used in global mode)
CREATE INDEX idx_creative_feature_snapshot_alignment
  ON creative_feature_snapshot (alignment_score DESC)"""
)

heading2("creative")
add_table(
    ["Column", "Type", "Notes"],
    [
        ["creative_id",       "TEXT PK",       "Primary key"],
        ["title",             "TEXT",          "Display title"],
        ["description",       "TEXT",          "Longer description"],
        ["thumbnail_url",     "TEXT | NULL",   "CDN URL for cover image"],
        ["result_video_url",  "TEXT | NULL",   "CDN URL for the final remixed video"],
        ["creator_user_id",   "TEXT | NULL",   "Ownership — links to user"],
        ["visibility",        "TEXT",          "private | public"],
        ["render_status",     "TEXT",          "Default: completed"],
        ["selected_music_id", "TEXT FK",       "Links to music_asset"],
    ],
    col_widths=[1.8, 1.5, 3.2],
)

heading2("music_asset")
add_table(
    ["Column", "Type", "Notes"],
    [
        ["music_id",           "TEXT PK",       "Primary key"],
        ["full_audio_url",     "TEXT | NULL",   "CDN URL for the complete generated audio"],
        ["matched_audio_url",  "TEXT | NULL",   "Fallback CDN URL"],
        ["lyrics_text",        "TEXT | NULL",   "Full lyrics string"],
        ["lyrics_timestamp_json","JSONB[]",     "Word-level timestamps for karaoke display"],
        ["music_feature_json", "JSONB",         "Provider metadata (style, tempo, tags)"],
        ["variant_label",      "TEXT",          "Music variant identifier"],
        ["provider_task_id",   "TEXT | NULL",   "External provider task reference"],
    ],
    col_widths=[1.8, 1.5, 3.2],
)

heading2("music_video_alignment")
add_table(
    ["Column", "Type", "Notes"],
    [
        ["alignment_id",            "TEXT PK",   "Primary key"],
        ["alignment_score",         "FLOAT",     "0.0–1.0 quality score"],
        ["selected_clip_start_s",   "FLOAT",     "Start of matched clip in seconds"],
        ["selected_clip_duration_s","FLOAT | NULL","Duration of matched clip"],
        ["alignment_reason_json",   "JSONB",     "Explainability data for the match"],
        ["matching_used_track",     "TEXT | NULL","Which audio track was used for alignment"],
    ],
    col_widths=[2.2, 1.5, 2.8],
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 5. VECTOR EMBEDDING PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
heading1("5. Vector Embedding Pipeline")
body("File: EdennCode/Scripts/backfill_recommendation_embeddings.py")

body("Embeddings are computed offline by a backfill script and stored in the database. "
     "The production API does not compute embeddings at request time.")

add_table(
    ["Vector Field", "Source Text", "Table", "Role in Ranking"],
    [
        ["user_prompt_embedding",  "generation_job.user_prompt (raw user input)",       "generation_job",            "Query vector — centroid of user history"],
        ["music_embedding",        "creative_feature_snapshot.music_prompt_json::text", "creative_feature_snapshot", "Document vector — what the music sounds like"],
    ],
    col_widths=[1.8, 2.4, 1.8, 2.0],
)

heading3("Embedding Configuration")
add_table(
    ["Property", "Value"],
    [
        ["Model",            "hosted text-embedding model"],
        ["Provider",         "the upstream provider"],
        ["Dimensions",       "1536"],
        ["Storage dtype",    "float32"],
        ["Env var",          "the embedding-deployment key in the service env (see deployment config)"],
        ["Batch size",       "64 rows per iteration"],
    ],
    col_widths=[2.5, 4.0],
)

heading3("Backfill Loop")
code_block(
"""while True:
    rows = await pool.fetch(
        \"\"\"SELECT job_id, user_prompt FROM generation_job
           WHERE user_prompt IS NOT NULL
             AND user_prompt <> ''
             AND user_prompt_embedding IS NULL
           ORDER BY created_at LIMIT 64\"\"\")
    if not rows:
        break
    embeddings = await embed_batch(client, [r["user_prompt"] for r in rows])
    for r, emb in zip(rows, embeddings):
        await pool.execute(
            "UPDATE generation_job SET user_prompt_embedding = $1 WHERE job_id = $2",
            np.array(emb, dtype=np.float32), r["job_id"])"""
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 6. EVENT TELEMETRY
# ══════════════════════════════════════════════════════════════════════════════
heading1("6. Event Telemetry")
body("All events extend AnnotationEvent and are dispatched non-blocking via asyncio.create_task(). "
     "Events are fire-and-forget — pipeline latency is not affected.")

heading3("Base: AnnotationEvent")
code_block(
"""@dataclass(kw_only=True)
class AnnotationEvent:
    event_type:      str          # stable category identifier
    job_id:          str          # shared across all events in one run
    schema_version:  str = "v1"
    timestamp_utc:   float        # time.time()
    event_id:        str          # uuid4
    metadata:        dict"""
)

heading2("MusicPromptEvent")
body("Emitted after Stage 3.1 (MusicPromptOrchestration). Captures the prompt constructed for the music model.")
add_table(
    ["Field", "Type", "Description"],
    [
        ["model_spec",          "str",            "edenn_basic | edenn_enhanced | edenn_studio"],
        ["style_prompt",        "str | None",     "Style description passed to music model"],
        ["lyrics_prompt",       "str | None",     "Lyrics text passed to music model"],
        ["combined_prompt",     "str | None",     "Single prompt for edenn_basic"],
        ["include_vocals",      "bool",           "Whether vocals were requested"],
        ["vocal_gender",        "str",            "Gender of vocals"],
        ["generation_language", "str",            "Language for generation"],
        ["token_usage",         "dict[str, int]", "LLM token breakdown"],
        ["prompt_dict",         "dict",           "Full prompt dictionary"],
        ["stage_latency_s",     "float",          "Wall-clock time for this stage"],
    ],
    col_widths=[2.0, 1.5, 3.0],
)

heading2("MusicGenerationEvent")
body("Emitted after Stage 4 (MusicGeneration). Highest-value annotation event.")
add_table(
    ["Field", "Type", "Description"],
    [
        ["provider_name",         "str",        "internal provider routing key"],
        ["task_id",               "str | None", "External provider task ID"],
        ["audio_id",              "str | None", "External provider audio ID"],
        ["style_prompt",          "str | None", ""],
        ["lyrics_prompt",         "str | None", ""],
        ["has_lyrics",            "bool",       "Whether generated track includes lyrics"],
        ["full_lyrics_text",      "str | None", "Complete lyrics output"],
        ["line_timestamp_count",  "int",        "Number of timestamped lines"],
        ["word_timestamp_count",  "int",        "Number of timestamped words"],
        ["video_duration_s",      "float",      "Source video length in seconds"],
        ["video_category",        "str",        "Video category tag"],
        ["scene_count",           "int",        "Number of detected scenes"],
        ["overall_mood",          "str",        "Mood label from visual analysis"],
        ["generation_latency_s",  "float | None","Wall-clock time for music generation"],
        ["extension_rounds",      "int",        "How many times audio was extended"],
    ],
    col_widths=[2.0, 1.5, 3.0],
)

heading2("RemixCompletionEvent")
add_table(
    ["Field", "Type", "Description"],
    [
        ["remixed_video_filename",     "str",           "Output file name"],
        ["preserve_original_audio",    "bool",          "Whether original audio track was kept"],
        ["music_volume",               "float",         "Volume multiplier for music track"],
        ["duck_gain_db",               "float",         "Audio ducking level in dB (default -9.0)"],
        ["stage_latency_s",            "float",         "Wall-clock time for remix stage"],
        ["total_pipeline_latency_s",   "float | None",  "End-to-end wall-clock time"],
    ],
    col_widths=[2.5, 1.5, 2.5],
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 7. FULL REQUEST → RESPONSE FLOW
# ══════════════════════════════════════════════════════════════════════════════
heading1("7. Full Request → Response Flow")

code_block(
"""GET /api/v1/recommendations?user_id=u1&limit=5
        │
        ▼
  ┌──────────────────────────────────────────┐
  │  VALIDATION                              │
  │  · Exactly one of (user_id, global)      │
  │  · 1 ≤ limit ≤ 50                        │
  └──────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────┐
  │  STAGE 1 — BUILD USER PROFILE            │
  │  SELECT user_prompt_embedding,           │
  │         user_requested_language,         │
  │         include_vocals                   │
  │  FROM generation_job                     │
  │  WHERE creator_user_id = 'u1'            │
  │                                          │
  │  → history_count = 42                    │
  │  → preferred_language = "ENGLISH_US"     │
  │  → prompt_centroid = [1536 floats]       │
  └──────────────────────────────────────────┘
        │
        │  if history_count == 0
        ├──────────────────────────────────► HTTP 200
        │                                    { is_cold_start: true,
        │                                      recommendations: [] }
        ▼
  ┌──────────────────────────────────────────┐
  │  STAGE 2 — RETRIEVE & RANK               │
  │  pgvector ANN query filtered by language │
  │  final_score = 0.6×align + 0.4×cosine   │
  │                                          │
  │  → 5 Candidate objects ranked DESC       │
  └──────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────┐
  │  STAGE 3 — HYDRATE                       │
  │  JOIN creative + music_asset + snapshot  │
  │  WHERE creative_id IN (ids…)             │
  │                                          │
  │  → 5 slim payload dicts                  │
  └──────────────────────────────────────────┘
        │
        ▼
  HTTP 200
  {
    "mode": "user",
    "is_cold_start": false,
    "profile_history_count": 42,
    "version": "dev-abc1234",
    "recommendations": [
      { "creative_id": "c1", "score": 0.92, ... },
      { "creative_id": "c2", "score": 0.88, ... },
      ...
    ]
  }"""
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 8. KEY ALGORITHM PARAMETERS
# ══════════════════════════════════════════════════════════════════════════════
heading1("8. Key Algorithm Parameters")
add_table(
    ["Parameter", "Value", "Source File"],
    [
        ["Alignment weight",       "0.6",                  "retrieve_candidates_stage.py — ALIGNMENT_WEIGHT"],
        ["Cosine weight",          "0.4",                  "retrieve_candidates_stage.py — COSINE_WEIGHT"],
        ["Embedding model",        "hosted text-embedding model","EdennCode/Scripts/_common.py"],
        ["Embedding dimensions",   "1536",                 "upstream model output"],
        ["Backfill batch size",    "64 rows",              "EdennCode/Scripts/backfill_recommendation_embeddings.py"],
        ["Language normalisation", "SPLIT_PART(lang,'_',1) LOWER()", "retrieve_candidates_stage.py SQL"],
        ["Default limit",          "10",                   "api_recommendations.py"],
        ["Max limit",              "50",                   "api_recommendations.py"],
    ],
    col_widths=[2.0, 2.0, 3.5],
)

divider()

# ══════════════════════════════════════════════════════════════════════════════
# 9. FRONTEND INTEGRATION GUIDE
# ══════════════════════════════════════════════════════════════════════════════
heading1("9. Frontend Integration Guide", color=GREEN)

body(
    "This section documents every piece of data the frontend must capture, store, and send — "
    "including the fields required to personalise recommendations, display creatives correctly, "
    "and track user interactions."
)

# ─── 9.1 Data Fields the Frontend Must Capture ────────────────────────────────
heading2("9.1  Data Fields the Frontend Must Capture", color=GREEN)

body("The following table lists every field in a recommendation response that the frontend must "
     "read and act on.")

add_table(
    ["Field", "Location", "Type", "Frontend Action"],
    [
        ["mode",                    "root",        "string",  'Show UI label: "Personalised" or "Trending"'],
        ["is_cold_start",           "root",        "boolean", "If true show onboarding / empty-state UI"],
        ["profile_history_count",   "root",        "integer", "Display progress indicator toward personalisation"],
        ["version",                 "root",        "string",  "Log to analytics for debugging"],
        ["creative_id",             "item",        "string",  "Primary key — use for play, share, save actions"],
        ["music_id",                "item",        "string",  "Link audio playback to music asset"],
        ["full_audio_url",          "item",        "string",  "Stream audio — preload on item render"],
        ["title",                   "item",        "string",  "Display in card header"],
        ["thumbnail_url",           "item",        "string",  "Render card cover image — lazy load"],
        ["score",                   "item",        "float",   "Sort guard: never reorder after fetch; optionally display as match %"],
        ["description",             "item (debug)","string",  "Show in expanded / detail view"],
        ["result_video_url",        "item (debug)","string",  "Play full remixed video"],
        ["lyrics_text",             "item (debug)","string",  "Karaoke / lyrics panel"],
        ["alignment_score",         "item (debug)","float",   "Internal — log only; not for user display"],
        ["cosine_similarity",       "item (debug)","float",   "Internal — log only; not for user display"],
    ],
    col_widths=[1.7, 1.3, 1.1, 2.4],
)

# ─── 9.2 User Identity & Session ──────────────────────────────────────────────
heading2("9.2  User Identity & Session", color=GREEN)

body("The recommendation API is keyed on user_id. The frontend must:")
bullet("Store the authenticated user's ID in session/local state immediately after login.")
bullet("Pass user_id as a query parameter on every personalised fetch.")
bullet("Switch to ?global=true only for unauthenticated or guest users.")
bullet("Never mix user_id and global in the same request.")

note_block(
    "user_id must match the value stored in generation_job.creator_user_id. "
    "Any mismatch results in a cold-start response even for returning users."
)

heading3("Request Templates")
code_block("// Authenticated user\nGET /api/v1/recommendations?user_id={userId}&limit=10\n\n// Guest / unauthenticated\nGET /api/v1/recommendations?global=true&limit=10")

# ─── 9.3 Recommendation Display ───────────────────────────────────────────────
heading2("9.3  Recommendation Display", color=GREEN)

heading3("Card Rendering Requirements")
add_table(
    ["UI Element", "Data Source", "Notes"],
    [
        ["Cover image",     "thumbnail_url",   "Lazy-load; show placeholder skeleton on load"],
        ["Card title",      "title",           "Truncate at ~40 chars with ellipsis"],
        ["Audio player",    "full_audio_url",  "Preload first 3 items; stream on demand for rest"],
        ["Video player",    "result_video_url","Load only on tap/click to avoid bandwidth waste"],
        ["Match badge",     "score",           "Optional: display as percentage e.g. '92% match'"],
        ["Mode label",      "mode (root)",     "Show 'For You' (user) or 'Trending' (global)"],
        ["Lyrics panel",    "lyrics_text",     "Show only if lyrics_text is non-null"],
    ],
    col_widths=[1.8, 1.8, 3.0],
)

heading3("List Ordering")
body("Preserve server-returned order exactly. The backend returns items pre-sorted by final_score DESC. "
     "Do not re-sort client-side.")

# ─── 9.4 Cold-Start Handling ──────────────────────────────────────────────────
heading2("9.4  Cold-Start Handling", color=GREEN)

body("A cold-start response is HTTP 200 with is_cold_start=true and recommendations=[].")
body("Required frontend behaviours:")
bullet("Do not show an error state — cold-start is expected for new users.")
bullet("Render an onboarding empty-state: e.g. 'Generate your first video to get personalised picks.'")
bullet("Show a progress indicator if profile_history_count > 0 but < threshold.")
bullet("Auto-fall back to a global feed call after showing the onboarding state.")

code_block(
"""if (response.is_cold_start) {
  showOnboardingEmptyState();
  // optionally fetch global recommendations as fallback
  const fallback = await fetch('/api/v1/recommendations?global=true&limit=10');
  renderFeed(fallback.recommendations);
}"""
)

# ─── 9.5 Debug Mode ───────────────────────────────────────────────────────────
heading2("9.5  Debug Mode", color=GREEN)

body("Append ?debug=true to expand each item with score breakdown fields.")
body("Frontend use cases for debug mode:")
bullet("Internal admin / QA dashboard — show alignment_score and cosine_similarity side-by-side.")
bullet("A/B test logging — capture full score breakdown alongside user interaction events.")
bullet("Do NOT expose alignment_score or cosine_similarity in production user-facing UI.")

# ─── 9.6 Interaction Events to Track ──────────────────────────────────────────
heading2("9.6  Interaction Events to Track", color=GREEN)

body("The backend does not automatically record user interactions with recommendations. "
     "The frontend must fire analytics events for these actions:")

add_table(
    ["User Action", "Event Name (suggested)", "Required Fields", "Purpose"],
    [
        ["Feed rendered",       "recommendation_impression", "creative_id, score, mode, rank_position, user_id",   "Measure click-through rate"],
        ["Item tapped / played","recommendation_play",       "creative_id, music_id, score, rank_position",        "Signal positive engagement"],
        ["Audio played >30s",   "recommendation_engaged",    "creative_id, play_duration_s, rank_position",        "Quality engagement signal"],
        ["Item shared",         "recommendation_share",      "creative_id, share_target",                          "Virality tracking"],
        ["Item saved",          "recommendation_save",       "creative_id, user_id",                               "Explicit positive signal"],
        ["Item skipped",        "recommendation_skip",       "creative_id, rank_position, time_visible_s",         "Negative signal for future ranking"],
        ["Feed scrolled past",  "recommendation_scroll_past","creative_id, rank_position",                         "Implicit negative signal"],
        ["Cold-start shown",    "cold_start_impression",     "user_id, profile_history_count",                     "Funnel tracking"],
    ],
    col_widths=[1.6, 1.9, 2.2, 1.8],
)

# ─── 9.7 State Management ─────────────────────────────────────────────────────
heading2("9.7  State Management Recommendations", color=GREEN)

heading3("Caching Strategy")
add_table(
    ["Scenario", "Recommended TTL", "Rationale"],
    [
        ["Authenticated user feed",  "60 – 120 seconds",  "New generations can shift personalisation"],
        ["Global / guest feed",      "5 – 10 minutes",    "Alignment scores change slowly"],
        ["Cold-start response",      "Do not cache",      "Re-check after each generation attempt"],
    ],
    col_widths=[2.0, 1.8, 2.7],
)

heading3("Pagination")
body("The API does not support offset/cursor pagination — it returns a fixed top-K list per call. "
     "To implement infinite scroll, increase the limit parameter (max 50) or re-fetch on scroll end.")

heading3("Required State Fields")
code_block(
"""interface RecommendationState {
  mode:                 'user' | 'global';
  is_cold_start:        boolean;
  profile_history_count: number;
  items:                RecommendationItem[];
  loaded_at:            number;    // Date.now() for TTL tracking
  user_id:              string | null;
}"""
)

# ─── 9.8 Error Handling ───────────────────────────────────────────────────────
heading2("9.8  Error Handling", color=GREEN)

add_table(
    ["HTTP Status", "Cause", "Frontend Action"],
    [
        ["200 + is_cold_start=true", "No user history",                   "Onboarding empty-state (not an error)"],
        ["200 + recommendations=[]", "No matching candidates in language", "Show 'nothing here yet' message; retry global"],
        ["422 Unprocessable",        "Bad query params",                   "Log error — indicates a code bug, not user error"],
        ["500 Internal Error",       "DB failure",                         "Show generic error state; retry with back-off"],
        ["Network timeout",          "Service unavailable",                "Show stale cached feed if available; else empty-state"],
    ],
    col_widths=[1.8, 2.0, 2.7],
)

# ══════════════════════════════════════════════════════════════════════════════
# SAVE
# ══════════════════════════════════════════════════════════════════════════════
output_path = "/path/to/repo/Edenn_Recommendation_Agent_Documentation.docx"
doc.save(output_path)
print(f"Saved: {output_path}")
