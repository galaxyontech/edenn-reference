-- 按时长计费:视频配乐改成 30 秒一个计费单位,多图配乐加 15 秒起步
--
-- 改的是计量口径,不是价格。单价一分没动:
--
--   视频配乐  ¥0.9/次   ->  ¥0.9 / 每 30 秒(不足 30 秒按 30 秒)
--             30 秒的视频价格不变,90 秒的从 ¥0.9 变成 ¥2.7。
--   多图配乐  ¥0.7/秒   ->  ¥0.7/秒,不足 15 秒按 15 秒
--             20 秒的价格不变,10 秒的从 ¥7.0 变成 ¥10.5。
--
-- 公式:units = ⌈max(成品时长, min_billable_seconds) / unit_seconds⌉
--
-- 为什么放在价格行上而不是写死在代码里:块长和起步时长是商务参数,和单价一样。
-- 以后要把 30 秒改成 60 秒,应该是改一行价格,不是发一次版。
--
-- 为什么 usage_ledger 也要存这两列:和价格快照同一个理由 —— 改了口径以后,
-- 历史账单还要能自己解释"这 3 个单位是怎么来的"。老行是 NULL,表示"当时没有
-- 这个概念",而不是"按 1 秒一单位、无下限算的"(虽然结果一样,但 NULL 不撒谎)。
--
-- 用法:
--   psql "$CONN" -v ON_ERROR_STOP=1 -f 007_duration_billing.sql

BEGIN;

-- ============================================================
-- 1. 计量参数落到价格行
-- ============================================================

ALTER TABLE price_list
    ADD COLUMN IF NOT EXISTS unit_seconds INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS min_billable_seconds INTEGER NOT NULL DEFAULT 0;

ALTER TABLE price_list
    DROP CONSTRAINT IF EXISTS price_list_unit_seconds_check,
    DROP CONSTRAINT IF EXISTS price_list_min_billable_seconds_check;

ALTER TABLE price_list
    ADD CONSTRAINT price_list_unit_seconds_check
        CHECK (unit_seconds >= 1),
    ADD CONSTRAINT price_list_min_billable_seconds_check
        CHECK (min_billable_seconds >= 0);

-- 按次计费的产品带一个非默认的计量参数,只可能是配错了:那两个数在按次计费
-- 里没有任何作用,留着就是一条永远不会生效、却看起来生效了的规则。
ALTER TABLE price_list
    DROP CONSTRAINT IF EXISTS price_list_metering_only_per_second;
ALTER TABLE price_list
    ADD CONSTRAINT price_list_metering_only_per_second CHECK (
        billing_mode = 'per_second'
        OR (unit_seconds = 1 AND min_billable_seconds = 0)
    );

COMMENT ON COLUMN price_list.unit_seconds IS
    '一个单价买多少秒成品时长(视频配乐 30)。仅 per_second 有意义。';
COMMENT ON COLUMN price_list.min_billable_seconds IS
    '起步时长,短于此按此计费(多图配乐 15)。仅 per_second 有意义。';

-- ============================================================
-- 2. 账单行留下计量快照
-- ============================================================

ALTER TABLE usage_ledger
    ADD COLUMN IF NOT EXISTS unit_seconds INTEGER,
    ADD COLUMN IF NOT EXISTS min_billable_seconds INTEGER;

COMMENT ON COLUMN usage_ledger.unit_seconds IS
    '成交当时一个单位等于多少秒。NULL = 该行早于按时长计费。';
COMMENT ON COLUMN usage_ledger.min_billable_seconds IS
    '成交当时的起步时长。NULL = 该行早于按时长计费。';

-- ============================================================
-- 3. 两个产品的新口径
-- ============================================================
--
-- 只动口径,不动 unit_price_micros / teaser_unit_price_micros。
-- 用 WHERE 收敛到确实存在的行:这个文件在只跑过一半迁移的库上也应该是安全的。

UPDATE price_list
   SET billing_mode = 'per_second',
       unit_seconds = 30,
       min_billable_seconds = 0,
       note = '视频配乐。¥0.9 / 每 30 秒(不足 30 秒按 30 秒),'
              '新用户 90 天优惠 ¥0.45(@7.0)',
       updated_at = now()
 WHERE price_key = 'video_music';

UPDATE price_list
   SET billing_mode = 'per_second',
       unit_seconds = 1,
       min_billable_seconds = 15,
       note = '多图配乐。¥0.7/秒,不足 15 秒按 15 秒,'
              '新用户 90 天优惠 ¥0.35/秒(@7.0)',
       updated_at = now()
 WHERE price_key = 'image_music';

-- 档位级价格行(video_music:edenn_studio 这类)不在这里改:它们是按需加的,
-- 加的时候就该带上自己的计量参数。这里凭空替它们决定块长会改到没人预期的价。

COMMIT;

-- 校验:
--   SELECT price_key, billing_mode, unit_price_micros,
--          unit_seconds, min_billable_seconds
--     FROM price_list ORDER BY price_key;
--
-- 期望:
--   image_music  | per_second | 100000 | 1  | 15
--   video_music  | per_second | 128571 | 30 | 0
--
-- 换算自查(¥7.0/USD):
--   59 秒视频   -> ⌈59/30⌉ = 2 单位  -> $0.257142 (¥1.80)
--   10 秒多图   -> max(10,15) = 15   -> $1.500000 (¥10.50)
