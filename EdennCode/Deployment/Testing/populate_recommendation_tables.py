"""
Create and populate the three recommendation-layer tables from existing t_media_work data.

Tables created:
  user_profile            – static user metadata (inferred from uid patterns + creation history)
  user_creation_summary   – per-user aggregates derived from t_media_work
  user_interaction_event  – synthetic but realistic engagement events (impression / click / watch / …)

Safe to re-run: all DDL uses IF NOT EXISTS and INSERTs use ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import random
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from psycopg2.extras import Json

from EdennCode.Deployment.postgres_wrapper import PostgresClient

random.seed(42)

# ── locale / platform inference ───────────────────────────────────────────────

LANGUAGE_LOCALE: dict[str, tuple[str, str]] = {
    "CHINESE_MAINLAND": ("zh-CN", "Asia/Shanghai"),
    "Chinese":          ("zh-CN", "Asia/Shanghai"),
    "CHINESE":          ("zh-CN", "Asia/Shanghai"),
    "ENGLISH_US":       ("en-US", "America/New_York"),
    "ENGLISH":          ("en-US", "America/New_York"),
    "English":          ("en-US", "America/New_York"),
    "KOREAN":           ("ko-KR", "Asia/Seoul"),
    "GERMAN":           ("de-DE", "Europe/Berlin"),
    "Japanese":         ("ja-JP", "Asia/Tokyo"),
    "Hindi":            ("hi-IN", "Asia/Kolkata"),
}
DEFAULT_LOCALE = ("zh-CN", "Asia/Shanghai")

PLATFORM_WEIGHTS: dict[str, list[float]] = {
    "zh-CN": [0.50, 0.40, 0.10],
    "en-US": [0.30, 0.30, 0.40],
    "ko-KR": [0.45, 0.45, 0.10],
    "de-DE": [0.20, 0.25, 0.55],
    "ja-JP": [0.40, 0.45, 0.15],
    "hi-IN": [0.35, 0.55, 0.10],
}

# ── content taxonomy ──────────────────────────────────────────────────────────

CONTENT_KEYWORDS: dict[str, list[str]] = {
    "automotive":   ["汽车", "驾驶", "沙漠", "公路", "车", "drive", "car"],
    "sports":       ["篮球", "骑行", "运动", "健身", "球场", "NBA", "basketball", "cheer"],
    "retail":       ["购物", "商场", "零食", "品牌", "促销", "shopping", "snack"],
    "nature":       ["山峰", "云雾", "樱花", "花海", "自然", "海岸", "mountain", "flower"],
    "travel":       ["旅行", "旅游", "飞机", "俯瞰", "探索", "travel", "flight"],
    "fashion":      ["服饰", "穿搭", "白色", "极简", "时装", "fashion", "style"],
    "food":         ["美食", "餐厅", "饮料", "饮品", "咖啡", "food", "drink"],
    "dance":        ["舞者", "舞台", "编舞", "律动", "dance", "halftime"],
    "festival":     ["春节", "节日", "晚宴", "灯光", "庆典", "festival", "gala"],
    "office":       ["办公", "工作", "效率", "创造力", "office", "work"],
    "kids":         ["孩子", "童趣", "课堂", "children", "kids", "play"],
    "jewelry":      ["手链", "饰品", "项链", "首饰", "珠串", "jewelry"],
}

STYLE_SUMMARIES: dict[str, str] = {
    "automotive":   "Creates premium automotive and road-trip content with sweeping cinematic audio.",
    "sports":       "Produces high-energy sports and athletics content with driving, motivational music.",
    "retail":       "Creates commercial retail and seasonal shopping content with upbeat festive soundtracks.",
    "nature":       "Focuses on serene nature and landscape content with ambient, atmospheric instrumentals.",
    "travel":       "Produces travel and aerial exploration content with immersive cinematic soundscapes.",
    "fashion":      "Creates minimalist fashion and lifestyle content with elegant, refined background music.",
    "food":         "Produces food and beverage content with warm, inviting ambient soundscapes.",
    "dance":        "Specialises in performance and dance content with high-energy rhythmic music.",
    "festival":     "Creates festive event and gala content with celebratory, uplifting orchestral music.",
    "office":       "Produces professional office and productivity content with calm, focused background audio.",
    "kids":         "Creates joyful children and family content with playful, cheerful music.",
    "jewelry":      "Specialises in elegant jewelry and accessories content with soft, refined instrumentals.",
    "general":      "Creates diverse video content across multiple categories and styles.",
}


def infer_tags(title: str, description: str) -> list[str]:
    text = ((title or "") + " " + (description or "")).lower()
    return [
        tag for tag, kws in CONTENT_KEYWORDS.items()
        if any(kw.lower() in text for kw in kws)
    ] or ["general"]


def most_common(items: list[Any]) -> Any:
    return Counter(items).most_common(1)[0][0] if items else None


# ── DDL ───────────────────────────────────────────────────────────────────────

DDL = [
    """
    CREATE TABLE IF NOT EXISTS user_profile (
        user_id           VARCHAR(64)  PRIMARY KEY,
        locale            VARCHAR(20),
        language          VARCHAR(30),
        timezone          VARCHAR(50),
        platform          VARCHAR(20),
        signup_date       TIMESTAMP,
        subscription_tier VARCHAR(20)  NOT NULL DEFAULT 'free',
        is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
        created_at        TIMESTAMP    NOT NULL DEFAULT NOW(),
        updated_at        TIMESTAMP    NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_creation_summary (
        user_id                VARCHAR(64)   PRIMARY KEY REFERENCES user_profile(user_id),
        total_creations        INTEGER       NOT NULL DEFAULT 0,
        first_creation_time    TIMESTAMP,
        last_creation_time     TIMESTAMP,
        preferred_language     VARCHAR(30),
        preferred_model        VARCHAR(50),
        preferred_voice        VARCHAR(30),
        language_distribution  JSONB,
        model_distribution     JSONB,
        voice_distribution     JSONB,
        avg_duration_seconds   NUMERIC(8,2),
        total_tokens_used      BIGINT        NOT NULL DEFAULT 0,
        music_taxonomy         JSONB,
        semantic_style_summary TEXT,
        updated_at             TIMESTAMP     NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_interaction_event (
        id                       BIGSERIAL    PRIMARY KEY,
        user_id                  VARCHAR(64)  NOT NULL,
        work_id                  VARCHAR(64)  NOT NULL,
        event_type               VARCHAR(30)  NOT NULL,
        watch_duration_ms        INTEGER,
        watch_completion_pct     NUMERIC(5,2),
        audio_listen_duration_ms INTEGER,
        session_id               VARCHAR(64),
        platform                 VARCHAR(20),
        event_time               TIMESTAMP    NOT NULL DEFAULT NOW(),
        metadata                 JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_uie_user_time ON user_interaction_event(user_id, event_time)",
    "CREATE INDEX IF NOT EXISTS idx_uie_work_type ON user_interaction_event(work_id, event_type)",
]

# ── main ─────────────────────────────────────────────────────────────────────

with PostgresClient.from_user_db_env() as client:

    # ── 1. Create tables ──────────────────────────────────────────────────────
    for stmt in DDL:
        client.run_sql(stmt)
    print("✓ schema ready")

    # ── 2. Load source data ───────────────────────────────────────────────────
    works = client.run_sql("""
        SELECT uid, work_id, title, description, model, voice, language,
               duration_seconds, tokens_used, create_time
        FROM   t_media_work
        WHERE  is_deleted = FALSE AND status = 2
        ORDER  BY create_time
    """)

    by_user: dict[str, list] = defaultdict(list)
    for w in works:
        by_user[w["uid"]].append(w)

    all_uids   = sorted(by_user.keys())
    work_index = [(w["work_id"], w["uid"], w["create_time"],
                   float(w["duration_seconds"] or 30)) for w in works]

    print(f"  loaded {len(works)} works across {len(all_uids)} users")

    # ── 3. user_profile ───────────────────────────────────────────────────────
    profiles: list[dict] = []
    for uid in all_uids:
        uworks = by_user[uid]
        langs      = [w["language"] for w in uworks if w["language"]]
        dom_lang   = most_common(langs) or "CHINESE_MAINLAND"
        locale, tz = LANGUAGE_LOCALE.get(dom_lang, DEFAULT_LOCALE)
        platform   = random.choices(
            ["ios", "android", "web"],
            weights=PLATFORM_WEIGHTS.get(locale, [0.40, 0.40, 0.20]),
        )[0]
        has_premium = any(
            w.get("model") in ("edenn_studio", "edenn_enhanced") for w in uworks
        )
        tier       = "pro" if has_premium else random.choice(["free", "free", "basic"])
        earliest   = min(w["create_time"] for w in uworks)
        signup     = earliest - timedelta(days=random.randint(7, 120))
        profiles.append(dict(
            user_id=uid, locale=locale, language=dom_lang,
            timezone=tz, platform=platform, signup_date=signup,
            subscription_tier=tier,
        ))

    existing_profiles = client.run_sql("SELECT COUNT(*) n FROM user_profile")[0]["n"]
    if existing_profiles:
        print(f"✓ user_profile: {existing_profiles} rows already present, skipping")
    else:
        client.bulk_insert_rows("user_profile", profiles)
        n = client.run_sql("SELECT COUNT(*) n FROM user_profile")[0]["n"]
        print(f"✓ user_profile: {n} rows")

    # ── 4. user_creation_summary ──────────────────────────────────────────────
    summaries: list[dict] = []
    for uid in all_uids:
        uworks  = by_user[uid]
        langs   = [w["language"] for w in uworks if w["language"]]
        models  = [w["model"]    for w in uworks if w["model"]]
        voices  = [w["voice"]    for w in uworks if w["voice"]]
        durs    = [float(w["duration_seconds"]) for w in uworks if w["duration_seconds"]]
        tokens  = sum(w["tokens_used"] or 0 for w in uworks)
        times   = [w["create_time"] for w in uworks]

        tag_counts: Counter = Counter()
        for w in uworks:
            tag_counts.update(infer_tags(w["title"], w["description"]))
        top_tag = tag_counts.most_common(1)[0][0] if tag_counts else "general"

        summaries.append(dict(
            user_id               = uid,
            total_creations       = len(uworks),
            first_creation_time   = min(times),
            last_creation_time    = max(times),
            preferred_language    = most_common(langs),
            preferred_model       = most_common(models),
            preferred_voice       = most_common(voices),
            language_distribution = Json(dict(Counter(langs))),
            model_distribution    = Json(dict(Counter(models))),
            voice_distribution    = Json(dict(Counter(voices))),
            avg_duration_seconds  = round(sum(durs) / len(durs), 2) if durs else None,
            total_tokens_used     = tokens,
            music_taxonomy        = Json(dict(tag_counts.most_common())),
            semantic_style_summary= STYLE_SUMMARIES.get(top_tag, STYLE_SUMMARIES["general"]),
        ))

    existing_summaries = client.run_sql("SELECT COUNT(*) n FROM user_creation_summary")[0]["n"]
    if existing_summaries:
        print(f"✓ user_creation_summary: {existing_summaries} rows already present, skipping")
    else:
        client.bulk_insert_rows("user_creation_summary", summaries)
        n = client.run_sql("SELECT COUNT(*) n FROM user_creation_summary")[0]["n"]
        print(f"✓ user_creation_summary: {n} rows")

    # ── 5. user_interaction_event ─────────────────────────────────────────────
    existing = client.run_sql("SELECT COUNT(*) n FROM user_interaction_event")[0]["n"]
    if existing:
        print(f"✓ user_interaction_event: {existing:,} rows already present, skipping")
    else:
        PLATFORMS = ["ios", "android", "web"]
        SOURCES   = ["feed", "search", "profile", "discovery"]
        CHANNELS  = ["wechat", "weibo", "link", "instagram"]

        events: list[dict] = []
        for work_id, creator_uid, base_time, dur_s in work_index:
            dur_ms   = int(dur_s * 1000)
            n_viewers = random.randint(4, 16)
            viewers  = random.sample(all_uids, min(n_viewers, len(all_uids)))
            if creator_uid not in viewers:
                viewers[0] = creator_uid

            for viewer in viewers:
                plat = random.choice(PLATFORMS)
                sess = f"sess_{viewer[:8]}_{work_id[:8]}"
                t    = base_time + timedelta(
                    hours=random.randint(0, 72), minutes=random.randint(0, 59)
                )

                def ev(etype, **kw) -> dict:
                    base = dict(user_id=viewer, work_id=work_id, event_type=etype,
                                watch_duration_ms=None, watch_completion_pct=None,
                                audio_listen_duration_ms=None,
                                session_id=sess, platform=plat, event_time=t,
                                metadata=None)
                    base.update(kw)
                    return base

                events.append(ev("impression",
                                  metadata=Json({"source": random.choice(SOURCES)})))

                if random.random() < 0.35:          # 35 % → click
                    t += timedelta(seconds=random.randint(1, 10))
                    events.append(ev("click"))

                    if random.random() < 0.60:       # 60 % of clicks → complete
                        pct   = random.uniform(80, 100)
                        wms   = int(dur_ms * pct / 100)
                        t    += timedelta(milliseconds=wms)
                        events.append(ev("watch_complete",
                                          watch_duration_ms=wms,
                                          watch_completion_pct=round(pct, 2)))

                        if random.random() < 0.45:   # audio listen-through
                            lms = int(dur_ms * random.uniform(0.5, 1.0))
                            t  += timedelta(seconds=2)
                            events.append(ev("audio_listen",
                                              audio_listen_duration_ms=lms,
                                              watch_completion_pct=round(lms / dur_ms * 100, 2)))

                        if random.random() < 0.20:   # like
                            t += timedelta(seconds=random.randint(1, 5))
                            events.append(ev("like"))

                        if random.random() < 0.10:   # save
                            t += timedelta(seconds=random.randint(1, 3))
                            events.append(ev("save"))

                        if random.random() < 0.05:   # share
                            t += timedelta(seconds=random.randint(1, 5))
                            events.append(ev("share",
                                              metadata=Json({"channel": random.choice(CHANNELS)})))
                    else:                            # abandon
                        pct = random.uniform(5, 75)
                        wms = int(dur_ms * pct / 100)
                        t  += timedelta(milliseconds=wms)
                        events.append(ev("watch_abandon",
                                          watch_duration_ms=wms,
                                          watch_completion_pct=round(pct, 2)))

        inserted = client.bulk_insert_rows("user_interaction_event", events)
        print(f"✓ user_interaction_event: {inserted:,} rows inserted")

    # ── 6. Summary ────────────────────────────────────────────────────────────
    print("\n── final counts ──────────────────────────────────────────────")
    for tbl in ["user_profile", "user_creation_summary", "user_interaction_event"]:
        n = client.run_sql(f"SELECT COUNT(*) n FROM {tbl}")[0]["n"]
        print(f"  {tbl:<35} {n:>8,} rows")

    # quick sanity: event breakdown
    print("\n── interaction event breakdown ───────────────────────────────")
    breakdown = client.run_sql("""
        SELECT event_type, COUNT(*) n
        FROM   user_interaction_event
        GROUP  BY event_type ORDER BY n DESC
    """)
    for r in breakdown:
        print(f"  {r['event_type']:<20} {r['n']:>8,}")

    # sample user_creation_summary row
    print("\n── sample creation summary (1 user) ─────────────────────────")
    sample = client.run_sql("""
        SELECT ucs.user_id, up.locale, up.subscription_tier,
               ucs.total_creations, ucs.preferred_model,
               ucs.music_taxonomy, ucs.semantic_style_summary
        FROM   user_creation_summary ucs
        JOIN   user_profile up USING (user_id)
        ORDER  BY ucs.total_creations DESC LIMIT 1
    """)
    for k, v in sample[0].items():
        print(f"  {k}: {v}")
