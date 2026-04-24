---
schedule: 30 9,11,17 * * 1-5
budget: 1.0
max_turns: 20
db_host: EOS_DB_HOST
---

# 任务：数据库资源与慢查询巡检

## 数据库约束
- 权限要求：仅限只读。
- 严禁行为：禁止执行任何写操作。

## 背景说明
本巡检对应 eos-monitor-py/utils/starrocks_util.py 中的辅助检查逻辑，
以及对 StarRocks 自身运行状态的监控。
注意：StarRocks 不支持 information_schema.processlist，使用 SHOW FULL PROCESSLIST。

## 监控规则

### 1. 当前连接数
检查 StarRocks FE 当前连接数，防止连接泄露导致服务不稳定。
- 异常定义：非 Sleep 活跃连接超过 80 触发 WARN，超过 150 触发 CRITICAL
- 查询示例：
  ```sql
  SHOW FULL PROCESSLIST;
  -- 统计 Command != 'Sleep' 的行数为活跃连接数
  ```

### 2. 长时间运行的查询
检查是否存在执行超时的查询，可能阻塞资源。
- 异常定义：存在执行超过 30 秒的非 Sleep 查询触发 WARN，超过 5 分钟触发 CRITICAL
- 查询示例：
  ```sql
  SHOW FULL PROCESSLIST;
  -- 筛选 Command != 'Sleep' 且 Time > 30 的行
  ```

### 3. 各库磁盘占用
检查 flying_api / flying_api_pro 库的数据量，识别增长异常的大表。
- 异常定义：单库超过 200 GB 触发 WARN
- 查询示例：
  ```sql
  -- 各库大小
  SELECT TABLE_SCHEMA,
         ROUND(SUM(DATA_LENGTH) / 1024 / 1024 / 1024, 2) AS size_gb
  FROM information_schema.tables
  WHERE TABLE_SCHEMA IN ('flying_api', 'flying_api_pro')
  GROUP BY TABLE_SCHEMA
  ORDER BY size_gb DESC;

  -- flying_api 前 10 大表
  SELECT TABLE_NAME,
         ROUND(DATA_LENGTH / 1024 / 1024 / 1024, 3) AS size_gb,
         TABLE_ROWS
  FROM information_schema.tables
  WHERE TABLE_SCHEMA = 'flying_api'
  ORDER BY DATA_LENGTH DESC
  LIMIT 10;
  ```

### 4. 关键业务表近期数据写入心跳
验证核心表在过去 1 小时内仍有数据写入，确认数据管道正常。
- 异常定义：关键表 1 小时内无新数据触发 CRITICAL
- 查询示例：
  ```sql
  -- pat_order 最新写入时间
  SELECT MAX(CreationTime) AS last_insert_time,
         TIMESTAMPDIFF(MINUTE, MAX(CreationTime), NOW()) AS minutes_ago
  FROM flying_api.pat_order;

  -- FIM_FixedPrice 最新账单日期
  SELECT MAX(CheckDate) AS last_check_date
  FROM flying_api.FIM_FixedPrice;

  -- erp_transshipment 最新写入
  SELECT MAX(CreatedDate) AS last_insert_time,
         TIMESTAMPDIFF(MINUTE, MAX(CreatedDate), NOW()) AS minutes_ago
  FROM flying_api.erp_transshipment;
  ```

### 5. 审计日志近期错误查询
检查 StarRocks 审计表中近 30 分钟的异常查询（非正常结束）。
- 异常定义：失败查询超过 50 条触发 WARN
- 查询示例：
  ```sql
  SELECT COUNT(*) AS error_count
  FROM _starrocks_audit_db_.starrocks_audit_tbl
  WHERE queryTime >= DATE_SUB(NOW(), INTERVAL 30 MINUTE)
    AND state != 'EOF';
  ```

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 具体数值
- 连接数输出：活跃连接数 / 总连接数
- 磁盘输出：各库 GB 数 + 前 5 大表
- 数据心跳输出：各表最新写入时间及距今分钟数
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "数据停写"、"容量告警"、"慢查询"、"连接超限"、"数据异常"
  - brief: 30字以内的一句话摘要，如无异常填"全部检查通过"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
