---
schedule: 0 9,13,17 * * 1-5
budget: 0.8
max_turns: 15
db_host: EOS_DB_HOST
---

# 任务：StarRocks 集群健康巡检

## 数据库约束
- 权限要求：仅限只读。
- 严禁行为：禁止执行任何写操作。

## 背景说明
本巡检检查 StarRocks 集群自身的节点状态、Tablet 健康度和导入任务状态，
与 db-health 的"连接数/慢查询/磁盘/数据心跳"互为补充，聚焦集群层面可用性。

## 监控规则

### 1. FE 节点状态
检查 Frontend 节点是否全部存活，是否有节点掉线。
- 异常定义：存在 Alive=false 的 FE 节点触发 CRITICAL
- 查询：
  ```sql
  SHOW PROC '/frontends';
  -- 检查 Alive 列，所有节点应为 true
  -- 同时记录 IsMaster 列标识主节点
  ```

### 2. BE 节点状态
检查 Backend 节点是否全部存活，是否有节点下线。
- 异常定义：存在 Alive=false 的 BE 节点触发 CRITICAL；存在 SystemDecommissioned=true 触发 WARN
- 查询：
  ```sql
  SHOW PROC '/backends';
  -- 检查 Alive 列，所有节点应为 true
  -- 检查 SystemDecommissioned 列是否有节点被系统下线
  -- 同时记录各 BE 的 DataUsedCapacity、TotalCapacity 用于磁盘评估
  ```

### 3. BE 磁盘使用率
从 BE 节点信息中计算各节点磁盘使用率，预防磁盘打满导致写入失败。
- 异常定义：任一 BE 节点磁盘使用率超过 75% 触发 WARN，超过 90% 触发 CRITICAL
- 计算方式：DataUsedCapacity / TotalCapacity（来自 SHOW PROC '/backends' 结果）

### 4. Tablet 健康度
检查集群中是否存在不健康的 Tablet（副本缺失、版本不一致等），不健康 Tablet 可能导致查询报错。
- 异常定义：不健康 Tablet 数量超过 0 触发 WARN；超过 100 触发 CRITICAL
- 查询：
  ```sql
  SHOW PROC '/statistic';
  -- 关注 UnhealthyTabletNum 列（如该列存在）
  -- 或关注 TotalReplicaCount vs HealthyReplicaCount 的差值
  ```

### 5. 近期导入任务失败
检查过去 1 小时内是否有 LOAD 任务异常结束，失败的导入会造成数据缺失。
- 异常定义：CANCELLED 状态的导入任务超过 3 个触发 WARN；超过 10 个触发 CRITICAL
- 查询：
  ```sql
  -- Routine Load 状态
  SHOW ROUTINE LOAD;
  -- 检查 State 列，关注 PAUSED / CANCELLED 状态

  -- Stream/Broker Load 近期失败（如有权限）
  SELECT COUNT(*) AS failed_loads
  FROM information_schema.loads
  WHERE CREATE_TIME >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
    AND STATE = 'CANCELLED';
  ```
  注意：information_schema.loads 在部分 StarRocks 版本中可用，若报错可跳过此查询，仅检查 Routine Load。

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 具体数值
- FE/BE 输出：节点总数、存活数，列出异常节点 IP
- 磁盘输出：各 BE 节点使用率百分比
- Tablet 输出：不健康 Tablet 数量
- 导入任务输出：PAUSED/CANCELLED 任务数及任务名
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "FE节点异常"、"BE节点异常"、"磁盘告警"、"Tablet异常"、"导入任务失败"
  - brief: 30字以内的一句话摘要，如无异常填"集群全部节点健康"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
