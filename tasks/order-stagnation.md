---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：订单状态积压告警


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api

## 背景说明
检查各关键业务状态下长时间未推进的订单/转运单，识别流程卡点和异常积压。
与 order-consistency（数据质量）不同，本任务关注的是"状态停滞"：
订单/转运单创建了但长时间未流转到下一状态，意味着人工干预不及时或系统异常。
排除测试仓：WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'

## 监控规则

### 1. 一件代发订单 — 待处理积压（OwnStatus=1）
OwnStatus=1 表示待处理，超过 4 小时仍未处理说明有订单被遗漏或系统推送异常。
- 异常定义：超过 4 小时的待处理单 ≥ 10 触发 WARN；≥ 50 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    COUNT(*) AS stagnant_count,
    MIN(CreationTime) AS oldest_order_time,
    TIMESTAMPDIFF(HOUR, MIN(CreationTime), NOW()) AS max_wait_hours
  FROM flying_api.pat_order
  WHERE OwnStatus = 1
    AND CreationTime < DATE_SUB(NOW(), INTERVAL 4 HOUR)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 积压最久的前 5 条
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, CreationTime
  FROM flying_api.pat_order
  WHERE OwnStatus = 1
    AND CreationTime < DATE_SUB(NOW(), INTERVAL 4 HOUR)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  ORDER BY CreationTime ASC
  LIMIT 5;
  ```

### 2. 一件代发订单 — 已打单长时间未出库（OwnStatus=3）
打单完成（OwnStatus=3）超过 48 小时未进入已出库状态，可能是仓库扫描异常或物流交接问题。
- 异常定义：超过 48 小时的已打单 ≥ 20 触发 WARN；≥ 100 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    COUNT(*) AS stagnant_count,
    MIN(ScanTime) AS oldest_scan_time,
    TIMESTAMPDIFF(HOUR, MIN(ScanTime), NOW()) AS max_wait_hours
  FROM flying_api.pat_order
  WHERE OwnStatus = 3
    AND ScanTime < DATE_SUB(NOW(), INTERVAL 48 HOUR)
    AND ScanTime IS NOT NULL
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 积压最久的前 5 条
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, ScanTime
  FROM flying_api.pat_order
  WHERE OwnStatus = 3
    AND ScanTime < DATE_SUB(NOW(), INTERVAL 48 HOUR)
    AND ScanTime IS NOT NULL
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  ORDER BY ScanTime ASC
  LIMIT 5;
  ```

### 3. 转运单 — 处理中积压（State 非终态）
转运单长时间停留在非终态（非 600 取消、非 800 完成），说明转运流程卡住。
- 异常定义：State NOT IN (600, 800) 且超过 72 小时未更新 ≥ 10 触发 WARN；≥ 30 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    State,
    COUNT(*) AS stagnant_count,
    MIN(CreatedDate) AS oldest_time,
    TIMESTAMPDIFF(HOUR, MIN(CreatedDate), NOW()) AS max_wait_hours
  FROM flying_api.erp_transshipment
  WHERE State NOT IN (600, 800)
    AND CreatedDate < DATE_SUB(NOW(), INTERVAL 72 HOUR)
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY State
  ORDER BY stagnant_count DESC;

  -- 积压最久的前 5 条
  SELECT TransID, PO, CustomerName, State, CreatedDate
  FROM flying_api.erp_transshipment
  WHERE State NOT IN (600, 800)
    AND CreatedDate < DATE_SUB(NOW(), INTERVAL 72 HOUR)
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  ORDER BY CreatedDate ASC
  LIMIT 5;
  ```

### 4. 账单未生成积压（打单有量但无账单）
检查近 3 个工作日内，打单有量但 FIM_FixedPrice 无对应账单的日期，
说明账单生成任务漏跑。
- 异常定义：存在打单有量但无账单的日期即告警 CRITICAL
- 查询示例：
  ```sql
  -- 近 3 天每日打单量
  SELECT
    DATE(ScanTime) AS scan_date,
    COUNT(*) AS labeled_count
  FROM flying_api.pat_order
  WHERE ScanTime >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
    AND OwnStatus = 3
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY scan_date
  ORDER BY scan_date DESC;

  -- 近 3 天每日账单量
  SELECT
    CheckDate,
    COUNT(*) AS bill_count
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
    AND IsDeleted = 0
  GROUP BY CheckDate
  ORDER BY CheckDate DESC;
  -- 对比两个结果：若某日打单量 > 0 但账单量 = 0，则告警
  ```

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 积压单量及最久积压时间
- 若有积压，输出积压最久的前 5 条订单的 RecordNumber/TransID 及创建时间
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "待处理积压"、"打单未出库"、"转运单积压"、"账单未生成"
  - brief: 30字以内的一句话摘要，如无异常填"无异常积压"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
