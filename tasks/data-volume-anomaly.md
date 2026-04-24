---
schedule: 30 9,15 * * 1-5
budget: 1.0
max_turns: 30
db_host: EOS_DB_HOST
---

# 任务：业务数据量环比异常巡检

## 数据库约束
- 权限要求：仅限只读。
- 严禁行为：禁止执行任何写操作。

## 背景说明
系统性检查核心业务表的日环比数据量波动，识别管道异常或业务异常。
检查基准：今日截至当前时间 vs 昨日同时段（等长窗口对比）。

## 监控规则

### 1. 一件代发订单量（pat_order）
检查今日新增订单量与昨日同时段对比，识别接单量骤降或激增。
- 异常定义：今日量 < 昨日量 × 30% 触发 CRITICAL（断流）；今日量 > 昨日量 × 300% 触发 WARN（激增）
- 查询示例：
  ```sql
  -- 今日截至当前
  SELECT COUNT(*) AS today_count
  FROM flying_api.pat_order
  WHERE CreationTime >= CURDATE()
    AND CreationTime < NOW()
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 昨日同时段（等长窗口）
  SELECT COUNT(*) AS yesterday_count
  FROM flying_api.pat_order
  WHERE CreationTime >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND CreationTime < DATE_SUB(NOW(), INTERVAL 1 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```

### 2. 转运单量（erp_transshipment）
检查今日新增转运单量与昨日同时段对比。
- 异常定义：今日量 < 昨日量 × 30% 触发 CRITICAL；今日量 > 昨日量 × 300% 触发 WARN
- 查询示例：
  ```sql
  SELECT COUNT(*) AS today_count
  FROM flying_api.erp_transshipment
  WHERE CreatedDate >= CURDATE()
    AND CreatedDate < NOW()
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  SELECT COUNT(*) AS yesterday_count
  FROM flying_api.erp_transshipment
  WHERE CreatedDate >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND CreatedDate < DATE_SUB(NOW(), INTERVAL 1 DAY)
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```

### 3. 打单量（OwnStatus=3 已打单）
检查今日完成打单的订单数量，识别打单流程中断。
- 异常定义：今日打单量 < 昨日打单量 × 20% 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT COUNT(*) AS today_labeled
  FROM flying_api.pat_order
  WHERE ScanTime >= CURDATE()
    AND ScanTime < NOW()
    AND OwnStatus = 3
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  SELECT COUNT(*) AS yesterday_labeled
  FROM flying_api.pat_order
  WHERE ScanTime >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND ScanTime < DATE_SUB(NOW(), INTERVAL 1 DAY)
    AND OwnStatus = 3
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```

### 4. 运费账单写入量（FIM_FixedPrice）
检查近两日账单条数对比，识别账单生成管道异常。
- 异常定义：昨日账单量 < 前日账单量 × 30% 触发 CRITICAL；前日无账单则跳过
- 查询示例：
  ```sql
  SELECT
    CheckDate,
    COUNT(*) AS bill_count,
    ROUND(SUM(Fee), 2) AS total_fee
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
    AND IsDeleted = 0
  GROUP BY CheckDate
  ORDER BY CheckDate DESC;
  ```

### 注意事项
- 如果当前时间在 09:00 之前，今日数据量极少属正常，此时环比检查意义不大，输出说明即可，不触发告警
- 周一与周五的量与周中可能有差异，若当日为周一且昨日为周日（无业务），请对比上周同工作日
- 环比倍数请输出精确数值（如"今日 120 单，昨日同时段 380 单，比率 31.6%"）

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 今日量/昨日量/比率
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "接单量骤降"、"打单量骤降"、"转运单骤降"、"账单量异常"、"数据量激增"
  - brief: 30字以内的一句话摘要，如无异常填"各业务量环比正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
