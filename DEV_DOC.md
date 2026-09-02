# SSM 书面询价监控 v2 开发文档

> 版本: v2.0 | 日期: 2026-09-02
> 目标: 借鉴 synbio-daily-agent 的成熟机制, 升级 ssm_monitor.py 的去重/状态管理/可审计性

## 一、背景与现状

- 现有 `ssm_monitor.py` v1 已完成: ASP.NET 会话抓取(已验证)、类别筛选+翻页、`/C/26$` 编号过滤、SMTP 邮件、seen_ids.json 去重、dry_run 模式。
- 参考仓库 synbio-daily-agent 中确认值得借鉴的机制(已评审): 持久化注册表、发送成功才标记 sent、fail-closed、每日落盘、发送门禁、追踪行。

## 二、开发内容(五项)

| # | 内容 | 来源参考 |
|---|------|---------|
| 1 | 去重注册表升级: `seen_ids.json`(纯列表) → `registry.json`(每条带 first_seen_date/last_seen_date/seen_count/status/last_reason) | synbio `scripts/search_history.py` 的 entry schema |
| 2 | 状态机: `pending` → `sent`; 发送失败保持 `pending` 并记 `last_reason`, 下次运行自动重发 | synbio `classify_and_record_result` 的 sent/terminal 语义 |
| 3 | 每日原始数据落盘: `data/raw_YYYY-MM-DD.json` 保存当天抓到的全部行(两类别), 供审计回溯 | synbio `data/raw_*.json` 约定 |
| 4 | 发送前自检门禁(fail-closed): SMTP 配置仍是占位符 → 阻断; 抓取总行数为 0(页面结构可能变更) → 视为抓取失败, 不更新任何状态 | synbio pre_check / fail-closed 流水线 |
| 5 | 邮件末尾追踪行: `本次抓取 X 条 → 符合条件 Y 条 → 新增 Z 条` | synbio 报告头部追踪标记 |

## 三、具体方法

### 3.1 复用策略(基本原则: 尽量少自己写代码)

- **注册表**: 不复用 synbio 文件本体(其逻辑绑定其 artifacts 目录结构), 仅**借用其 entry schema 与状态语义**, 在 `ssm_monitor.py` 内实现一个 ~40 行的 Registry(标准库 json), 不新增文件、不引入第三方库。
- **抓取**: 完全保留 v1 已验证的 `SsmClient`(ASP.NET 全字段回传是关键, 不重写)。
- **邮件**: 保留 v1 的 `smtplib` 实现; 仅修改 `format_email` 增加追踪行。
- **配置**: `config.ini` 向后兼容, 仅 `[general]` 新增 `registry_file = registry.json`; `seen_file` 字段保留用于一次性迁移。
- **运行环境**: 纯 Python 标准库, 继续用 managed Python 3.13, 定时方式不变(WorkBuddy 自动化 + install_task.bat)。

### 3.2 注册表 entry 结构

```json
{
  "20408/C/26": {
    "title": "各科專用醫療消耗品",
    "category": "醫療消耗品",
    "pub_date": "2026.08.18",
    "deadline": "2026.08.27 17:45",
    "first_seen_date": "2026-09-02",
    "last_seen_date": "2026-09-02",
    "seen_count": 1,
    "status": "sent | pending",
    "last_reason": ""
  }
}
```

### 3.3 状态流转

```
抓取到符合条件条目
  ├─ 不在注册表 → 新增 status=pending, 进入本次待发送清单
  ├─ status=pending → 仍进入待发送清单(上次发送失败的重试), seen_count+1
  └─ status=sent → 跳过, 仅更新 last_seen_date/seen_count
发送成功 → 本次所有 pending 条目标记 sent
发送失败 → 全部保持 pending, last_reason=错误摘要, 退出码 3, 注册表照常落盘
```

### 3.4 迁移

首次运行若发现旧 `seen_ids.json` 存在而 `registry.json` 不存在:
将其中的编号批量导入, status=sent, first_seen_date=当天, 其余字段留空; 导入后旧文件改名为 `seen_ids.json.bak`。

### 3.5 门禁规则(fail-closed)

1. 抓取阶段异常或两类别总行数 == 0 → 退出码 2, 不写注册表、不发邮件。
2. dry_run=false 且 SMTP host/user 含占位符(`your_email` / `在此填`) → 退出码 4, 日志明确提示, 不发邮件、**不把 pending 标记为 sent**。
3. 邮件发送异常 → 退出码 3, pending 状态保留(下次重发)。

### 3.6 追踪行

邮件正文末尾追加:
`--- 追踪: 抓取 81 条(藥物 25 / 醫療消耗品 56) → 符合条件 81 条 → 本次提醒 3 条(含重试 1 条) ---`

## 四、边界约束(明确不做)

1. **不改抓取逻辑**: 会话流程、字段提取正则、翻页逻辑保持 v1 原样。
2. **不加第三方依赖**: 不用 requests/bs4, 纯标准库。
3. **不引入 synbio 的其他机制**: 标题相似度去重、URL 归一化、价值评分、LLM 防幻觉、HTML 报告渲染——本场景均不需要。
4. **不做截标临近二次提醒**(已评审为可选增强, 本期不做, 避免范围蔓延)。
5. **dry_run 语义不变**: true 时打印邮件但不发送; 注意 dry_run 也会把条目写入注册表为 sent(用于建立基线), 文档中明示此行为。
6. 单文件脚本, 除 `data/` 目录外不新增模块文件。

## 五、验收标准

1. `dry_run=true` 首次运行: 迁移 seen_ids.json → registry.json, 81 条全部 status=sent。
2. 第二次运行: 新增 0 条, 不发邮件, 退出码 0。
3. 模拟发送失败(dry_run=false + 错误密码): 注册表中新条目标记 pending + last_reason, 退出码 3; 修复后再次运行该条目重发并成功转 sent。
4. 邮件正文末尾含追踪行。
5. `data/raw_YYYY-MM-DD.json` 生成且含两类别的全部抓取行。
6. 人为把页面解析结果清空(模拟) → 触发门禁, 退出码 2, 注册表不变。
