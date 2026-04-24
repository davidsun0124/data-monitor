---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：FF 运费账单合规性巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api

## 背景说明
本巡检对应 eos-monitor-py/finance/shippment_check.py 中的运费差异检查逻辑。
核心账单表：flying_api.FIM_FixedPrice（每日结算账单，每条对应一个 TrackingNumber）
检查日期：通常为昨日（CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)）
当前生产启用：daily_bill_ahso_fee_check（超长/超重/超大尺寸附加费）

## 监控规则

### 1. 账单总金额内部一致性
验证 FIM_FixedPrice 中各分项费用之和 = BeforeDiscountFee，折扣后 = Fee。
- 异常定义：任何一条记录总额不一致即告警
- 查询示例：
  ```sql
  SELECT
    TrackingNumber,
    ROUND(Fee0+Fee2+Fee3+Fee4+Fee5+Fee6+Fee7+Fee9+Fee10+Fee11+Fee12+
          AHS_Size_Fee+AHS_Weight_Fee+Peak_AHS_Fee+Peak_OS_Fee+
          Peak_Residential_Fee+Peak_Ship_Fee+DASRemoteFee+
          DeliveryAndReturnsFee+PeakUnauthorizedFee+PeakHDGround+
          PeakSmartPost+DemandSurcharge+DemandPrePackageFee+
          NonStandardFee+NonStandardPackaging_Fee+
          ShipmentCorrectionAuditFee+OverVolumeSurcharge, 2) AS calc_total,
    BeforeDiscountFee,
    Discount,
    ROUND(Fee, 2) AS final_fee
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
  HAVING ABS(calc_total - BeforeDiscountFee) > 0.01
      OR ABS(ROUND(calc_total * Discount, 2) - final_fee) > 0.01
  LIMIT 20;
  ```

### 2. AHS 超尺寸/超重/超大附加费异常统计
统计昨日账单中有 AHS 附加费的订单数量及金额分布，识别异常高费记录。
- 异常定义：单条 AHS_Size_Fee 或 AHS_Weight_Fee > 50 即告警（疑似错误计费）
- 查询示例：
  ```sql
  -- 汇总统计
  SELECT
    COUNT(*)                              AS total_ahs_orders,
    SUM(AHS_Size_Fee)                     AS total_size_fee,
    SUM(AHS_Weight_Fee)                   AS total_weight_fee,
    MAX(AHS_Size_Fee)                     AS max_size_fee,
    MAX(AHS_Weight_Fee)                   AS max_weight_fee
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND IsDeleted = 0
    AND (AHS_Size_Fee <> 0 OR AHS_Weight_Fee <> 0 OR Fee8 <> 0);

  -- 费用异常高的 Top 5 记录（超过 50）
  SELECT TrackingNumber, CustomerId, AHS_Size_Fee, AHS_Weight_Fee, Fee8 AS oversize_fee
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND IsDeleted = 0
    AND (AHS_Size_Fee > 50 OR AHS_Weight_Fee > 50)
  ORDER BY (AHS_Size_Fee + AHS_Weight_Fee) DESC
  LIMIT 5;
  ```

### 3. Unauthorized 费异常（Fee9 <> 0）
Fee9（Unauthorized 附加费）不应出现，凡 <> 0 即为异常需核查。
- 查询示例：
  ```sql
  SELECT TrackingNumber, CustomerId, CheckDate, Fee9 AS unauthorized_fee
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND Fee9 <> 0;
  ```

### 4. NonStandardPackaging 费异常（NonStandardPackaging_Fee <> 0）
非标准包装费不应出现，凡 <> 0 即为异常需核查。
- 查询示例：
  ```sql
  SELECT TrackingNumber, CustomerId, CheckDate, NonStandardPackaging_Fee
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND NonStandardPackaging_Fee <> 0;
  ```

### 5. 账单条数 vs 打单条数一致性
FIM_FixedPrice 的条数应与 PAT_Order + ERP_Parts + ERP_Returned_New 的出库单总数一致。
- 查询示例：
  ```sql
  -- 账单条数
  SELECT COUNT(*) AS bill_count
  FROM flying_api.FIM_FixedPrice
  WHERE CheckDate = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND IsDeleted = 0;

  -- 打单条数（一件代发 + 配件 + 退件）
  SELECT COUNT(*) AS label_count
  FROM flying_api.pat_order
  WHERE DATE(ScanTime) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    AND OwnStatus = 3
    AND IsDeleted = 0;
  ```

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 异常条数
- 如有异常，输出前 10 条 TrackingNumber 及具体差异金额
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS（无问题）/ FAIL（有问题）
  - level: OK / WARN / CRITICAL（取所有检查项中最高级别）
  - anomaly_types: 数组，可包含以下值（有哪些填哪些）：
    "账单不一致"、"附加费异常"、"未授权费用"、"账单条数差异"、"数据异常"
  - brief: 30字以内的一句话摘要，如无异常填"全部检查通过"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
