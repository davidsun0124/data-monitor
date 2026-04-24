---
db_host: EOS_DB_HOST
schedule: 0 9 * * 1-5
max_turns: 15
budget: 0.5
---

# 任务：转运账单与转运单一致性巡检


## 数据库约束
- 权限要求：仅限只读，禁止执行 DML/DDL 操作。
- 安全准则：严禁修改任何数据或表结构。

## 连接信息
- 数据库：mysql -h ${DB_HOST} -P ${DB_PORT} -u ${DB_USER} -p${DB_PASS} ${DB_NAME}
- 只读权限，禁止执行任何 INSERT / UPDATE / DELETE / DROP 语句
- 数据库引擎：StarRocks，业务库：flying_api

## 背景说明
本巡检对应 eos-monitor-py/finance/trans_shippment_check.py 中的 daily_bill_and_data_check_b2b 逻辑。
核心逻辑：检查每日 B2B 转运账单（FIM_TransShipPrice）与已完成/拣货后取消的转运单
（erp_transshipment）之间的数量和对应关系，发现账单多计或漏计。
当前 eos-monitor-py 中 D2C 卡车部分（FIM_TruckPrice vs Platform=5）已注释掉，
本任务仅覆盖 B2B（Platform NOT IN (4,5)）。

## 监控规则

### 1. 有账单但无对应转运单（账单多出）
FIM_TransShipPrice 中存在，但在 erp_transshipment 中找不到匹配记录（既非完成 800 也非拣货后取消）。
- 异常定义：任意多出记录即触发 CRITICAL
- 查询示例：
  ```sql
  -- 近 7 天有账单但无有效转运单的记录
  SELECT fp.TransId, fp.CreationTime AS bill_time
  FROM flying_api.FIM_TransShipPrice fp
  WHERE fp.CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND NOT EXISTS (
      SELECT 1 FROM flying_api.erp_transshipment t
      WHERE t.TransID = fp.TransId
        AND (
          t.State = 800  -- 已完成
          OR (t.State = 600 AND t.BeforeCancelState > 202)  -- 拣货后取消
        )
    )
  LIMIT 20;
  ```
  注意：若 erp_transshipment 无 BeforeCancelState 字段，则只检查 State=800 的情况，并在输出中说明。

### 2. 有完成转运单但无对应账单（账单漏计）
erp_transshipment State=800 或拣货后取消（State=600 AND BeforeCancelState>202），
但在 FIM_TransShipPrice 中找不到对应记录。
- 异常定义：漏计比例 > 5% 触发 WARN；> 15% 触发 CRITICAL
- 查询示例：
  ```sql
  -- 近 7 天应有账单的转运单总数
  SELECT COUNT(*) AS should_bill_count
  FROM flying_api.erp_transshipment t
  WHERE (t.State = 800 OR (t.State = 600 AND t.BeforeCancelState > 202))
    AND t.Platform NOT IN (4, 5)
    AND t.UpdatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND t.FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 其中有账单记录的数量
  SELECT COUNT(DISTINCT t.TransID) AS billed_count
  FROM flying_api.erp_transshipment t
  INNER JOIN flying_api.FIM_TransShipPrice fp ON fp.TransId = t.TransID
  WHERE (t.State = 800 OR (t.State = 600 AND t.BeforeCancelState > 202))
    AND t.Platform NOT IN (4, 5)
    AND t.UpdatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND t.FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';

  -- 漏计的前 10 条
  SELECT t.TransID, t.PO, t.CustomerName, t.State, t.UpdatedDate
  FROM flying_api.erp_transshipment t
  WHERE (t.State = 800 OR (t.State = 600 AND t.BeforeCancelState > 202))
    AND t.Platform NOT IN (4, 5)
    AND t.UpdatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND t.FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791'
    AND NOT EXISTS (
      SELECT 1 FROM flying_api.FIM_TransShipPrice fp WHERE fp.TransId = t.TransID
    )
  ORDER BY t.UpdatedDate DESC
  LIMIT 10;
  ```

### 3. D2C 卡车账单一致性（FIM_TruckPrice）
检查 FIM_TruckPrice 中的卡车账单与 Platform=5 的转运单一致性。
- 查询示例：
  ```sql
  -- 近 7 天卡车账单总数
  SELECT COUNT(*) AS truck_bill_count
  FROM flying_api.FIM_TruckPrice
  WHERE CreationTime >= DATE_SUB(NOW(), INTERVAL 7 DAY);

  -- 近 7 天 Platform=5 完成的转运单总数
  SELECT COUNT(*) AS truck_trans_count
  FROM flying_api.erp_transshipment
  WHERE Platform = 5
    AND (State = 800 OR (State = 600 AND BeforeCancelState > 203))
    AND UpdatedDate >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    AND FromWarehouseId <> 'ee76872c-a139-4b9c-9bf1-16c29f4f7791';
  ```
  若 FIM_TruckPrice 表不存在，输出 [SKIP]。

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] 或 [SKIP] + 具体数值及差异条数
- 最后输出汇总：PASS / FAIL
- **最后一行**必须输出如下格式的结构化摘要（不要用 markdown 代码块包裹，直接输出这一行）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":[],"brief":"一句话说明","top5":[]}
  - status: PASS / FAIL
  - level: OK / WARN / CRITICAL
  - anomaly_types: 可包含："账单多计"、"账单漏计"、"卡车账单异常"
  - brief: 30字以内，如无异常填"转运账单与转运单一致"
  - top5: 有异常时列出最多5条异常记录的简短描述字符串（如\"[客户/订单号]: 描述\"），无异常时为空数组
