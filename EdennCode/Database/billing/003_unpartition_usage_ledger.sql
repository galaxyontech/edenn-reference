-- usage_ledger:去掉月分区,改回普通表
--
-- 为什么撤销 001 里的分区设计:
--
-- Postgres 的范围分区不会自动建分区。没有对应分区的时间段插行会直接报
--     no partition of relation "usage_ledger" found for row
-- 也就是说必须有个定时任务一直往前铺分区,而那个任务一旦静默失败,
-- 下个月 1 号 00:00 UTC 起所有计费写入开始报错。用"账本可能断流"换性能,
-- 对计费台账是划不来的买卖。
--
-- 而分区的三个好处在当前量级逐条不成立:
--   * 时间范围查询快 —— 索引已经做到了(usage_account_time_idx)
--   * DROP PARTITION 秒删旧数据 —— 计费台账不删,税务和审计要留数年
--   * 索引与 vacuum 变小 —— 要到 5000 万行量级才有感,这里一年百万级
--
-- 什么时候回头再分区:单表超过约 5000 万行,或出现了真正的按月清理需求。
-- 那时用 pg_partman(Azure Flexible Server 支持)由数据库后台进程维护分区,
-- 而不是外部 cron —— 那才是"自动铺分区"的正确形态。pg_partman 也支持把
-- 已有的普通表就地转成分区表,现在不分区不会堵死那条路。
--
-- 用法:
--   psql "$CONN" -v ON_ERROR_STOP=1 -f 003_unpartition_usage_ledger.sql

BEGIN;

-- 安全闸:这个文件只在台账还是空的时候可以跑(迁移前)。有数据就停下,
-- 因为重建表会丢数据 —— 宁可报错也不要静默删账单。
DO $$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM usage_ledger;
    IF n > 0 THEN
        RAISE EXCEPTION
            'usage_ledger 已有 % 行,拒绝重建。要迁移已有数据请先备份并改用 '
            'ALTER TABLE ... DETACH / 数据搬迁流程。', n;
    END IF;
END;
$$;

DROP TABLE usage_ledger CASCADE;   -- 连带删掉 usage_ledger_pre2026 分区

CREATE TABLE usage_ledger (
    -- 不再是复合主键:(usage_id, occurred_at) 那个形状是分区键强加的,
    -- 普通表用单列代理主键更直白。
    usage_id     BIGSERIAL PRIMARY KEY,
    job_id       TEXT NOT NULL,
    -- 匿名请求(AUTH_MODE != enforce 时可能发生)没有账户。
    account_id   TEXT REFERENCES accounts(account_id) ON DELETE RESTRICT,
    key_prefix   TEXT,                -- 归属机制上线前的历史行为 NULL
    endpoint     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'canceled')),
    auth_mode    TEXT NOT NULL DEFAULT '',

    -- 内部成本(我们付给供应商的钱),与向客户收的钱是两回事,数量级也不同
    prompt_tokens          INTEGER NOT NULL DEFAULT 0,
    completion_tokens      INTEGER NOT NULL DEFAULT 0,
    total_tokens           INTEGER NOT NULL DEFAULT 0,
    token_cost_micros      BIGINT  NOT NULL DEFAULT 0,
    music_provider         TEXT    NOT NULL DEFAULT '',
    model_spec             TEXT    NOT NULL DEFAULT '',
    generation_call_count  INTEGER NOT NULL DEFAULT 0,
    generation_cost_micros BIGINT  NOT NULL DEFAULT 0,
    total_cost_micros      BIGINT  NOT NULL DEFAULT 0,

    -- 计费(向客户收的钱)+ 价格快照。
    -- 快照的意义:改价、改折扣档、改合同之后,历史账单纹丝不动,且每一张都能自证
    -- "当时市场价多少、给了什么折扣、依据哪条策略"。
    billing_mode           TEXT CHECK (billing_mode IN ('per_request', 'per_second')),
    billed_units           INTEGER NOT NULL DEFAULT 0 CHECK (billed_units >= 0),
    list_unit_price_micros BIGINT,   -- 成交当时的市场价(即使走合同价也记,便于算让利)
    unit_price_micros      BIGINT,   -- 实际生效单价
    billed_amount_micros   BIGINT NOT NULL DEFAULT 0,
    price_source           TEXT CHECK (price_source IN
                             ('contract', 'teaser', 'volume_tier', 'list')),
    price_ref              BIGINT,   -- contract_id 或 tier_id,便于溯源
    -- 生成列:由市场价和生效价推导,不可能写错也不会漂移。
    discount_rate NUMERIC(9, 6) GENERATED ALWAYS AS (
        CASE
            WHEN list_unit_price_micros IS NULL OR list_unit_price_micros = 0 THEN NULL
            ELSE 1 - unit_price_micros::numeric / list_unit_price_micros
        END
    ) STORED,

    video_duration_s NUMERIC(10, 3),   -- 按秒计费的单位来源
    latency_ms       INTEGER,
    occurred_at      TIMESTAMPTZ NOT NULL,                 -- 业务发生(任务结算那一刻)
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),   -- 入库

    -- 业务规则下沉到数据库:表存储时代这两条只能靠代码自觉。
    CONSTRAINT usage_billed_needs_price CHECK (
        billed_amount_micros = 0
        OR (unit_price_micros IS NOT NULL AND price_source IS NOT NULL)),
    CONSTRAINT usage_only_completed_is_billed CHECK (
        status = 'completed' OR billed_amount_micros = 0)
);

CREATE INDEX usage_account_time_idx  ON usage_ledger (account_id, occurred_at DESC);
CREATE INDEX usage_time_idx          ON usage_ledger (occurred_at);
CREATE INDEX usage_model_time_idx    ON usage_ledger (model_spec, occurred_at);
CREATE INDEX usage_job_idx           ON usage_ledger (job_id);

COMMENT ON TABLE usage_ledger IS
    '按任务的用量台账(详单数据源)。刻意不分区 —— 见 003_unpartition_usage_ledger.sql '
    '文件头。超过约 5000 万行时用 pg_partman 就地转分区。';

-- 重建表会丢掉 002 授过的权限,补回来。
GRANT SELECT, INSERT ON usage_ledger TO billing_rw;
GRANT SELECT           ON usage_ledger TO billing_readonly;
GRANT USAGE, SELECT ON SEQUENCE usage_ledger_usage_id_seq TO billing_rw;

COMMIT;

-- 校验(应该是普通表,relkind = 'r' 而非 'p'):
--   SELECT relname, relkind FROM pg_class WHERE relname = 'usage_ledger';
--   \dp usage_ledger
