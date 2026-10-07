-- 数据库角色:三层权限
--
--   billing_readonly  只读 —— 日常查数、接 BI 工具、给不需要改数的人
--   billing_rw        读写 —— 运维改配置和资料;台账/流水只能追加,不能改删
--   billingadmin      超级用户(建库时自带)—— 破窗用,改 schema 或真出事时才用
--
-- 为什么 billing_rw 不能 UPDATE/DELETE 台账和流水:
--   wallet_txns 是 append-only 的账本。一旦允许改写,"余额 == 流水之和"这条
--   对账不变量就失去意义 —— 有人改了余额再改一条流水去圆,对账视图照样是 0 偏差,
--   而钱已经错了。记错了就记一条冲正(负数流水),这是会计的做法,不是技术洁癖。
--   usage_ledger 同理:它是开账单的依据,删一行等于凭空少收一笔且无迹可寻。
--
--   真需要改写(极少见)用 billingadmin,那是有意设成"要多一步"的。
--
-- ⚠️ 直连改数的两个陷阱(API 改不会有,直连会):
--   1. 改 api_keys 不会清进程内缓存 —— 被吊销的 key 最多还能再用 60 秒。
--      要立刻生效请走 DELETE /api/v1/admin/keys/{prefix}。
--   2. 改 accounts.balance_micros 不会清余额缓存(15 秒),也不会记流水 ——
--      对账视图会立刻报出偏差。要调余额请走 POST /api/v1/admin/accounts/{id}/recharge
--      (支持负数),它会同时记流水。
--
-- 用法(密码从 Key Vault 取,不落文件):
--   RO=$(az keyvault secret show --vault-name example-vault --name billing-pg-readonly-password --query value -o tsv)
--   RW=$(az keyvault secret show --vault-name example-vault --name billing-pg-rw-password       --query value -o tsv)
--   psql "host=... dbname=billing user=billingadmin sslmode=require" \
--     -v ro_password="$RO" -v rw_password="$RW" -f 002_roles.sql

-- ============================================================
-- 1. 只读角色
-- ============================================================

CREATE ROLE billing_readonly LOGIN PASSWORD :'ro_password';

GRANT CONNECT ON DATABASE billing TO billing_readonly;
GRANT USAGE ON SCHEMA public TO billing_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO billing_readonly;

-- 以后新建的表(每月新增的台账分区)自动可读,不用每次补授权。
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO billing_readonly;

COMMENT ON ROLE billing_readonly IS
    '只读。日常查数、BI 工具、以及所有不需要改数的人。';

-- ============================================================
-- 2. 读写角色
-- ============================================================

CREATE ROLE billing_rw LOGIN PASSWORD :'rw_password';

GRANT CONNECT ON DATABASE billing TO billing_rw;
GRANT USAGE ON SCHEMA public TO billing_rw;

-- 全部表可读
GRANT SELECT ON ALL TABLES IN SCHEMA public TO billing_rw;

-- 配置与资料:完整 CRUD
GRANT INSERT, UPDATE, DELETE ON
    accounts,
    account_identities,
    api_keys,
    price_list,
    discount_tiers,
    account_contract_prices
TO billing_rw;

-- 账本:只能追加,不能改删(见文件头的理由)
GRANT INSERT ON usage_ledger, wallet_txns TO billing_rw;

-- BIGSERIAL 主键要用到序列
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO billing_rw;

-- 新建对象的默认权限。注意这里只给 SELECT/INSERT ——
-- 每月新增的台账分区继承的是"可追加不可改删",与母表一致。
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT ON TABLES TO billing_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO billing_rw;

COMMENT ON ROLE billing_rw IS
    '读写。配置(价格/折扣档/合同)与资料(账户/身份/密钥)可增删改;'
    '台账与流水只能追加 —— 改错了记冲正,不要改历史。';

-- ============================================================
-- 3. 两个角色都不能建对象(避免有人在 public 里随手建表)
-- ============================================================

REVOKE CREATE ON SCHEMA public FROM billing_readonly, billing_rw;
