# 项目架构

本文描述当前实现，不代表已具备无人值守实盘运行能力。
安全约束见 [SECURITY.md](./SECURITY.md)，接口操作见 [broker 使用说明](../broker/README.md)。

## 1. 目标与范围

项目核心策略命名为“核心资产风险平价策略”；
[momentum.py](../momentum.py) 承担其动量选股入口职责。
独立模拟策略入口已串联候选、风险平价权重与订单执行；研究模块本身不自动下单。

项目按职责分为三个独立部分：

1. **broker：券商通信**。通过富途免网关 REST API 认证、查询账户和行情、
   操作实盘及云端模拟盘订单，不包含选股或权重优化。
2. **momentum：动量筛选**。根目录 `momentum.py` 是 CLI 和 Python API 入口，
   核心实现位于 `momentum_core/`；通过 TradingView 初筛动态选池，并调用 broker 获取
   流动性及动量所需日线，不计算风险平价。
3. **risk_parity：风险平价 / 风险预算**。根据已有价格及收益信息预测风险、计算权重，
   不负责动量选股或下单。核心策略与行情下载 / 回测入口分离。

动量可选调用 broker 获取筛选行情；筛选结果及回测权重不会自动变成订单。
只有显式运行 [run_play_money.sh](../run_play_money.sh) 才会对账并提交模拟调仓订单。
该流程的 [strategy_ledger.py](../strategy_ledger.py) 使用 SQLite 保存策略资金、持仓、
累计成交增量及审计事件；外部资产作基线隔离，差异或未知订单会阻止新增交易。
An explicit fresh-ledger `--history-start` cutoff is stored in the initialization
event and reused for history queries and reconciliation. It excludes only
terminal orders last updated before the cutoff; open orders and later updates
remain visible. Initialization requires `--reconcile-only` and retains existing
holdings as baseline, without importing old strategy accounting.
周末将上个交易日的选股/波动率结果写入 `weekly_inputs`，开盘时从缓存生成唯一
`rebalance_cycles` 目标；只在纽约 09:30–17:30（不含结束时间）窗口提交，各次运行均先对账。
[trading_schedule.py](../trading_schedule.py) 使用现有 pandas 规则排除常规美股休市日。
账本导出同目录可读交易流水，SQLite 仍是唯一记账事实源；
[us_stock_fees.py](../us_stock_fees.py) 保留完整套餐费用估算；模拟策略单独采用每个有成交订单
0.99 USD 的假定费用，规划和购买力检查采用相同模型。对账按意向 ID 自动补记费用，
首次部分成交收费一次，重启和后续成交不重复收费；无成交订单不收费。
该费用从现金、净值和总盈亏扣除，明确标注非券商实际回报；其他现金差异仍停止对账。
富途接口封装属于本项目代码，不是富途官方 Python SDK，也不需要 OpenD。

## 2. 目录与模块

| 路径 | 职责 |
| --- | --- |
| [risk_parity/](../risk_parity/) | 研究、风险预测、组合优化及回测脚本 |
| [风险平价入口](../core_asset_risk_parity.py) | 通过 Futu REST 下载行情、调用核心策略并输出最新权重（`--backtest` 时输出最后一期回测权重） |
| [风险平价核心](../risk_parity/optimization.py) | `PipelineConfig`、GARCH 预测、协方差构建、风险预算优化及滚动回测 |
| [broker/client.py](../broker/client.py) | HTTP 传输、OAuth、Token 刷新及响应解包 |
| [broker/futu_client.py](../broker/futu_client.py) | 实盘账户查询和受保护的交易操作 |
| [broker/sim_trading_client.py](../broker/sim_trading_client.py) | 模拟盘专用路径、参数转换、模拟账户及订单操作 |
| [broker/market_data.py](../broker/market_data.py) | 统一行情客户端、历史 K 线解析、节流、缓存及下载请求 |
| [broker/credentials.py](../broker/credentials.py) | Linux Secret Service / GCP Secret Manager 凭证存取、交互录入及客户端创建 |
| [momentum.py](../momentum.py) | 独立只读入口：TradingView 动态筛选与策略侧行情处理 |
| [momentum_core/](../momentum_core/) | 动量筛选实现：动态普通股选池规则、TradingView 适配器、默认 21 日波动率调整动量排名及排除信息 |
| [broker/__init__.py](../broker/__init__.py) | 对外导出的客户端、数据类型和异常 |
| [broker/pyproject.toml](../broker/pyproject.toml) | Python 包构建配置及可选密钥库依赖 |
| [docs/](./) | 项目级架构与安全说明 |

## 3. 总体结构

```text
动量链路（独立运行）
  TradingView 全美普通股初筛 + Futu 原始/前复权日线
       ↓
  momentum.py → 动态流动性/市值选池 → ouput/ 格式化 → 动量排名表格（可选 JSON）

研究链路（独立运行）
  broker.market_data → broker.futu_client → Futu REST
       ↓
  risk_parity 历史价格 → 收益率 → 风险预测 / 协方差 → 风险预算优化
       ↓
  滚动回测 → 权重、收益序列、图表与报告

账户链路
  业务脚本 / 交互 Python
       ├─ OAuthClient → 浏览器授权 → 回调校验 → TokenSet
       ├─ SecretServiceStore ↔ 当前用户的 Linux Secret Service
       │    （headless GCP 环境由 get_store 自动切换到 GCP Secret Manager）
       ├─ FutuClient → 实盘账户查询 / 受保护的订单操作
       └─ SimTradingClient → 模拟盘查询 / 模拟订单操作
                 ↓
       复用请求层：刷新 → 持久化 → HTTP → 响应解包
                 ↓
       HTTPS https://webapi.futunn.com
```

根目录 `requirements.txt` 集中列出项目依赖，包括研究脚本的风险建模和优化库。
富途核心 HTTP 客户端仅依赖 Python 标准库。

`python momentum.py` 或 `python -m momentum` 是独立入口，不连接 risk_parity，
不提交任何订单，也不查询或选择模拟交易账户。使用
`tradingview-screener` 做美国股票初筛，再以
默认 Secret Service 中的 Futu 凭证通过 Futu 拉取 ADV 原始日线及前复权动量日线；
行情请求和凭证客户端统一由 `broker/market_data.py` 提供；策略入口不直接构造
Futu HTTP 请求。CLI 行情只读客户端通过 `get_store("default")` 选择的凭证存储创建
（本地 Secret Service；headless GCP 上为 Secret Manager）。
依赖统一安装自根目录的
`requirements.txt`。CLI 不读取环境变量 Token、holdings 或 prices CSV；
Python 核心 API 仍保留离线 CSV loader。TradingView 当前证券/市值信息不是历史快照，
不能直接用于历史回测。规则、输入格式及边界见
[动量筛选说明](../README.md#动量筛选入口)。
其输出尚未接入 risk_parity 回测，也不会自动转成模拟盘或实盘订单。

## 4. 认证与凭证生命周期

### 首次 OAuth 授权

1. `OAuthClient.register()` 向官方注册端点申请客户端 ID；已有 ID 可复用。
2. `authorize()` 在本地生成 `state`、PKCE verifier/challenge 和浏览器 URL。
3. 用户在富途页面登录并授权，浏览器重定向到配置的回调 URI。
4. `exchange_callback()` 校验回调地址和 `state`，使用本次 verifier 换取 Token。
5. 用 `SecretServiceStore.save()` 保存 `Credentials`，或由调用方自行管理存储。

当前没有内置本地回调 HTTP 服务，使用说明采用手动复制完整回调 URL 的方式。
`AuthorizationSession` 是一次授权的数据对象，不是 Python 解释器会话。
回调必须与创建链接的同一个授权对象配套。

### 持久化和刷新

- `TokenSet` 保存 access token、可选 refresh token、到期时间和 scope。
- `Credentials` 持久化客户端 ID、回调地址、Token 及可选的**实盘**账户 ID；
  当前存储格式不保存 `TokenSet.scope`。
- `SecretServiceStore` 将一组凭证写入默认密钥库的一个条目，
  使用 `application=finacial-futu` 与 `profile` 定位，不枚举其他应用条目。
- `client()` 创建默认预览模式的实盘客户端，并提供刷新持久化回调。
- `sim_client()` 复用凭证及持久化回调，但不继承已保存的实盘账户选择。
- 账户请求前，已知到期时间不足 30 秒时刷新 Token；
  若配置保存回调，必须先保存成功，再更新内存 Token 并发送业务请求。

密钥库不是服务端 Token 撤销系统；删除条目不等于取消富途授权。

## 5. HTTP 和错误处理

[client.py](../broker/client.py) 中的职责分层：

1. `_http()`：构造请求、设置认证头、编码查询参数/JSON/表单，
   使用超时发送一次请求，解析 JSON；不跟随重定向、不自动重试。
2. `FutuClient._request()`：刷新 Token，调用传输层并识别响应结构。
3. 公开账户/行情/订单方法：校验参数、选择路径、组装服务端字段。
4. `_write()`：控制预览或实际发送交易写请求。

| 响应类型 | 成功结构 | 客户端返回值 |
| --- | --- | --- |
| 实盘交易 | `s="ok"`，通常携带 `d` | `d`；成功改单/撤单允许没有 `d`，返回 `None` |
| 行情和模拟盘 | `ret_code=0`，携带 `data` | `data` |

业务错误和无法识别的响应会抛出异常，不转为空列表或虚假成功。
主要异常为 `FutuAPIError`、`FutuTransportError`、`TradingDisabledError`，
订单需要二次确认或网页操作时使用 `OrderConfirmationRequired`。
二次确认必须由调用方显式处理。

## 6. 实盘、模拟盘与预览

| 维度 | 实盘客户端 | 模拟盘客户端 |
| --- | --- | --- |
| 类 | `FutuClient` | `SimTradingClient` |
| 账户/订单路径 | `/api/v1.0/accounts/...` | `/api/v1.0/sim-trade/...` |
| 证券标识 | `code`，例如 `US.VOO` | `symbol`，例如 `VOO` |
| 市场选择 | 依接口使用市场/交易所参数 | 选定模拟账户的整数 `market_id` |
| 订单枚举 | 字符串，例如 `LIMIT/BUY` | 封装将字符串转换成服务端数字枚举 |
| 默认写行为 | 本地预览 | 本地预览 |
| 实际发送条件 | `dry_run=False` 且 `allow_live_trading=True` | `dry_run=False` |

`dry_run=True` 只阻止交易写请求，查询仍访问服务器。
模拟账户首次查询可能由服务端创建模拟账户。
模拟客户端通过组合复用 `FutuClient` 的请求层，不继承实盘订单方法，
并在请求和写入口检查模拟盘路径前缀。
这些约束防止正常使用时混用路径，不是对恶意调用者的进程级安全隔离。

## 7. 数据、分页和并发约定

- 金额和数量保留服务端字段；下单以十进制字符串发送，不静默四舍五入。
- Token 的 `expires_at` 为 Unix 秒；订单有效期和历史订单时间参数为 Unix 微秒。
  行情字段各自的时间单位以官方文档为准。
- 实盘订单/成交分页：`page_flag` 和 `completed`。
- 模拟历史订单分页：`pagination.next_key` 和 `pagination.has_more`。
- 缺失或重复游标会报错，避免无限循环或静默丢数据。
- 历史 K 线仅封装单页查询；不保证覆盖整个回测区间。
- 客户端同步执行、非线程安全，没有跨进程 Token 刷新锁。
  模拟策略层有单账本进程锁及持久化意图/累计成交去重，不是通用券商客户端的幂等保证。

## 8. 测试与已知边界

### Observed US simulation MARKET submission failure

On 2026-10-26 New York time (2026-10-27 Asia/Hong_Kong), user-run direct
broker-module tests on a US simulation account (`market=100`) returned HTTP
200 with `ret_code=-5` and nested `backend_biz_error` / `backend business error`
for MARKET SELL 2 MRNA and MARKET BUY 8 HAS. Position and capacity queries
worked and reported sufficient quantities; the user reported a successful
LIMIT BUY 1 VOO submission at price 1, without verified execution.
The direct failures isolate the issue from strategy ledger/window logic,
but do not establish the backend cause, a universal MARKET restriction, or an
incorrect enum. The documented broker `MARKET=3` mapping, reconciliation safeguards,
and no-retry behavior remain unchanged. By explicit user request, all new strategy
orders now use LIMIT, including remaining submissions from saved MARKET plans;
already submitted orders are not replaced. Legacy MARKET CLI arguments are
converted with a notice. The direct buy-test script uses the broker last price
plus 1% (optionally 2%), rounded up to cents, or an explicit LIMIT price override.
The test-script premium is separate from strategy execution: strategy orders
refresh snapshots and use ask for buys (rounded up to cents), bid for sells
(rounded down). Snapshot `update_time` is Unix milliseconds and must be no
more than 60 seconds old or future-dated. Invalid or missing quotes fail closed.
Limits feed capacity, fee, cash and buy-exposure checks. Each order refreshes
quotes again before intent creation and stops if they move beyond the checked
limit; there is no automatic repricing or retry.
See [broker usage notes](../broker/README.md) for the operational record.

离线测试：

- [test_trading_safety.py](../test/test_trading_safety.py)：实盘写操作保护、失败不重试、模拟盘路径及账户隔离。
- [test_strategy_ledger.py](../test/test_strategy_ledger.py)：部分成交去重、成本/净值、费用、基线隔离、对账和超时恢复。
- [test_play_money_orders.py](../test/test_play_money_orders.py)：波动率仓位、模拟账户接入及先卖后买的差额调仓。
- [test_momentum.py](../test/test_momentum.py)：股票池并集、动量精确窗口、排名、未来数据隔离、缺失数据、命令行退出码、两个独立入口及模块导入隔离。
- [test_tradingview.py](../test/test_tradingview.py)：动量选股的 TradingView 股票池筛选。
- [test_output.py](../test/test_output.py)：动量候选表格与 JSON 输出。
- [test_play_money_asset_risk_parity.py](../test/test_play_money_asset_risk_parity.py)：模拟资产风险平价的计算、CLI 及脚本流程。
- [test_play_money_asset_risk_parity_selection.py](../test/test_play_money_asset_risk_parity_selection.py)：候选排名、符号校验和 CLI 参数边界。
- [test_play_money_asset_risk_parity_risk_output.py](../test/test_play_money_asset_risk_parity_risk_output.py)：优化诊断输出及无效组合方差处理。
- [test_risk_forecasting_output.py](../test/test_risk_forecasting_output.py)：风险平价配置与最后一期权重输出。
- [test_kline_cache.py](../test/test_kline_cache.py)：行情缓存、增量更新和复权一致性。

测试范围涵盖动量筛选、风险平价、行情缓存、策略账本与交易安全隔离；
不再包含独立 OAuth、通用 HTTP 协议及真实密钥库集成测试。
离线模拟响应测试不能证明真实服务已接受参数或已授予权限。

当前模拟盘最大可买卖数量、今日订单及历史订单接口存在文档与服务要求不一致：
服务要求额外的 `market` 参数，封装已发送该参数；
历史订单每一页均携带所选账户的原始市场编号，未设置或无效时在请求前报错。
最大可买卖数量查询中用户后续遇到的笼统 `invalid parameter` 尚未确认根因。
不能将该接口描述为已完成端到端验证。

### 模拟账户 market_id=102 空内容记录

2026-10-18，用户提供的实际模拟账户列表中存在以下记录（省略账户 ID）：

```json
{
  "account_title": "",
  "account_type": 0,
  "broker_id": 0,
  "intra_account_id": 0,
  "market_id": 102
}
```

用户进一步确认该项未见内容。当前仅记录这一观察，尚未确认其用途、
是否启用或是否可交易；不能将其认定为占位账户或某一具体市场。
本项目保留 `102` 的编号支持，不映射成其他市场，也不自动删除该记录。
编号通过本地校验不代表该账户可交易，不建议使用它测试下单。
美股融资融券模拟账户的实际 `market_id` 为 `100`，
应使用 `sim.select_account(market=100)` 明确选择。
完整已观察市场列表见 [broker 使用说明](../broker/README.md)。

尚未实现自动策略调度、完整执行风控、持久化订单跟踪、断线恢复、
跨进程幂等、WebSocket 推送，以及 risk_parity 权重到订单的自动转换。
未来接入策略时，应先在模拟盘验证这些环节，再单独评估实盘上线。
