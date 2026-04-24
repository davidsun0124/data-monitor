---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：库存异常巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api
- 排除测试仓：productwarehouse <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'

## 背景说明
库存核心计算：可用库存 = productstockin - productstockout - productzhanyong
- 负库存（available < 0）说明出库/占用超过在库数量，触发超卖风险
- 虚拟库存（virtualinventory ≠ 0）由 CountVirtualInventoryJob 每日23点清理；
  若持续存在说明任务连续失败或遇到算法边界情况（超过10个库位时跳过处理）
- erp_stock 为按仓库维度的汇总库存；erp_stocklocationmanage 为库位级别明细

## 监控规则

### 1. 负库存 SKU（非测试仓）
可用库存 = productstockin - productstockout - productzhanyong < 0
说明该 SKU 被超卖或出库数据异常，直接影响客户发货和财务对账。
- 异常定义：非测试仓出现负库存 SKU > 0 即触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    productwarehouse,
    COUNT(*) AS negative_sku_count,
    SUM(productstockin - productstockout - productzhanyong) AS total_available,
    MIN(productstockin - productstockout - productzhanyong) AS worst_sku
  FROM flying_api.erp_stock
  WHERE (productstockin - productstockout - productzhanyong) < 0
    AND productwarehouse <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY productwarehouse
  ORDER BY negative_sku_count DESC;

  -- 负库存最严重的前 10 个 SKU
  SELECT
    productno,
    productwarehouse,
    productstockin,
    productstockout,
    productzhanyong,
    (productstockin - productstockout - productzhanyong) AS available
  FROM flying_api.erp_stock
  WHERE (productstockin - productstockout - productzhanyong) < 0
    AND productwarehouse <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  ORDER BY available ASC
  LIMIT 10;
  ```

### 2. 虚拟库存悬挂（非测试仓）
erp_stock.virtualinventory ≠ 0 的 SKU 数量，正常情况下每日23点应被 CountVirtualInventoryJob 清零。
连续多天存在说明清理任务异常。
- 异常定义：非测试仓 virtualinventory ≠ 0 的 SKU > 50 触发 WARN；> 200 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    productwarehouse,
    COUNT(*) AS virtual_count,
    SUM(ABS(virtualinventory)) AS total_abs_virtual,
    SUM(CASE WHEN virtualinventory > 0 THEN 1 ELSE 0 END) AS positive_virtual,
    SUM(CASE WHEN virtualinventory < 0 THEN 1 ELSE 0 END) AS negative_virtual
  FROM flying_api.erp_stock
  WHERE virtualinventory != 0
    AND productwarehouse <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY productwarehouse
  ORDER BY virtual_count DESC;
  ```

### 3. 库位级负库存（erp_stocklocationmanage）
库位级别的 productstockin - productstockout - productzhanyong < 0，
说明某库位出库/占用记录异常，即使汇总库存为正也可能导致拣货失败。
- 异常定义：非测试仓负库位 > 0 触发 WARN；> 20 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    productwarehouse,
    COUNT(*) AS negative_location_count,
    SUM(productstockin - productstockout - productzhanyong) AS total_available
  FROM flying_api.erp_stocklocationmanage
  WHERE (productstockin - productstockout - productzhanyong) < 0
    AND productwarehouse <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY productwarehouse
  ORDER BY negative_location_count DESC;
  ```

### 4. 退件托盘收货完成但未最终处理（erp_returned_pallet）
palletcodetype=0（Hold 类型），iscomplete=1（已收货），isfinish=0（未最终处理/销毁），
且入库超过 14 天的托盘。关联配件流程卡死时，AutoDestroyPalletJob 会跳过，托盘永久悬挂。
- 异常定义：> 5 个超期未处理托盘触发 WARN；> 20 触发 CRITICAL
- 查询示例：
  ```sql
  SELECT
    COUNT(*) AS stuck_pallet_count,
    MIN(completetime) AS oldest_complete_time,
    TIMESTAMPDIFF(DAY, MIN(completetime), NOW()) AS max_days_waiting
  FROM flying_api.erp_returned_pallet
  WHERE palletcodetype = 0
    AND iscomplete = 1
    AND isfinish = 0
    AND completetime < DATE_SUB(NOW(), INTERVAL 14 DAY)
    AND warehouseid <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```
  注意：若 completetime 为 NULL，改用 creationtime 作为参照时间。

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 具体数值及按仓库的分布
- 负库存检查列出前 10 个最严重的 SKU（货号/仓库/可用数量）
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："负库存"、"虚拟库存悬挂"、"库位负库存"、"退件托盘积压"
  - brief: 30字以内，如无异常填"库存数据正常"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
