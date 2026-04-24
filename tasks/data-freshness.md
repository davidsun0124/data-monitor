---
schedule: 0 8,9,14,16 * * 1-5
budget: 1.0
max_turns: 30
db_host: EOS_DB_HOST
---

# 任务：核心表数据新鲜度全量巡检

## 数据库约束
- 权限要求：仅限只读。
- 严禁行为：禁止执行任何写操作。

## 背景说明
系统性检查所有核心业务表的最新数据写入时间，确认数据同步管道持续正常运行。
与 db-health 的"数据心跳"不同，本任务覆盖更多表，并按业务重要性分级告警。
检查逻辑：距当前时间超过阈值则告警，工作日非业务时段（00:00-08:00）延迟标准适当放宽。

## 监控规则

### 1. 订单核心表（CRITICAL 级，阈值 2 小时）
以下表为实时业务数据，2 小时内无新数据说明接单或同步管道故障。
- 查询示例：
  ```sql
  -- 一件代发订单
  SELECT MAX(CreationTime) AS last_time,
         TIMESTAMPDIFF(MINUTE, MAX(CreationTime), NOW()) AS minutes_ago
  FROM flying_api.pat_order;

  -- 转运单
  SELECT MAX(CreatedDate) AS last_time,
         TIMESTAMPDIFF(MINUTE, MAX(CreatedDate), NOW()) AS minutes_ago
  FROM flying_api.erp_transshipment;
  ```

### 2. 账单/财务表（WARN 级，阈值 26 小时）
账单为 T+1 日结，允许最多 26 小时无新数据，超出则说明账单生成管道异常。
- 查询示例：
  ```sql
  -- FF 运费账单
  SELECT MAX(CheckDate) AS last_check_date,
         DATEDIFF(CURDATE(), MAX(CheckDate)) AS days_ago
  FROM flying_api.FIM_FixedPrice
  WHERE IsDeleted = 0;

  -- 账单明细（以 CreateTime 为写入时间，若列存在）
  SELECT MAX(CreateTime) AS last_time,
         TIMESTAMPDIFF(HOUR, MAX(CreateTime), NOW()) AS hours_ago
  FROM flying_api.FIM_FixedPrice
  WHERE IsDeleted = 0;
  ```
  注意：若 CreateTime 列不存在，仅以 CheckDate 判断。

### 3. 仓储/配件表（WARN 级，阈值 4 小时）
仓储操作表和配件表，4 小时内无写入说明仓库系统对接异常。
- 查询示例：
  ```sql
  -- 仓库操作表（若存在）
  SELECT MAX(OperateTime) AS last_time,
         TIMESTAMPDIFF(MINUTE, MAX(OperateTime), NOW()) AS minutes_ago
  FROM flying_api.erp_warehouse_operate
  WHERE 1=1
  LIMIT 1;
  -- 若表不存在则跳过此项

  -- 配件单
  SELECT MAX(CreatedDate) AS last_time,
         TIMESTAMPDIFF(HOUR, MAX(CreatedDate), NOW()) AS hours_ago
  FROM flying_api.erp_parts
  WHERE 1=1
  LIMIT 1;
  -- 若表不存在则跳过此项
  ```

### 4. 审计/日志类表（INFO 级，阈值 1 小时）
StarRocks 审计日志表，1 小时内无新查询记录说明数据库可能停止服务。
- 查询示例：
  ```sql
  SELECT MAX(queryTime) AS last_query,
         TIMESTAMPDIFF(MINUTE, MAX(queryTime), NOW()) AS minutes_ago
  FROM _starrocks_audit_db_.starrocks_audit_tbl;
  ```

### 注意事项
- 若某张表查询报错（表不存在、权限不足），输出 [SKIP] + 原因，不触发告警
- 当前时间在 00:00-08:00 之间时，对 CRITICAL 级阈值放宽至 10 小时（夜间低峰期）
- 输出每张表的：表名 / 最新数据时间 / 距今时长 / 状态

## 输出要求
- 每张表输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 最新数据时间及距今时长
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "订单停写"、"账单停写"、"仓储停写"、"数据延迟"
  - brief: 30字以内的一句话摘要，如无异常填"全部核心表数据正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
