-- Edenn 计费库 schema
-- 目标库:billing-db / billing (Azure Postgres Flexible Server 16, japaneast)
-- 设计文档:docs/superpowers/specs/2026-07-31-billing-postgres-migration-design.md
--
-- 约定(全库一致):
--   * 金额一律 BIGINT 整数 micros,$1 = 1,000,000。绝不用浮点 —— 浮点会让余额判零
--     失灵、流水求和对不上账。
--   * 时间一律 TIMESTAMPTZ。库级 timezone=UTC,应用侧只传 tz-aware 值。
--   * 台账与流水区分 occurred_at(业务发生)与 recorded_at(入库):补数据、重放、
--     迁移时两者会分开。出账单用前者,审计用后者。

BEGIN;

-- ============================================================
-- 1. 账户与钱包
-- ============================================================

CREATE TABLE accounts (
    account_id             TEXT PRIMARY KEY,
    entity_type            TEXT NOT NULL DEFAULT 'individual'
                             CHECK (entity_type IN ('individual', 'company')),
    registered_name        TEXT NOT NULL,
    id_number              TEXT NOT NULL DEFAULT '',
    address                TEXT NOT NULL DEFAULT '',
    email                  TEXT NOT NULL DEFAULT '',
    phone                  TEXT NOT NULL DEFAULT '',
    note                   TEXT NOT NULL DEFAULT '',
    -- 余额刻意不加非负约束:单笔任务允许把余额扣成小幅负数(事后结清型),
    -- 拦截发生在提交时的计费闸门(余额 <= 0 -> 402),不在这里。
    balance_micros         BIGINT NOT NULL DEFAULT 0,
    -- 累计入账(充值 + 正向调整),只增不减 —— 低余额提醒和阶梯折扣的基数。
    total_recharged_micros BIGINT NOT NULL DEFAULT 0
                             CHECK (total_recharged_micros >= 0),
    is_active              BOOLEAN NOT NULL DEFAULT true,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_via            TEXT NOT NULL DEFAULT 'admin'
                             CHECK (created_via IN ('admin', 'self_serve', 'migration')),
    created_by             TEXT NOT NULL DEFAULT '',
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- created_at 是 90 天优惠期的起算点,也是账户列表的排序键。
CREATE INDEX accounts_created_at_idx ON accounts (created_at DESC);

COMMENT ON COLUMN accounts.balance_micros IS
    '当前余额(micro-USD)。可为负:单笔任务允许透支,闸门在提交时拦。';
COMMENT ON COLUMN accounts.total_recharged_micros IS
    '累计入账,只增不减。低余额提醒阈值与阶梯折扣档位都以此为基数。';

-- ============================================================
-- 2. API 密钥
-- ============================================================

CREATE TABLE api_keys (
    -- 主键即完整密钥的 SHA-256:天然唯一(碰撞需 2^128 量级),而且鉴权热路径
    -- 本来就按它点读 —— 主键即查询键,不需要额外的代理主键。
    key_hash     TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    key_prefix   TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_via  TEXT NOT NULL DEFAULT 'admin'
                   CHECK (created_via IN ('admin', 'self_serve', 'migration')),
    created_by   TEXT NOT NULL DEFAULT '',
    revoked_at   TIMESTAMPTZ,
    -- 不要每请求更新:那会把纯读的热路径变成写路径并制造行级争用。
    -- 应用侧内存聚合、按分钟级精度批量刷。
    last_used_at TIMESTAMPTZ,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    CONSTRAINT api_keys_revoked_implies_inactive
        CHECK (revoked_at IS NULL OR is_active = false)
);

-- 前缀只有 54 bit 熵(sk- + 9 个 base64url 字符),1.6 亿把 key 才 50% 碰撞率。
-- 约束成本为零,却把"极不可能的静默损坏"变成"极不可能的一次重试"——
-- 尤其因为按前缀吊销一旦碰撞会同时命中两个账户的 key。
CREATE UNIQUE INDEX api_keys_prefix_uniq ON api_keys (key_prefix);
CREATE INDEX api_keys_account_active_idx ON api_keys (account_id) WHERE is_active;

-- ============================================================
-- 3. 身份索引(手机 / 邮箱 / Firebase UID -> 账户)
-- ============================================================

CREATE TABLE account_identities (
    kind       TEXT NOT NULL CHECK (kind IN ('phone', 'email', 'firebase')),
    -- 规范化后的原值。表存储时代要哈希是因为 RowKey 不允许 / \ # ? 等字符;
    -- Postgres 没有这个限制,直接存原值,运维可读。
    value      TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 主键即保证"一个联系方式只属于一个账户":表存储时代靠 create_entity 撞行
    -- 实现的原子占位,这里是主键冲突,语义更直白。
    PRIMARY KEY (kind, value)
);

CREATE INDEX account_identities_account_idx ON account_identities (account_id);

-- ============================================================
-- 4. 定价:市场价 / 阶梯折扣 / 合同价
-- ============================================================

CREATE TABLE price_list (
    price_key                TEXT PRIMARY KEY,   -- 'video_music' | 'image_music:edenn_studio'
    billing_mode             TEXT NOT NULL
                               CHECK (billing_mode IN ('per_request', 'per_second')),
    unit_price_micros        BIGINT NOT NULL CHECK (unit_price_micros > 0),
    -- NULL = 该产品没有新用户优惠价。比表存储时代的空字符串干净(那是 MERGE 语义
    -- 逼出来的妥协)。
    teaser_unit_price_micros BIGINT
                               CHECK (teaser_unit_price_micros IS NULL
                                      OR teaser_unit_price_micros > 0),
    note                     TEXT NOT NULL DEFAULT '',
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE price_list IS
    '市场价(挂牌价)。档位级 product:model_spec 优先于产品级 product。';

CREATE TABLE discount_tiers (
    tier_id              BIGSERIAL PRIMARY KEY,
    min_recharged_micros BIGINT NOT NULL CHECK (min_recharged_micros >= 0),
    -- 0.1000 = 减 10%(打 9 折)。< 1 保证价格不会归零或为负。
    discount_rate        NUMERIC(5, 4) NOT NULL
                           CHECK (discount_rate >= 0 AND discount_rate < 1),
    note                 TEXT NOT NULL DEFAULT '',
    effective_from       TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to         TIMESTAMPTZ
);

-- 同一门槛在同一时刻只能有一档生效。
CREATE UNIQUE INDEX discount_tiers_threshold_active
    ON discount_tiers (min_recharged_micros) WHERE effective_to IS NULL;

COMMENT ON TABLE discount_tiers IS
    '按累计充值额的阶梯折扣(全局配置,非按账户)。结算时取门槛 <= 账户累计充值 '
    '中最大的一档。取整规则:floor(市场价 * (1 - rate)),向下取整对客户有利。';

CREATE TABLE account_contract_prices (
    contract_id       BIGSERIAL PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    price_key         TEXT NOT NULL,
    billing_mode      TEXT NOT NULL
                        CHECK (billing_mode IN ('per_request', 'per_second')),
    unit_price_micros BIGINT NOT NULL CHECK (unit_price_micros >= 0),
    contract_ref      TEXT NOT NULL DEFAULT '',   -- 合同编号,给财务对
    -- 带时间区间是刻意的:合同改价不是 UPDATE,而是把旧行 effective_to 收口、
    -- 插一条新行。几个月后回头看某张账单,能查到当时生效的是哪份合同、什么价。
    effective_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by        TEXT NOT NULL DEFAULT '',
    CONSTRAINT contract_period_sane
        CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- 同一账户同一产品在同一时刻只能有一个生效合同价。
CREATE UNIQUE INDEX account_contract_active
    ON account_contract_prices (account_id, price_key) WHERE effective_to IS NULL;
CREATE INDEX account_contract_lookup_idx
    ON account_contract_prices (account_id, price_key, effective_from DESC);

COMMENT ON TABLE account_contract_prices IS
    '特殊客户(如大客户)的逐产品绝对合同价。用绝对价而非折扣率:以后上调市场价时,'
    '合同客户的价不会被静默抬高。';

-- ============================================================
-- 5. 用量台账(详单数据源)—— 按月分区
-- ============================================================

CREATE TABLE usage_ledger (
    usage_id     BIGSERIAL,
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
        status = 'completed' OR billed_amount_micros = 0),

    PRIMARY KEY (usage_id, occurred_at)   -- 分区表的主键必须包含分区键
) PARTITION BY RANGE (occurred_at);

CREATE INDEX usage_account_time_idx  ON usage_ledger (account_id, occurred_at DESC);
CREATE INDEX usage_time_idx          ON usage_ledger (occurred_at);
CREATE INDEX usage_model_time_idx    ON usage_ledger (model_spec, occurred_at);
CREATE INDEX usage_job_idx           ON usage_ledger (job_id);

COMMENT ON TABLE usage_ledger IS
    '按任务的用量台账。按月分区:时间范围查询只碰相关分区,过期数据 DROP PARTITION '
    '一秒完成。需要一个每月建下月分区的定时任务(或 pg_partman)。';

-- 迁移前的历史数据落这里(没有价格快照,price_source 为 NULL)
CREATE TABLE usage_ledger_pre2026 PARTITION OF usage_ledger
    FOR VALUES FROM (MINVALUE) TO ('2026-01-01 00:00:00+00');

-- ============================================================
-- 6. 资金流水(append-only)
-- ============================================================

CREATE TABLE wallet_txns (
    txn_id               BIGSERIAL PRIMARY KEY,
    account_id           TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    txn_type             TEXT NOT NULL
                           CHECK (txn_type IN ('debit', 'recharge', 'adjustment')),
    amount_micros        BIGINT NOT NULL CHECK (amount_micros <> 0),
    balance_after_micros BIGINT NOT NULL,
    job_id               TEXT,
    -- 扣费用 'job-{job_id}',手动操作用 UUID。让数据库挡住重复扣费 ——
    -- 表存储时代靠确定性 RowKey 实现的幂等,这里是唯一约束。
    idempotency_key      TEXT NOT NULL,
    note                 TEXT NOT NULL DEFAULT '',
    created_by           TEXT NOT NULL DEFAULT '',
    created_via          TEXT NOT NULL DEFAULT 'system'
                           CHECK (created_via IN ('system', 'admin', 'migration')),
    occurred_at          TIMESTAMPTZ NOT NULL,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX wallet_txns_idem_uniq ON wallet_txns (account_id, idempotency_key);
CREATE INDEX wallet_txns_account_time_idx ON wallet_txns (account_id, occurred_at DESC);

COMMENT ON TABLE wallet_txns IS
    'append-only。永远不要 UPDATE 一条交易记录;记错了记一条冲正。'
    '扣款与记流水必须在同一个事务里完成。';

-- ============================================================
-- 7. 对账视图 —— 有它才叫账本,没有它只是个 CRUD 表
-- ============================================================

CREATE VIEW balance_reconciliation AS
SELECT a.account_id,
       a.registered_name,
       a.balance_micros                            AS balance_micros,
       COALESCE(SUM(t.amount_micros), 0)           AS ledger_sum_micros,
       a.balance_micros - COALESCE(SUM(t.amount_micros), 0) AS drift_micros
FROM accounts a
LEFT JOIN wallet_txns t ON t.account_id = a.account_id
GROUP BY a.account_id, a.registered_name, a.balance_micros;

COMMENT ON VIEW balance_reconciliation IS
    '每个账户的余额必须等于其全部流水之和。挂成定时任务查 drift_micros <> 0,'
    '非空即告警 —— 这是账本正确性的最后一道防线。';

COMMIT;
