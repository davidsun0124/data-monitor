---
schedule: 30 9,11,17 * * 1-5
budget: 1.5
max_turns: 30
db_host: EOS_DB_HOST
---

# 任务：一件代发订单数据质量巡检

## 数据库约束
- 权限要求：仅限只读。
- 严禁行为：禁止执行任何写操作 (INSERT/UPDATE/DELETE/DROP等)。

## 背景说明
本巡检对应 eos-monitor-py/utils/starrocks_util.py 中的 database_inspector() 函数逻辑。
检查窗口：面单异常取近 3 天，重复单号/空字段等取近 7 天。
排除仓库：WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'（测试仓）

## 监控规则

### 1. 面单文件格式异常
OwnStatus=3（已打单）但 FilePath 不是 .png 格式的订单，说明面单文件生成有问题。
- 异常定义：有任意一条即告警
- 查询示例：
  ```sql
  SELECT TrackingNumber, Platform, WarehouseName, ShortName, Store, FilePath
  FROM flying_api.pat_order
  WHERE OwnStatus = 3
    AND SendType <> 2
    AND CreationTime >= DATE_SUB(NOW(), INTERVAL 3 DAY)
    AND (FilePath NOT LIKE '%.png' OR FilePath IS NULL OR FilePath LIKE '%/.png');
  ```

### 2. RecordNumber 重复一件代发订单
同一客户下存在相同 RecordNumber 的多条订单，可能导致财务对账错误。
- 异常定义：有任意重复即告警
- 查询示例：
  ```sql
  -- Step 1: 找出重复的 RecordNumber
  SELECT Recordnumber, COUNT(1) AS num
  FROM flying_api.pat_order
  WHERE CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND OwnStatus NOT IN (98, 99, 100, 95)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY Recordnumber, CustomerId
  HAVING COUNT(1) > 1;

  -- Step 2: 查重复单的明细（将 Step1 结果的 RecordNumber 填入）
  SELECT Recordnumber, Platform, WarehouseName, ShortName, Store
  FROM flying_api.pat_order
  WHERE Recordnumber IN (/* Step1 结果 */)
  ORDER BY Recordnumber ASC;
  ```

### 3. TrackingNumber 重复一件代发订单
同一追踪号出现在多个订单，承运商层面会产生冲突。
- 异常定义：有任意重复即告警
- 查询示例：
  ```sql
  SELECT TrackingNumber, COUNT(1) AS num
  FROM flying_api.pat_order
  WHERE CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND OwnStatus NOT IN (1, 98, 99, 100, 95)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY TrackingNumber
  HAVING COUNT(1) > 1;
  ```

### 4. TransID/BOL 重复转运单
转运单 TransID 重复（非取消状态 State <> 600）。
- 异常定义：有任意重复即告警
- 查询示例：
  ```sql
  SELECT TransID, COUNT(1) AS num
  FROM flying_api.erp_transshipment
  WHERE CreatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND State <> 600
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  GROUP BY TransID
  HAVING COUNT(1) > 1;
  ```

### 5. PO 重复转运单
转运单 PO 重复（排除已取消 600 和已完成 800 状态）。
- 异常定义：有任意重复即告警
- 查询示例：
  ```sql
  SELECT t.PO, t.TransID, t.CustomerName, w.WarehouseName
  FROM flying_api.erp_transshipment t
  LEFT JOIN flying_api.erp_warehouse w ON t.FromWarehouseId = w.ID
  WHERE t.CreatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND t.State NOT IN (600, 800)
    AND t.IsTms = 0
    AND t.FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND t.PO IN (
      SELECT PO FROM flying_api.erp_transshipment
      WHERE CreatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        AND State NOT IN (600, 800)
      GROUP BY PO HAVING COUNT(1) > 1
    )
  ORDER BY t.PO ASC;
  ```

### 6. 空字段检查（追踪号/仓库/物流商/物流服务/货号）
已处理中的订单（OwnStatus 不在 1/98/99/100/95，Status != 0）出现关键字段为空。
- 异常定义：任意字段为空即告警
- 查询示例：
  ```sql
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store,
         TrackingNumber, ServiceCode, ServiceID, ProductDetail
  FROM flying_api.pat_order
  WHERE CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND OwnStatus NOT IN (1, 98, 99, 100, 95)
    AND Status != 0
    AND (
      TrackingNumber IS NULL OR TrackingNumber = '' OR
      WarehouseName IS NULL OR WarehouseName = '' OR
      ServiceCode IS NULL OR ServiceCode = '' OR
      ServiceID IS NULL OR ServiceID = '' OR
      ProductDetail IS NULL OR ProductDetail = ''
    );
  ```

### 7. 订单数量 < 1
已处理中的订单出现 TotalQuantity < 1。
- 查询示例：
  ```sql
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, TotalQuantity
  FROM flying_api.pat_order
  WHERE CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND OwnStatus NOT IN (1, 98, 99, 100, 95)
    AND TotalQuantity < 1;
  ```

### 8. 带面单导入/推单的待处理订单
OwnStatus=1（待处理）且 Status=10，说明已有面单但订单仍停留在待处理状态。
- 查询示例：
  ```sql
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, Status, OwnStatus
  FROM flying_api.pat_order
  WHERE CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND OwnStatus = 1
    AND Status = 10;
  ```

### 9. 箱号 ID=0 的已打单订单
打单完成（OwnStatus=3）但 BoxId=0 且为重新包装（RuleType=4）的异常订单，说明包装规则执行异常。
- 异常定义：有任意一条即告警
- 查询示例：
  ```sql
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, OwnStatus, BoxId, RuleType
  FROM flying_api.pat_order
  WHERE OwnStatus = 3
    AND BoxId = 0
    AND RuleType = 4
    AND CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  LIMIT 10;
  ```

### 10. 异常客户待处理订单
特定异常客户（CustomerId 白名单）存在 OwnStatus=1 的待处理订单，需人工介入。
- 异常定义：有任意一条即告警
- 客户 ID 白名单（以下客户为已知异常客户）：
  12ab651f-14af-48d3-b25a-15e9778e9d0e、397ec3c5-dc7d-4928-8d2a-825ba3748cfd、
  14f4c28b-4413-42de-90fd-6ebb647007d0、35ee082c-a1c4-4bde-aac7-65de0460a801、
  39fad3f2-59e2-4a33-bc46-e593e918adf6、2ed826d3-6fb8-4650-ac11-da58bed3622a、
  9325a75d-1324-40fc-a743-981076df07f6、c8dd6cb7-00c2-47b5-9c61-2f0b76ab20d0、
  73766d4c-b21a-4d39-b46f-7d3b5bcffe21、e80f2c3c-d104-41ba-847c-1a4ca0939c9a、
  e803db87-21f3-4a1d-b7dc-65b5f28e4c7e
- 查询示例：
  ```sql
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, OwnStatus
  FROM flying_api.pat_order
  WHERE CustomerId IN (
      '12ab651f-14af-48d3-b25a-15e9778e9d0e','397ec3c5-dc7d-4928-8d2a-825ba3748cfd',
      '14f4c28b-4413-42de-90fd-6ebb647007d0','35ee082c-a1c4-4bde-aac7-65de0460a801',
      '39fad3f2-59e2-4a33-bc46-e593e918adf6','2ed826d3-6fb8-4650-ac11-da58bed3622a',
      '9325a75d-1324-40fc-a743-981076df07f6','c8dd6cb7-00c2-47b5-9c61-2f0b76ab20d0',
      '73766d4c-b21a-4d39-b46f-7d3b5bcffe21','e80f2c3c-d104-41ba-847c-1a4ca0939c9a',
      'e803db87-21f3-4a1d-b7dc-65b5f28e4c7e'
  )
  AND OwnStatus = 1
  AND CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
  AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```

### 11. 物流服务不匹配检查
ServiceCode 与 ServiceID 的组合不在白名单内（FEDEX/UPS/USPS/AMAZON/ONTRAC）。
- 查询示例（以 FEDEX 为例）：
  ```sql
  SELECT RecordNumber, Platform, WarehouseName, ShortName, Store, ServiceCode, ServiceID
  FROM flying_api.pat_order
  WHERE UPPER(ServiceCode) = 'FEDEX'
    AND UPPER(ServiceID) NOT IN (
      'FEDEX_GROUND','SMART_POST','GROUND_HOME_DELIVERY','FEDEX_2_DAY',
      'GROUND_ECONOMY','FEDEX_EXPRESS_SAVER','PRIORITY_OVERNIGHT',
      'STANDARD_OVERNIGHT','FIRST_OVERNIGHT','FEDEX_2_DAY_AM',
      'FEDEX_REGIONAL_ECONOMY'
      -- 完整列表见 starrocks_util.py database_inspector_pat_order_service_check()
    )
    AND CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND OwnStatus NOT IN (1, 4, 98, 99, 100, 95);
  ```
  对 UPS / USPS / AMAZON / ONTRAC 执行同样逻辑的检查。

## 输出要求
- 每条检查项输出：[OK] 或 [CRITICAL] + 异常条数及代表性记录（最多 5 条）
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "重复单号"、"空字段"、"面单异常"、"服务不匹配"、"数量异常"、"待处理积压"、"箱号异常"、"异常客户订单"、"数据异常"
  - brief: 30字以内的一句话摘要，如无异常填"全部检查通过"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
