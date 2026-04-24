---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：订单物流状态停滞检测


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api

## 背景说明
系统后台 AllocationPullOrderStatusJob + PullOrderStatusJob 定期拉取 FedEx/UPS/Amazon/17Track 物流状态。
若物流 API 超时/限流/配置错误，订单物流状态会长期停留在 LabelCreate(0) 或 null，
客户无法追踪包裹，仓库也无法识别异常件。
各物流状态含义：0=LabelCreate 已出单, 1=Pickup 已揽收, 2=InTransit 在途, 3=Delivered 已签收, 4=Exception 异常

## 监控规则

### 1. 已出库但物流状态从未更新的订单
OwnStatus=4（已发货）且发货超过 3 天，但 LogisticsStatus 仍为 null 或 0（LabelCreate），
说明 PullOrderStatusJob 从未成功拉取该批订单的物流状态。
- 异常定义：此类订单 > 50 触发 WARN；> 200 触发 CRITICAL
- 查询示例：
  ```sql
  -- LogisticsStatus 字段可能在 pat_order 本身，也可能在关联的 erp_logistics 表
  -- 先尝试直接查 pat_order
  SELECT
    COUNT(*) AS stuck_count,
    MIN(ShippingTime) AS oldest_ship_time,
    TIMESTAMPDIFF(DAY, MIN(ShippingTime), NOW()) AS max_days_stuck
  FROM flying_api.pat_order
  WHERE OwnStatus = 4
    AND ShippingTime < DATE_SUB(NOW(), INTERVAL 3 DAY)
    AND ShippingTime >= DATE_SUB(NOW(), INTERVAL 30 DAY)
    AND (LogisticsStatus IS NULL OR LogisticsStatus = 0)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 按物流商分组统计
  SELECT ServiceCode,
         COUNT(*) AS stuck_count,
         ROUND(AVG(TIMESTAMPDIFF(DAY, ShippingTime, NOW())), 1) AS avg_days_stuck
  FROM flying_api.pat_order
  WHERE OwnStatus = 4
    AND ShippingTime < DATE_SUB(NOW(), INTERVAL 3 DAY)
    AND ShippingTime >= DATE_SUB(NOW(), INTERVAL 30 DAY)
    AND (LogisticsStatus IS NULL OR LogisticsStatus = 0)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY ServiceCode
  ORDER BY stuck_count DESC;
  ```
  注意：若 LogisticsStatus 字段不在 pat_order 表中，尝试：
  SHOW COLUMNS FROM flying_api.pat_order; 然后根据实际字段名适配查询。

### 2. 在途超期未签收订单（可能丢失）
OwnStatus=4，发货超过 30 天（FedEx/UPS 标准时限），LogisticsStatus 仍非 Delivered(3)。
可能是包裹丢失、地址错误或物流异常。
- 异常定义：此类订单 > 10 触发 WARN；> 30 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    ServiceCode,
    COUNT(*) AS overdue_count,
    MIN(ShippingTime) AS oldest_ship_time
  FROM flying_api.pat_order
  WHERE OwnStatus = 4
    AND ShippingTime < DATE_SUB(NOW(), INTERVAL 30 DAY)
    AND (LogisticsStatus IS NULL OR LogisticsStatus IN (0, 1, 2))
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY ServiceCode
  ORDER BY overdue_count DESC;
  ```

### 3. 物流异常件积压（LogisticsStatus=4）
物流状态为 Exception（4）且超过 7 天未处理/更新的订单，需人工介入跟进。
- 异常定义：物流异常件积压 > 20 触发 WARN；> 50 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    ServiceCode,
    COUNT(*) AS exception_count,
    MIN(ShippingTime) AS oldest_exception_time
  FROM flying_api.pat_order
  WHERE LogisticsStatus = 4
    AND OwnStatus = 4
    AND ShippingTime < DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY ServiceCode
  ORDER BY exception_count DESC;
  ```

### 4. 超期待处理取消订单（ClearPendingOrderJob 健康检测）
ClearPendingOrderJob 每批自动取消创建超过 30 天的 Pending 订单。
若此类订单大量存在，说明该任务异常失败。
- 异常定义：OwnStatus=1 且 CreationTime 超过 35 天的订单 > 10 触发 WARN
- 查询示例：
  ```sql
  SELECT COUNT(*) AS orphan_pending_count,
         MIN(CreationTime) AS oldest_pending_time
  FROM flying_api.pat_order
  WHERE OwnStatus = 1
    AND CreationTime < DATE_SUB(NOW(), INTERVAL 35 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 具体数值
- 按物流商（ServiceCode）分组输出停滞/超期订单分布
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："物流状态停滞"、"超期未签收"、"物流异常件积压"、"超期待处理订单"
  - brief: 30字以内，如无异常填"物流状态追踪正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
