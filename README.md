# Data Monitor (Zero-Config)

极简、自包含的零配置异步数据质量监控框架。

## 目录
- [项目结构](#项目结构)
- [极简上手指南](#极简上手指南)
- [核心架构：零配置 (Zero-Config)](#核心架构零配置-zero-config)
- [配置参考](#配置参考)
- [执行器说明](#执行器说明)
- [新增执行器规范](#新增执行器规范)
- [关键数据库表参考](#关键数据库表参考)
- [日志清理服务 (cleanup_logs.py)](#日志清理服务-cleanup_logspy)
- [常见问题](#常见问题)

## 项目结构
```text
.
├── claude/              # Claude 执行器目录
│   ├── scheduler.py     # 异步任务调度中心
│   └── logs/            # 巡检日志目录
├── qoderwork/           # QoderWork 执行器目录
├── tasks/               # 监控任务定义 (Markdown)
│   ├── _template.md     # 任务模板
│   └── *.md             # 具体巡检任务
├── .env                 # 环境变量 (DB连接、Webhook)
├── config.yaml          # 全局默认配置
├── cleanup_logs.py      # 日志自动清理服务
├── scratch/             # 临时调试代码目录
└── README.md            # 项目文档
```

## 极简上手指南

1.  **创建任务**：拷贝 `tasks/_template.md` 到 `tasks/your-task.md`。
2.  **配置任务**：在文件顶部 YAML 区设置 `schedule` 和 `db_host`（环境变量名）, 部分或全部不填会使用config.yaml中的默认值。
3.  **编写逻辑**：在 Markdown 正文中用自然语言描述巡检规则。
4.  **启动调度**：执行 `python claude/scheduler.py`。

---

## 核心架构：零配置 (Zero-Config)

本项目采用**去中心化**设计，所有任务相关的元数据均存储在任务文件自身中，无需修改全局配置文件。

### 1. 任务文件 (tasks/*.md)
每个任务是一个独立的 Markdown 文件，包含两部分：
*   **YAML Frontmatter**：定义调度周期、资源限制、数据库连接。
*   **巡检指令**：AI 执行的具体 SQL 逻辑和判断标准。

### 2. 全局配置 (config.yaml)
仅存放**全局默认值**（如默认模型、默认巡检预算、默认告警 Webhook 变量名）。任务文件中未指定的项将自动回退到全局默认值。

### 3. 环境变量 (.env)
存放敏感信息，如数据库密码和 Webhook 链接。
> **变量注入逻辑**：任务指定 `db_host: MY_DB_HOST`，调度器会自动从环境中匹配 `MY_DB_PORT`, `MY_DB_USER` 等变量注入给 AI。

---

## 配置参考

每个任务文件（`tasks/*.md`）的顶部都有一段由 `---` 包裹的 YAML 配置区。**配置的生效逻辑非常简单：**

1. **不填即使用默认**：如果你直接删除某一项（或者干脆不写整个配置区），系统会自动兜底使用 `config.yaml` 中的全局默认配置。
2. **填写即局部覆盖**：如果你填入了具体的数值或字符串，它会仅在这个任务里覆盖全局默认值。
3. **填 `false` 即彻底关闭**：如果你想完全关闭某个功能或不设限制，请显式填写 `false`（或 `none`、`""`），调度器会自动处理。

### 覆盖与关闭功能示例

```yaml
---
# 【覆盖默认值示例】
schedule: "00 9 * * 1-5" # 将执行时间专门覆盖为 9:00
budget: 1.0               # 将当前任务预算上限放宽到 1.0 USD

# 【关闭特定功能示例】
schedule: manual          # 关闭定时触发（仅允许手动执行）
timeout: false            # 关闭超时限制（无限等待）
budget: false             # 关闭费用上限（变相不限制预算）
max_turns: false          # 关闭思考轮次限制
db_host: false            # 关闭数据库功能（不向 AI 注入任何数据库连接信息）
alert_webhook_env: false  # 关闭企微告警（执行完后不发任何消息）
---
```


---

## 执行器说明

### 执行器 A：Claude Code (`claude/`)
基于 Claude CLI 执行。适合处理逻辑复杂、需要多步 SQL 关联分析的任务。
*   **启动方式**：`python claude/scheduler.py`
*   **手动执行**：`python -c "import scheduler; scheduler.run_task('任务文件名')"`

### 执行器 B：QoderWork (`qoderwork/`)
基于 QoderWork 桌面端执行。适合高性能、纯 SQL 巡检任务。

---

## 新增执行器规范

若需集成新的执行器（如基于其他 LLM 或脚本引擎），请遵循以下共用协议：

### 1. 配置共用
- **环境变量**：统一从根目录 `.env` 读取 DB 连接信息及 `ALERT_WEBHOOK`。
- **全局配置**：从 `config.yaml` 读取 `global_defaults`。
- **任务解析**：必须解析 `tasks/*.md` 顶部的 YAML Frontmatter，其优先级高于 `config.yaml`。

### 2. 标准输出协议 (SUMMARY_JSON)
执行器输出的最后一行必须包含以下格式，以便外壳脚本解析告警逻辑：
```json
SUMMARY_JSON:{"status": "FAIL", "level": "CRITICAL", "brief": "摘要", "anomaly_types": [], "top5": []}
```

### 3. 日志清理
执行器必须在自己的 `logs/` 目录下存储日志，并确保脚本能被 `cleanup_logs.py` 识别清理。

---

## 关键数据库表参考

### StarRocks (flying_api / flying_api_pro)

| 模块 | 表名 | 说明 |
|------|------|------|
| OMS 订单 | `pat_order` | 订单主表 |
| 订单明细 | `pat_item` | 订单 SKU 明细 |
| FF 拣货 | `erp_pickinglist` | 拣货单 |
| 结算运费 | `fim_orderdealprice` | 我方对客户的结算运费 |
| 账单明细 | `FIM_FixedPrice` | 每日运费账单 |
| 转运单 | `erp_transshipment` | 大货/海运转运单 |

### PostgreSQL (shipmasterdb)

| 模块 | 表名 | 说明 |
|------|------|------|
| 月度账单汇总 | `fms_monthly_summary` | 按客户/月汇总 |
| 订单运费明细 | `fms_order_freight` | 每票运费明细 |
| 客户账单 | `fms_customer_bill` | 月度对账单，`is_clear` 标记是否结清 |

---

## 日志清理服务 (cleanup_logs.py)

为了防止日志文件无限增长占用磁盘空间，项目根目录下提供了统一的日志清理脚本。

### 功能说明
- **全量扫描**：自动遍历所有执行器目录下的 `logs/` 文件夹（如 `claude/logs/`, `qoderwork/logs/`）。
- **过期清理**：删除修改时间超过 **90 天** 的 `.log` 文件。
- **白名单**：自动跳过 `scheduler.log` 等核心调度日志。

### 启动服务
```bash
python cleanup_logs.py
```
*启动后会立即执行一次清理，随后进入后台定时等待状态。*

---

## 常见问题

**Q: 企微没收到告警？**
A: 
1. 检查 `.env` 中的 `ALERT_WEBHOOK` 是否正确。
2. 只有 `status` 为 `FAIL` 或执行报错时才会发送告警。

**Q: 如何修改某个任务的执行时间？**
A: **直接修改该任务 `.md` 文件顶部的 `schedule` 字段**。修改后，Claude 执行器需要重启 `scheduler.py` 才能重新加载调度计划。

**Q: 任务日志提示 `error_max_turns` 或 `error_max_budget_usd`？**
A: 说明 AI 思考次数或费用超过了限制。请在对应任务的 `.md` 头部调大 `max_turns` 或 `budget`。

**Q: 数据库连不上？**
A: 
1. 检查任务头部的 `db_host` 变量名是否与 `.env` 中的一致。
2. 检查网络是否通畅（StarRocks 默认端口 9030）。

**Q: Claude 调度器停止了怎么重启？**
```bash
cd E:/Jasper/My_Projact/eos-sc-monorepo/sc-project/data-monitor/claude
python scheduler.py
```
