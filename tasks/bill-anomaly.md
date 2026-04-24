---
db_host: EOS_DB_HOST
schedule: 30 13 * * 1-5
max_turns: false
budget: false
---

# 任务：账单异常巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api

## 背景说明
本任务覆盖三个账单层面的异常：
1. 系统自动补偿杂费检测（对应 CheckBillAmountJob：账单金额与 FeeAmount 不一致时
   系统自动插入 MiscChargeFee 调平，频繁出现说明上游费用计算存在持续性 Bug）
2. 月度账单未自动创建（CreateVerificationBillJob 不重试，失败整月账单缺失）
3. 利息核销金额一致性（对应 eos-monitor-py/finance/added_service_check.py）

## 监控规则

### 1. DataRecovery 自动补偿杂费异常
系统 CheckBillAmountJob 在账单金额与 FeeAmount 不一致时，
会自动插入 FIM_MiscCharges 记录进行差额补偿（可识别标记如 Remark 或 Type 字段）。
该记录的存在是正常补偿，但若近期数量多说明上游计算持续出错。
- 异常定义：近 30 天 FIM_MiscCharges 中自动补偿记录 > 20 条触发 WARN；> 50 条触发 CRITICAL
- 查询示例：
  ```sql
  -- 近 30 天各客户的自动补偿杂费情况
  -- （尝试通过 Remark 字段识别系统自动生成的记录，若字段不存在尝试 Type 或 Source）
  SELECT
    CustomerId,
    COUNT(*) AS recovery_count,
    ROUND(SUM(Fee), 2) AS total_fee,
    MIN(Date) AS first_date,
    MAX(Date) AS last_date
  FROM flying_api.FIM_MiscCharges
  WHERE Date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
    AND IsDeleted = 0
    AND (
      Remark LIKE '%DataRecovery%'
      OR Remark LIKE '%自动调整%'
      OR Remark LIKE '%系统修正%'
    )
  GROUP BY CustomerId
  ORDER BY recovery_count DESC
  LIMIT 20;

  -- 若 Remark 字段没有上述关键字，则输出近 30 天所有 Type 的分布供分析
  SELECT Type, COUNT(*) AS cnt, ROUND(SUM(Fee), 2) AS total
  FROM flying_api.FIM_MiscCharges
  WHERE Date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
    AND IsDeleted = 0
  GROUP BY Type
  ORDER BY cnt DESC;
  ```

### 2. 月度账单创建缺失检查
每月首日 CreateVerificationBillJob 自动为客户创建 FIM_BillDetail 账单，
该任务不重试；如果当月账单行数与上月差异超过 20%，说明批量创建失败。
- 异常定义：本月已创建账单数量 < 上月的 70% 触发 WARN（月初3日内宽限，3日后严格检查）
- 查询示例：
  ```sql
  -- 本月账单条数
  SELECT COUNT(*) AS this_month_count
  FROM flying_api.FIM_BillDetail
  WHERE YEAR(CreationTime) = YEAR(NOW())
    AND MONTH(CreationTime) = MONTH(NOW())
    AND IsDeleted = 0;

  -- 上月账单条数
  SELECT COUNT(*) AS last_month_count
  FROM flying_api.FIM_BillDetail
  WHERE YEAR(CreationTime) = YEAR(DATE_SUB(NOW(), INTERVAL 1 MONTH))
    AND MONTH(CreationTime) = MONTH(DATE_SUB(NOW(), INTERVAL 1 MONTH))
    AND IsDeleted = 0;

  -- 当前日期是否 >= 3（月初3日内不触发告警）
  SELECT DAY(NOW()) AS current_day;
  ```
  注意：若 FIM_BillDetail 无 CreationTime 列，改用 Month 字段（格式 YYYYMM）筛选。

### 3. 利息杂费与核销金额一致性（eos-monitor-py/added_service_check.py 迁移）
FIM_MiscCharges 中 Type=5 的利息费用，应与 FIM_Verification 中的 OverdueMoney 对应匹配。
按客户/月度分组比较，差异说明有利息已入账但核销漏记，或核销了未入账的利息。
- 异常定义：差异绝对值 > 0.01 的客户月份存在即触发 WARN
- 查询示例：
  ```sql
  -- 按客户月度汇总利息杂费
  SELECT
    mc.CustomerId,
    DATE_FORMAT(mc.Date, '%Y-%m') AS bill_month,
    ROUND(SUM(mc.Fee), 2) AS total_interest_fee
  FROM flying_api.FIM_MiscCharges mc
  WHERE mc.Type = 5
    AND mc.IsDeleted = 0
    AND mc.Date >= DATE_SUB(NOW(), INTERVAL 3 MONTH)
  GROUP BY mc.CustomerId, DATE_FORMAT(mc.Date, '%Y-%m');

  -- 按客户月度汇总核销利息
  SELECT
    bd.CustomerId,
    DATE_FORMAT(v.CreationTime, '%Y-%m') AS bill_month,
    ROUND(SUM(v.OverdueMoney), 2) AS total_verification
  FROM flying_api.FIM_Verification v
  JOIN flying_api.FIM_BillDetail bd ON bd.Id = v.Billid
  WHERE v.IsDeleted = 0
    AND v.CreationTime >= DATE_SUB(NOW(), INTERVAL 3 MONTH)
  GROUP BY bd.CustomerId, DATE_FORMAT(v.CreationTime, '%Y-%m');
  ```
  对比两个结果集，输出差异绝对值 > 0.01 的 (CustomerId, 月份, 利息金额, 核销金额, 差额)。
  若 FIM_Verification 表不存在，输出 [SKIP] 并说明。

### 4. 欠费账单积压检查
过期未结清的账单（BillStatus=Overdue 或超出约定结算日仍 Normal）数量及金额异常。
- 异常定义：欠费账单 > 50 张触发 WARN；未收款总额 > $500,000 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    COUNT(*) AS overdue_count,
    ROUND(SUM(Amount), 2) AS total_amount,
    MIN(CreationTime) AS oldest_bill_date
  FROM flying_api.FIM_BillDetail
  WHERE IsDeleted = 0
    AND IsClear = 0
    AND (
      BillStatus = 1  -- Overdue
      OR (BillStatus = 0 AND CreationTime < DATE_SUB(NOW(), INTERVAL 60 DAY))
    );
  ```
  注意：若字段名称与实际不符（如 IsClear/BillStatus 字段名不同），先 DESC 表结构再适配查询。

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 具体数值
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："自动补偿杂费异常"、"月度账单缺失"、"利息核销不一致"、"欠费积压"
  - brief: 30字以内，如无异常填"账单数据正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
