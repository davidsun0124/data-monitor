---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：客户信用与账期异常巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api

## 背景说明
crm_customercredit 记录每个客户的信用额度、当前余额和冻结状态。
实际查询发现：390 个客户中有 140 个余额为负（超额使用），20 个已冻结。
fim_billdetail 记录月度账单，invoicedate=NULL 表示未开票（未收款）。
本任务监控：
1. 余额超限严重（可能影响接单资格）
2. 账单逾期未收（invoicedate=NULL 且超过 enddate）
3. crm_customercredit.isfreeze=1 的客户仍在产生新订单（冻结客户绕过了信用控制）

## 监控规则

### 1. 信用余额严重透支客户
balance 为负数表示客户已超出信用额度。透支过深说明系统信用控制可能失效，
或客户长期欠款未处理。
- 异常定义：非测试类客户中 balance < -creditline（透支超过信用额度本身）触发 WARN；
  balance < -creditline × 2 触发 CRITICAL
- 查询示例：
  ```sql
  -- 透支超过信用额度的客户
  SELECT
    c.customerid,
    c2.shortname,
    c.creditline,
    c.balance,
    ROUND(c.balance - (-c.creditline), 2) AS overdraft_amount,
    c.isfreeze,
    c.modifytime AS last_update
  FROM flying_api.crm_customercredit c
  LEFT JOIN flying_api.crm_customer c2 ON c2.id = c.customerid
  WHERE c.balance < -(c.creditline)
    AND c.creditline > 0
  ORDER BY c.balance ASC
  LIMIT 20;

  -- 汇总：透支超过2倍信用额度的客户数
  SELECT COUNT(*) AS severe_overdraft_count
  FROM flying_api.crm_customercredit
  WHERE balance < -(creditline * 2)
    AND creditline > 0;
  ```

### 2. 冻结客户近期仍有新订单
isfreeze=1 的客户不应再接新订单，若仍有近 7 天的 pat_order 记录说明冻结机制未生效。
- 异常定义：发现即触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    p.customerid,
    c2.shortname,
    COUNT(*) AS new_order_count,
    MAX(p.creationtime) AS latest_order_time
  FROM flying_api.pat_order p
  JOIN flying_api.crm_customercredit c ON c.customerid = p.customerid
  LEFT JOIN flying_api.crm_customer c2 ON c2.id = p.customerid
  WHERE c.isfreeze = 1
    AND p.creationtime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND p.ownstatus NOT IN (98, 99, 100, 95)
    AND p.warehouseaddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY p.customerid, c2.shortname
  ORDER BY new_order_count DESC;
  ```

### 3. 账单逾期未收（fim_billdetail）
invoicedate = NULL 表示账单尚未收款，enddate 为到期日。
超过 enddate 30 天以上仍未收款属于逾期。
- 异常定义：逾期未收账单 > 5 张触发 WARN；逾期金额 > $50,000 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    billmonth,
    COUNT(*) AS overdue_bill_count,
    ROUND(SUM(amount - receive), 2) AS unpaid_amount,
    MIN(enddate) AS oldest_due_date,
    DATEDIFF(NOW(), MIN(enddate)) AS max_overdue_days
  FROM flying_api.fim_billdetail
  WHERE invoicedate IS NULL
    AND enddate < DATE_SUB(NOW(), INTERVAL 30 DAY)
    AND hide = 0
  GROUP BY billmonth
  ORDER BY billmonth DESC
  LIMIT 10;

  -- 逾期最久的前 10 张账单
  SELECT
    c.shortname,
    b.billmonth,
    b.amount,
    b.receive,
    ROUND(b.amount - b.receive, 2) AS unpaid,
    b.enddate,
    DATEDIFF(NOW(), b.enddate) AS overdue_days
  FROM flying_api.fim_billdetail b
  LEFT JOIN flying_api.crm_customer c ON c.id = b.customerid
  WHERE b.invoicedate IS NULL
    AND b.enddate < DATE_SUB(NOW(), INTERVAL 30 DAY)
    AND b.hide = 0
  ORDER BY b.enddate ASC
  LIMIT 10;
  ```
  注意：若 hide / enddate 字段名不符，先 SHOW COLUMNS 确认后适配。

### 4. 杂费（fim_misccharges）待审核积压
fim_misccharges.status=0 为待审核状态，长期积压说明财务审核流程卡滞。
- 异常定义：待审核杂费 > 30 条触发 WARN；> 100 条触发 CRITICAL；
  利息类（type=5）待审核 > 10 条触发 WARN（利息需优先处理）
- 查询示例：
  ```sql
  -- 各类型待审核杂费积压
  SELECT
    type,
    COUNT(*) AS pending_count,
    ROUND(SUM(fee), 2) AS total_fee,
    MIN(date) AS oldest_date,
    DATEDIFF(NOW(), MIN(date)) AS max_pending_days
  FROM flying_api.fim_misccharges
  WHERE status = 0
  GROUP BY type
  ORDER BY pending_count DESC;

  -- 待审核超过 30 天的汇总
  SELECT COUNT(*) AS long_pending_count, ROUND(SUM(fee), 2) AS total_fee
  FROM flying_api.fim_misccharges
  WHERE status = 0
    AND date < DATE_SUB(NOW(), INTERVAL 30 DAY);
  ```

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 具体数值
- 信用透支列出最严重的前 5 个客户（客户简称/信用额/当前余额/透支额）
- 逾期账单列出逾期最久的前 5 张（客户/月份/未收金额/逾期天数）
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："信用透支"、"冻结客户接单"、"账单逾期"、"杂费积压"
  - brief: 30字以内，如无异常填"客户信用与账期正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
