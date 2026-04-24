---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：SPM 运费账单一致性巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：PGPASSWORD=${SPM_DB_PASS} psql -h ${SPM_DB_HOST} -p ${SPM_DB_PORT} -U ${SPM_DB_USER} -d ${SPM_DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：PostgreSQL（shipmasterdb，52.53.147.239:5432）

## 背景说明
本巡检对应 eos-monitor-py/finance/spm_shippment_check.py 的监控逻辑。
核心逻辑：对比 fms_monthly_summary（月度账单汇总）与 fms_order_freight（订单运费明细）
是否一致，差异数据需人工核查并在月结前完成对账。
检查范围：最近 6 个月。

## 测试数据排除规则
**所有查询必须排除内部/测试客户（UPPER(customer_name) NOT IN ('EOS', 'T1')）：**
- `EOS`：运营商自身内部账号，不计入业务对账
- `T1`：测试客户账号，不代表真实业务数据

## 监控规则

### 1. 月度汇总 vs 订单明细运费差异
比较每个客户每月的 fms_monthly_summary.freight_fee 与 fms_order_freight.discount_amount 之和是否一致。
- 异常定义：差异绝对值 > 0.01 即告警，列出客户名、年月、差异金额
- 查询示例：
  ```sql
  SELECT
      s.customer_name,
      s.year,
      s.month,
      s.freight_fee                              AS summary_freight_fee,
      ROUND(SUM(f.discount_amount)::numeric, 2)  AS order_total,
      ROUND((s.freight_fee - SUM(f.discount_amount))::numeric, 2) AS diff
  FROM fms_monthly_summary s
  JOIN fms_order_freight f
      ON s.customer_id = f.customer_id
      AND s.year  = EXTRACT(YEAR  FROM f.charge_time)::int
      AND s.month = EXTRACT(MONTH FROM f.charge_time)::int
  WHERE s.is_deleted = false
      AND f.is_deleted = false
      AND UPPER(s.customer_name) NOT IN ('EOS', 'T1')
      AND (s.year * 100 + s.month) >=
          TO_CHAR(NOW() - INTERVAL '6 months', 'YYYYMM')::int
  GROUP BY s.customer_name, s.year, s.month, s.freight_fee
  HAVING ABS(s.freight_fee - SUM(f.discount_amount)) > 0.01
  ORDER BY ABS(s.freight_fee - SUM(f.discount_amount)) DESC;
  ```

### 2. 未结清客户账单
统计当前未结清（is_clear = false）的账单数量及未收款总额。
- 异常定义：未结清账单超过 30 张，或未收款总额超过 $100,000 触发 WARN
- 查询示例：
  ```sql
  SELECT
      customer_name,
      year,
      month,
      amount,
      receive,
      unpaid,
      closing_date
  FROM fms_customer_bill
  WHERE is_clear = false
      AND is_deleted = false
      AND UPPER(customer_name) NOT IN ('EOS', 'T1')
  ORDER BY closing_date ASC;
  ```

### 3. 已记录运费差异明细（近 30 天）
查看 fms_order_freight_difference 中近期新增的差异记录，确认是否有未处理的差异。
- 异常定义：近 30 天内存在差异记录即输出，供人工核查
- 查询示例：
  ```sql
  SELECT
      customer_name,
      tracking_number,
      service_code,
      month,
      check_date,
      received,
      amount,
      ROUND((received - amount)::numeric, 2) AS diff,
      remark
  FROM fms_order_freight_difference
  WHERE is_deleted = false
      AND check_date >= NOW() - INTERVAL '30 days'
  ORDER BY ABS(received - amount) DESC
  LIMIT 20;
  ```

### 4. 近 24 小时订单运费写入心跳
确认 fms_order_freight 在过去 24 小时内仍有数据写入，验证数据管道正常。
- 异常定义：24 小时内无新数据触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
      MAX(created_time)                                              AS last_insert,
      EXTRACT(EPOCH FROM (NOW() - MAX(created_time)))/3600         AS hours_ago
  FROM fms_order_freight
  WHERE is_deleted = false;
  ```

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 具体数值
- 检查项 1 如有差异，逐行列出：客户名 / 年月 / 汇总金额 / 明细合计 / 差额
- 检查项 2 输出：未结清账单数 / 未收款总额
- 检查项 3 输出：近 30 天差异记录条数及金额最大的前 5 条
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "运费差异"、"未结清账单"、"数据停写"、"数据异常"
  - brief: 30字以内的一句话摘要，如无异常填"全部检查通过"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
