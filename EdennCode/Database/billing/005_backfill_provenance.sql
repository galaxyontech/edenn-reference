-- 台账的迁移溯源列:让回填可重复执行
--
-- 为什么需要:usage_ledger 的主键是 BIGSERIAL,没有任何自然唯一键。回填脚本
-- 重跑一次就会把所有历史行再插一遍,而账单是按这张表出的 —— 重复的行等于凭空
-- 多收一遍钱。
--
-- 而回填几乎一定会跑不止一次:先 dry-run,再跑一遍,中途网络断了再续,
-- 切换当天还要补最后一段增量。更要紧的是,最后那次增量回填是在**双写已经开着**
-- 的时候跑的 —— 那时不能 TRUNCATE 重来,因为表里已经有实时写入的新行了。
--
-- 所以给回填的行打上来源标记(表存储的 RowKey),并对它建唯一索引。
-- 实时双写的行 source_row_key 为 NULL,部分索引不管它们,互不干扰。
--
-- 用法:
--   psql "$CONN" -v ON_ERROR_STOP=1 -f 005_backfill_provenance.sql

BEGIN;

ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS source_row_key TEXT;

-- 部分索引:只约束回填来的行。实时写入的 NULL 不占索引,也不会互相冲突。
CREATE UNIQUE INDEX IF NOT EXISTS usage_source_row_uniq
    ON usage_ledger (source_row_key) WHERE source_row_key IS NOT NULL;

COMMENT ON COLUMN usage_ledger.source_row_key IS
    '迁移来源:表存储 usage 表的 RowKey。实时写入为 NULL。'
    '唯一索引让回填脚本可以反复跑而不会重复计费。';

-- 流水表和账户表不需要这一列:wallet_txns 有 (account_id, idempotency_key)
-- 唯一约束,accounts/api_keys/account_identities 都有天然主键,回填直接
-- ON CONFLICT DO NOTHING 就是幂等的。

COMMIT;
