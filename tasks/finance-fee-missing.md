---
schedule: 0 11,17 * * 1-5
budget: 1.5
max_turns: 35
db_host: EOS_DB_HOST
---

# 任务：财务费用漏算巡检

## 数据库约束
- 权限要求：仅限只读。
- 严禁行为：禁止执行任何写操作。

## 背景说明
系统后台通过事件驱动（CAP 消息总线）在订单打单/扫描成功时异步计算运费并写入
FIM_FixedPrice；每日任务计算仓储费写入 FIM_FeeAmount。
两个任务均配置 AutomaticRetry=0（不重试），任何失败都导致费用永久漏算。
本巡检从结果侧验证费用记录是否完整。
排除测试仓：WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'

## 监控规则

### 1. 有出库但无运费账单的订单（运费漏算）
昨日打单完成（OwnStatus=3/4，有 ScanTime）的订单，在 FIM_FixedPrice 中找不到对应记录。
说明 OrderBillingSuccessHandler 事件处理失败（邮编找不到、报价规则缺失或消息消费失败）。
- 异常定义：漏算订单数 > 0 触发 CRITICAL
- 查询示例：
  ```sql
  -- 昨日有扫描记录但 FIM_FixedPrice 无记录的订单
  SELECT p.TrackingNumber, p.RecordNumber, p.WarehouseName, p.ShortName,
         p.ServiceCode, p.ScanTime
  FROM flying_api.pat_order p
  WHERE DATE(p.ScanTime) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND p.OwnStatus IN (3, 4)
    AND p.TrackingNumber IS NOT NULL
    AND p.TrackingNumber <> ''
    AND p.WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND NOT EXISTS (
      SELECT 1 FROM flying_api.FIM_FixedPrice f
      WHERE f.TrackingNumber = p.TrackingNumber
        AND f.IsDeleted = 0
    )
  LIMIT 20;

  -- 汇总漏算条数
  SELECT COUNT(*) AS missing_fee_count
  FROM flying_api.pat_order p
  WHERE DATE(p.ScanTime) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND p.OwnStatus IN (3, 4)
    AND p.TrackingNumber IS NOT NULL AND p.TrackingNumber <> ''
    AND p.WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND NOT EXISTS (
      SELECT 1 FROM flying_api.FIM_FixedPrice f
      WHERE f.TrackingNumber = p.TrackingNumber AND f.IsDeleted = 0
    );
  ```

### 2. 按客户检查本月运费汇总是否异常为零
本月有出库量但 FIM_FeeAmount 中 FixedFee 类型金额为 0 的客户，说明该客户报价规则缺失，
系统静默跳过计费（OrderBillingSuccessHandler 中 feeRule==null 直接 return，不报错）。
- 异常定义：存在满足条件的客户即触发 WARN
- 查询示例：
  ```sql
  -- 本月有出库但运费汇总为0的客户
  SELECT o.CustomerId, o.ShortName,
         COUNT(o.TrackingNumber) AS shipped_count,
         COALESCE(fa.Received, 0) AS fee_amount_received
  FROM (
    SELECT CustomerId, ShortName, TrackingNumber
    FROM flying_api.pat_order
    WHERE YEAR(ScanTime) = YEAR(NOW())
      AND MONTH(ScanTime) = MONTH(NOW())
      AND OwnStatus IN (3, 4)
      AND WarehouseAddress <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
  ) o
  LEFT JOIN (
    SELECT CustomerId, Received
    FROM flying_api.FIM_FeeAmount
    WHERE Year = YEAR(NOW())
      AND Month = MONTH(NOW())
      AND FeeName = 'FixedFee'
      AND IsDeleted = 0
  ) fa ON o.CustomerId = fa.CustomerId
  GROUP BY o.CustomerId, o.ShortName, fa.Received
  HAVING COUNT(o.TrackingNumber) >= 10
     AND COALESCE(fa.Received, 0) = 0
  ORDER BY COUNT(o.TrackingNumber) DESC;
  ```

### 3. 近 3 天仓储费 FeeAmount 记录完整性
每日仓储费计算任务（DailyCalcStorageFeeJob）不重试，任何失败导致当日仓储费永久漏算。
通过检查 FIM_FeeAmount 中近 3 天的 StorageFee 写入记录来验证任务是否正常运行。
注意：仓储费按月汇总，每天刷新；若某日漏跑，该月 StorageFee 的 UpdatedDate 会停在前一天。
- 异常定义：StorageFee 类型的 UpdatedDate 落后当前时间超过 48 小时触发 WARN
- 查询示例：
  ```sql
  -- 仓储费最近更新时间
  SELECT
    FeeName,
    MAX(UpdatedDate) AS last_updated,
    TIMESTAMPDIFF(HOUR, MAX(UpdatedDate), NOW()) AS hours_ago,
    COUNT(DISTINCT CustomerId) AS customer_count,
    ROUND(SUM(Received), 2) AS total_received
  FROM flying_api.FIM_FeeAmount
  WHERE FeeName = 'StorageFee'
    AND Year = YEAR(NOW())
    AND Month = MONTH(NOW())
    AND IsDeleted = 0
  GROUP BY FeeName;
  ```
  注意：若 FIM_FeeAmount 无 UpdatedDate 列，改为查询最近 3 天 FeeName='StorageFee' 的记录数变化趋势。

### 4. 转运完成费用漏算（FIM_TransShipPrice 覆盖检查）
转运单完成（State=800）后，TransshipmentFinishedHandler 异步写入 FIM_TransShipPrice。
若事件消费失败，转运费永久丢失。
- 异常定义：近 7 天完成的转运单中，FIM_TransShipPrice 漏算比例 > 5% 触发 WARN；> 20% 触发 CRITICAL
- 查询示例：
  ```sql
  -- 近 7 天完成的转运单（B2B，非 Platform=4/5）
  SELECT COUNT(*) AS finished_count
  FROM flying_api.erp_transshipment
  WHERE State = 800
    AND FinishTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND Platform NOT IN (4, 5)
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 其中有 FIM_TransShipPrice 记录的数量
  SELECT COUNT(DISTINCT t.TransID) AS billed_count
  FROM flying_api.erp_transshipment t
  INNER JOIN flying_api.FIM_TransShipPrice fp ON fp.TransId = t.TransID
  WHERE t.State = 800
    AND t.FinishTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND t.Platform NOT IN (4, 5)
    AND t.FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 漏算的前 10 条
  SELECT t.TransID, t.PO, t.CustomerName, t.FinishTime
  FROM flying_api.erp_transshipment t
  WHERE t.State = 800
    AND t.FinishTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND t.Platform NOT IN (4, 5)
    AND t.FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND NOT EXISTS (
      SELECT 1 FROM flying_api.FIM_TransShipPrice fp WHERE fp.TransId = t.TransID
    )
  LIMIT 10;
  ```
  注意：若 erp_transshipment 无 FinishTime 列，改用 UpdatedDate。
  若 FIM_TransShipPrice 或 Platform 字段不存在，输出 [SKIP] 并说明。

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 具体数值
- 检查项 1 输出：漏算条数及前 5 条 TrackingNumber/订单号
- 检查项 2 输出：有问题的客户名及其出库量
- 检查项 3 输出：StorageFee 最后更新时间
- 检查项 4 输出：完成转运单总数/已计费数/漏算率
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："运费漏算"、"报价配置缺失"、"仓储费漏算"、"转运费漏算"
  - brief: 30字以内，如无异常填"费用计算记录完整"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
