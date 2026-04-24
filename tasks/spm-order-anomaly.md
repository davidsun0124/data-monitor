---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：SPM 订单停滞与账单逾期巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：PGPASSWORD=${SPM_DB_PASS} psql -h ${SPM_DB_HOST} -p ${SPM_DB_PORT} -U ${SPM_DB_USER} -d ${SPM_DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：PostgreSQL（shipmasterdb）

## 背景说明
实际数据快照（2026-03-25）：
- public.order 中 orderstatus=10 有361条，最老的从 2024-03-20 起停滞（超过1年）
- orderstatus=40 有237条、orderstatus=50 有1693条，最老从 2024-10-30 起
- fms_customer_bill 有23张未结清账单，含2025年9月以前的逾期款项
- fms_extra_fee 有待处理额外费用

## 测试数据排除规则
**所有查询必须排除以下内部/测试客户（UPPER(customer_name) NOT IN ('EOS', 'T1')）：**
- `T1`：测试客户账号，订单号如 cs001/cs002 等均为测试数据，历史积压单不代表真实业务问题
- `EOS`：运营商自身内部账号（EosName 常量），不计入业务监控指标
- 排除依据：代码中 RecalculateOrderFreightJob、UpdateLogisticsStatusJob 等均跳过这两个客户

已知 orderstatus 含义（通过数据分布推断）：
- 10：新建/待处理
- 20：处理中/已出单（正常运营中）
- 30：已打单/发货中
- 40：中间异常状态（长期积压）
- 50：另一中间状态（长期积压）
- 60：已完成/已关闭

## 监控规则

### 1. SPM 订单长期停滞（orderstatus 非终态积压）
orderstatus=10/40/50 的订单长期停留说明处理流程卡死或数据异常。
- 异常定义：
  - orderstatus=10 且创建超过 3 天：> 10 条触发 WARN；> 50 条触发 CRITICAL
  - orderstatus=40 或 50 且更新超过 7 天：> 20 条触发 WARN；> 100 条触发 CRITICAL
- 查询示例：
  ```sql
  -- orderstatus=10 长时间未处理（排除 EOS/T1 测试客户）
  SELECT
    orderstatus,
    COUNT(*) AS stuck_count,
    MIN(created_time) AS oldest_time,
    EXTRACT(DAY FROM NOW() - MIN(created_time)) AS max_days_stuck
  FROM public.order
  WHERE is_deleted = false
    AND orderstatus = 10
    AND created_time < NOW() - INTERVAL '3 days'
    AND UPPER(customer_name) NOT IN ('EOS', 'T1')
  GROUP BY orderstatus;

  -- orderstatus=40/50 长时间未流转（排除 EOS/T1 测试客户）
  SELECT
    orderstatus,
    COUNT(*) AS stuck_count,
    MIN(updated_time) AS oldest_update,
    EXTRACT(DAY FROM NOW() - MIN(updated_time)) AS max_days_stuck
  FROM public.order
  WHERE is_deleted = false
    AND orderstatus IN (40, 50)
    AND updated_time < NOW() - INTERVAL '7 days'
    AND UPPER(customer_name) NOT IN ('EOS', 'T1')
  GROUP BY orderstatus
  ORDER BY orderstatus;

  -- 积压最久的前 10 条（orderstatus=10，排除 EOS/T1 测试客户）
  SELECT
    id, customer_name, order_no, trackingnumber,
    orderstatus, created_time,
    EXTRACT(DAY FROM NOW() - created_time) AS days_stuck
  FROM public.order
  WHERE is_deleted = false
    AND orderstatus = 10
    AND created_time < NOW() - INTERVAL '3 days'
    AND UPPER(customer_name) NOT IN ('EOS', 'T1')
  ORDER BY created_time ASC
  LIMIT 10;
  ```

### 2. SPM 账单逾期未收款
fms_customer_bill.is_clear=false 且 closing_date（到期日）已过的账单。
- 异常定义：逾期超过 30 天的账单 > 3 张触发 WARN；逾期未收款总额 > $10,000 触发 CRITICAL
- 查询示例：
  ```sql
  -- 逾期账单按年月汇总
  SELECT
    year,
    month,
    COUNT(*) AS overdue_bill_count,
    ROUND(SUM(amount)::numeric, 2) AS total_amount,
    ROUND(SUM(unpaid)::numeric, 2) AS total_unpaid,
    MIN(closing_date) AS oldest_due_date,
    EXTRACT(DAY FROM NOW() - MIN(closing_date)) AS max_overdue_days
  FROM fms_customer_bill
  WHERE is_deleted = false
    AND is_clear = false
    AND closing_date < NOW() - INTERVAL '30 days'
  GROUP BY year, month
  ORDER BY year ASC, month ASC;

  -- 逾期最久的前 10 张账单
  SELECT
    customer_name,
    year,
    month,
    amount,
    unpaid,
    closing_date,
    EXTRACT(DAY FROM NOW() - closing_date) AS overdue_days
  FROM fms_customer_bill
  WHERE is_deleted = false
    AND is_clear = false
    AND closing_date < NOW() - INTERVAL '30 days'
  ORDER BY closing_date ASC
  LIMIT 10;
  ```

### 3. SPM 额外费用待审核积压（fms_extra_fee）
fms_extra_fee.status=1（待审核）或 feestate=1（待处理）的记录长期积压。
- 异常定义：待审核额外费用 > 10 条触发 WARN；积压超过 30 天的记录 > 5 条触发 CRITICAL
- 查询示例：
  ```sql
  -- 额外费用状态分布
  SELECT
    status,
    feestate,
    COUNT(*) AS cnt,
    ROUND(SUM(fee)::numeric, 2) AS total_fee,
    MIN(created_time) AS oldest_time,
    EXTRACT(DAY FROM NOW() - MIN(created_time)) AS max_pending_days
  FROM fms_extra_fee
  WHERE is_deleted = false
    AND status = 1  -- 待审核
  GROUP BY status, feestate
  ORDER BY cnt DESC;

  -- 积压超过 30 天
  SELECT COUNT(*) AS long_pending_count, ROUND(SUM(fee)::numeric, 2) AS total_fee
  FROM fms_extra_fee
  WHERE is_deleted = false
    AND status = 1
    AND created_time < NOW() - INTERVAL '30 days';
  ```

### 4. SPM 运费明细异常状态（fms_order_freight）
fee_status 非正常值的记录，说明运费计算或对账流程存在异常。
- 查询示例：
  ```sql
  -- 近 7 天 fee_status 分布（排除 EOS/T1 测试客户）
  SELECT
    fee_status,
    COUNT(*) AS cnt,
    ROUND(SUM(discount_amount)::numeric, 2) AS total_amount
  FROM fms_order_freight
  WHERE is_deleted = false
    AND charge_time >= NOW() - INTERVAL '7 days'
    AND UPPER(customer_name) NOT IN ('EOS', 'T1')
  GROUP BY fee_status
  ORDER BY fee_status;

  -- fee_status=2（异常）的近期记录（排除 EOS/T1 测试客户）
  SELECT customer_name, tracking_number, order_no, total_fee, charge_time
  FROM fms_order_freight
  WHERE is_deleted = false
    AND fee_status = 2
    AND charge_time >= NOW() - INTERVAL '7 days'
    AND UPPER(customer_name) NOT IN ('EOS', 'T1')
  ORDER BY charge_time DESC
  LIMIT 10;
  ```
  注意：若 fee_status=2 代表"已处理"而非"异常"，输出数量后跳过告警，备注说明。

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 具体数值
- 积压订单列出最老的 5 条（客户名/订单号/状态/积压天数）
- 逾期账单列出最久的 5 张（客户名/年月/未收金额/逾期天数）
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："SPM订单停滞"、"账单逾期"、"额外费用积压"、"运费异常"
  - brief: 30字以内，如无异常填"SPM数据正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
