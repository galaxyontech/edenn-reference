-- 定价种子数据
--
-- ⚠️ 计量口径已被 007_duration_billing.sql 改写:视频配乐从「按次」改成「每 30 秒
-- 一个单位」,多图配乐加了 15 秒起步。下面的单价数字仍然有效(一分没动),但
-- billing_mode 以 007 为准。两个文件按顺序跑,结果就是当前线上口径。
--
-- 这里的价格与当前表存储里生效的一致(RMB 基准价按 BILLING_RMB_PER_USD=7.0 换算):
--
--   视频配乐  标准 ¥0.9/次  -> $0.128571  = 128571 micros
--             优惠 ¥0.45/次 -> $0.064286  =  64286 micros
--   多图配乐  标准 ¥0.7/秒  -> $0.100000  = 100000 micros
--             优惠 ¥0.35/秒 -> $0.050000  =  50000 micros
--
-- 换算与线上代码同一套规则(rmb_to_micros:Decimal 除法 + ROUND_HALF_UP 到 1 micro),
-- 不是手算的近似值。注意 128571 ≠ 64286 × 2 —— 差 1 micro,因为两个价各自从 RMB
-- 独立换算并取整,这是对的:基准是人民币价,不是"优惠价翻倍"。
--
-- ON CONFLICT DO NOTHING 是刻意的:这个文件可能在管理员已经通过
-- PUT /api/v1/admin/pricing/... 改过价之后重跑,种子不该把线上价盖回去。
-- 迁移当天的权威来源是表存储的实际行(见回填脚本),不是本文件。
--
-- 用法:
--   psql "$CONN" -v ON_ERROR_STOP=1 -f 004_seed_pricing.sql

BEGIN;

INSERT INTO price_list
    (price_key, billing_mode, unit_price_micros, teaser_unit_price_micros, note)
VALUES
    ('video_music', 'per_request', 128571, 64286,
     '视频配乐。标准 ¥0.9/次,新用户 90 天优惠 ¥0.45/次(@7.0)'),
    ('image_music', 'per_second',  100000, 50000,
     '多图配乐。标准 ¥0.7/秒,新用户 90 天优惠 ¥0.35/秒(@7.0)')
ON CONFLICT (price_key) DO NOTHING;

-- 注意 audio_edit(/api/v1/jobs/audio-creative-edit)故意没有价格行。
-- 计费引擎遇到没有价格行的产品会记一条 warning 并计费 0 —— 也就是当前线上行为。
-- 这里不凭空造一个价:定价是商务决定,不是迁移的副产品。要上线时补一行即可。

-- ============================================================
-- 阶梯折扣 —— 刻意留空
-- ============================================================
--
-- 空表 = 没有任何账户拿到阶梯折扣 = 与今天的线上行为完全一致(今天只有
-- teaser 和标准价两轨)。这是迁移期唯一安全的默认值:迁移不该顺手改变谁付多少钱。
--
-- 档位是商务决定,给我门槛和折扣我来填。填之前先想清楚三件事:
--   1. 基数是 total_recharged_micros(累计充值,只增不减),不是当前余额 ——
--      所以折扣一旦到手不会因为花完钱而掉档。
--   2. 结算取"门槛 <= 账户累计充值"里最大的一档,不叠加。
--   3. 与 teaser 的优先级:合同价 > teaser > 阶梯折扣 > 市场价。新用户前 90 天
--      拿 teaser,不会同时再打阶梯折扣。
--
-- 模板(数字是示例,不要直接跑):
--
--   INSERT INTO discount_tiers (min_recharged_micros, discount_rate, note) VALUES
--       (  500 * 1000000, 0.0300, '累计充值 $500 起,95 折'),
--       ( 2000 * 1000000, 0.0700, '累计充值 $2000 起'),
--       (10000 * 1000000, 0.1200, '累计充值 $10000 起');
--
-- 合同价(企业合同客户这类)不在这里配,走 account_contract_prices,按账户按产品记绝对价。

COMMIT;

-- 校验:
--   SELECT price_key, billing_mode, unit_price_micros, teaser_unit_price_micros
--   FROM price_list ORDER BY price_key;
