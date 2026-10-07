-- api_keys.key_suffix:让控制台能显示一把认得出来的遮罩密钥
--
-- 为什么需要:控制台的 API key 页面按 ModelGateway / ModelVendorAlt 的样子分成两列 ——
-- 「追踪 ID」(key_prefix,用量详单和 ?key_prefix= 过滤用的那一段)和「密钥」
-- (sk-XbgqH8R8r…RLYA)。没有后 4 位,密钥列只能是一串固定的圆点,那一列就
-- 不携带任何信息,用户仍然分不出手里的 key 是不是列表里的这一把。
--
-- 泄露了多少:密钥 "sk-" 之后是 43 个 base64url 字符(约 256 bit)。前 9 个
-- 已经在 key_prefix 里了,再加后 4 个,一共露 13 个,剩下约 180 bit 猜不出来。
-- 这正是 ModelGateway 和 ModelVendorAlt 的做法。要改这两个长度前先重算这句话。
--
-- 老 key 怎么办:不回填,也回填不了 —— 明文只在创建时存在过,库里只有哈希。
-- 它们的 key_suffix 是 '',控制台把这种情况渲染成未揭示的尾巴,而不是编一个。
--
-- 用法:
--   psql "$CONN" -v ON_ERROR_STOP=1 -f 006_key_suffix.sql

BEGIN;

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_suffix TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN api_keys.key_suffix IS
    '完整密钥的后 4 个字符,只为控制台展示 sk-xxx…LAST4。'
    '本列新增之前铸出的 key 为空串:明文早已不存在,无法回填。';

COMMIT;
