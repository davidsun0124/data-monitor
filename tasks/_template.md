---
# 删除后会使用config.yaml中的默认值; 可以部分配置, 不影响其他配置项
schedule: "0 9 * * 1-5"          # 定时配置 (分 时 日 月 周)
budget: 0.5                      # 费用上限 (USD)
max_turns: 20                    # 最大轮次
timeout: 600                     # 超时时间 (秒)
db_host: EOS_DB_HOST              # 数据库 Host 变量名 (用于自动注入连接信息)
alert_webhook_env: ALERT_WEBHOOK # 告警机器人变量名 (在 .env 中配置)
---

# 任务：{任务名称}

## 数据库约束
- 权限要求：**仅限只读**。
- 严禁行为：禁止执行任何 `INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER` 等写操作或结构变更语句。
- 安全原则：如需查询敏感数据，请确保仅用于统计分析。

## 监控规则
{描述你要查什么数据、怎么判断正常/异常}

## 输出要求
- 每条检查项输出：[OK] 或 [WARN] 或 [CRITICAL] + 说明
- 如有异常，输出具体数据以便排查
- **极其重要**：不管执行结果如何，最后一行必须严格输出如下格式的结构化 JSON，供调度器解析并发送告警（请直接输出文本，不要包裹在 Markdown 代码块中）：
  SUMMARY_JSON:{"status":"PASS或FAIL","level":"OK或WARN或CRITICAL","anomaly_types":["异常类型1"],"brief":"一句话说明结论","top5":["异常记录简述"]}
