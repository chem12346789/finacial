# 股票筛选与风险平价

## 核心策略：核心资产风险平价策略

本项目以“核心资产风险平价策略”为核心策略，
[momentum.py](./momentum.py) 是其动量选股入口，保留现有文件名及调用方式。
当前已实现动态股票池、动量排名与前五名风险平价权重计算；
独立下单入口直接提交模拟仓订单，不提供实盘或预览模式。以下分别说明各模块。

项目按职责分为三个独立部分：

| 部分 | 职责 |
| --- | --- |
| [broker/](./broker/) | 仅负责券商通信：认证、账户、行情、实盘及模拟盘订单接口 |
| [momentum.py](./momentum.py) / [momentum_core/](./momentum_core/) | 动量筛选入口与核心实现；按调仓日动态筛选 NYSE、NASDAQ、AMEX 普通股 |
| [risk_parity/](./risk_parity/) | 根据已有价格和收益信息进行风险预测及风险平价 / 风险预算权重计算，不负责动量选股或下单 |

`momentum.py` 调用 TradingView 初筛并由 broker 获取筛选行情；筛选结果和风险权重
不会自行提交订单；提交需使用下文的独立下单入口。风险平价核心策略位于
[risk_parity/optimization.py](./risk_parity/optimization.py)，
根目录的 [core_asset_risk_parity.py](./core_asset_risk_parity.py) 负责下载行情并计算风险平价权重；
社保 / 定投计算仍作为独立研究脚本保留。
模块边界见 [架构说明](./docs/ARCHITECTURE.md)，
券商接入见 [broker 说明](./broker/README.md)。

目录已重命名：`FUTU` → `broker`，`RFO` → `risk_parity`。
Python 导入及 `python -m` 命令使用 `broker`；风险平价脚本路径改为
`risk_parity/optimization.py`。旧目录名不保留别名。
`FutuClient` 等类名、`FUTU_*` 环境变量及已有密钥库记录保持兼容；
原有可编辑安装和移过来的虚拟环境需按 [broker 迁移说明](./broker/README.md) 更新。

## 风险平价入口

在仓库根目录运行以下命令，下载价格数据并执行风险预测，默认只计算最新权重；
加 `--backtest` 才执行完整滚动回测：

行情通过项目内置的 Futu REST 客户端获取，使用 Secret Service 中的 `default`
凭证；按 360 个日历日分段请求历史日 K 线（更长窗口按 533 个日历日分段），
保持在 Futu 单页 370 根 K 线限制内；若返回满 370 根且起点晚于分段起点，
视为被截断并拒绝不完整历史。上市晚于分段起点的股票只保留已有数据。

```bash
python3 core_asset_risk_parity.py
# 或使用项目虚拟环境：
bash run_core.sh
# 运行完整滚动回测：
bash run_core.sh --backtest
# 指定其他 Python：
PYTHON=/path/to/python bash run_core.sh
```

`run_core.sh` 默认使用项目的 `broker/.venv/bin/python`，参数原样传给
`core_asset_risk_parity.py`；可从任意工作目录通过脚本绝对路径调用。

Latest-weight diagnostics use BIL's historical mean daily return multiplied by
252 as the sole annual benchmark. They report historical and forecast
half-Kelly positions, computed from the signed exposure that maximizes average
daily log wealth growth. Each scenario is weighted equally, so skew and observed
tail losses affect the result without histogram binning.
The displayed 3-SE downside return is the historical mean minus three standard
errors of the daily mean (an IID approximation), not a worst-case return bound.
Forecast Kelly keeps the sample mean and rescales deviations to match
`sqrt(w.T @ cov @ w)` from the same GARCH/Ledoit-Wolf covariance blend used for
risk budgeting. Historical Kelly uses the daily returns as observed. The
reported position is capped at one full allocation. Before the half-Kelly
search, the code checks whether the worst scenario remains solvent at `f=2`
and whether the log-growth derivative there is non-negative; if both hold, it
returns the full position directly without the 53-step search.
Reported returns and volatilities remain annualized; portfolio weights are unchanged.
Diagnostics describe the normalized risky basket before the cash allocation;
asset allocation weights are printed in a separate table.
These are sizing diagnostics, not leverage instructions or safety guarantees.

默认模式下载最近一个回看窗口的行情，直接用最新收益窗口计算一次风险预测与
风险预算权重并打印，不运行回测；`--backtest` 保留原有滚动回测计算，
标准输出仅显示最后一次调仓的权重。
Both modes print `Rebalance date: YYYY-MM-DD` before the weights: the latest
available price date in default mode, or the final historical rebalance date
with `--backtest`.
行情诊断及进度写入标准错误，不再自动生成权重图或 QuantStats HTML 报告。
若回测未产生权重，会在标准错误明确提示。Python `main()` 仍返回收益序列和权重历史。
配置类 `PipelineConfig` 与核心默认配置 `CFG` 保留在风险平价核心模块中；
根目录入口脚本通过自己的 `CFG = PipelineConfig(...)` 显式设置
`safe_target` 和 `risk_budget`，不影响核心模块的默认配置。
已移除不再使用的 `risk_free_rate`、`confidence_level` 配置项；
日期范围、回看窗口、调仓周期及交易成本仍参与现有回测计算。

## 动量筛选入口

要求 Python 3.10+。在仓库根目录安装统一依赖，并在默认 Secret Service
密钥库中配置 `default` Futu 凭证：

```bash
python3 -m venv broker/.venv
broker/.venv/bin/python -m pip install -r requirements.txt
broker/.venv/bin/python momentum.py \
  --tradingview --as-of 2026-10-15 --top 20

# 默认 stdout 和保存文件均为 JSON：
broker/.venv/bin/python momentum.py \
  --tradingview --as-of 2026-10-15 --top 20 --output ouput/momentum.json

# 可配置动态过滤条件（默认 $5、20 日 ADV 前 1000、市值前 1000；默认纳入 ADR 与 REIT）：
broker/.venv/bin/python momentum.py \
  --tradingview --as-of 2026-10-15 --min-price 10 \
  --adv-days 20 --adv-top 1000 --market-cap-top 1000
broker/.venv/bin/python momentum.py --help

# 调试/测试模式：只取初筛列表前 N 只股票，快速验证 K 线下载与筛选流程：
broker/.venv/bin/python momentum.py \
  --tradingview --as-of 2026-10-15 --top 20 --max-securities 100
```

CLI 不再接受 `--holdings` 或 `--prices`，也不读取 `FUTU_ACCESS_TOKEN`；
它从 Secret Service 的 `default` profile 创建行情只读客户端，无需选择交易账户。
股票目录从 TradingView 初筛获取，
ADV 与动量行情从 Futu 获取。Python 核心 API 仍保留 `load_universe` 和
`load_prices` 供既有离线调用；命令行入口为 `main()`。
原 `FUTU.momentum` 已移除，需改为 `momentum`；它不属于 broker 的 pip 安装包，
在其他工作目录运行时请使用根目录脚本的绝对路径。

### 输出格式

默认 `--format json`，保留完整配置、动态股票池元数据和逐股剔除原因。
每次筛选还会原子保存 JSON 到脚本所在项目的 `ouput/momentum.json`，
可用 `--output PATH` 指定其他文件；保存失败会报错，结果文件已加入 Git 忽略规则。
`--format table` 切换 stdout 为表格，不改变保存文件的 JSON 格式。
表格包含排名、代码、来源、风险调整评分、原始收益率、日波动率、起止日期与前复权价格，
并显示股票池大小、合格数量及剔除数量。空结果也会保存，退出码为 1。
所有结果格式化代码集中在 `ouput/`（按此目录名命名），不负责行情请求或计算；
进度仍写入 stderr，结果写入 stdout，可用 `--format table > momentum.txt` 保存表格。

### 动量前五名的风险平价

独立入口 `play_money_asset_risk_parity.py` 默认读取项目的 `ouput/momentum.json`，
按 `rank` 选择前 5 只股票，以等风险预算（各 20%）计算当前权重，不运行回测、不下单。
复用现有 GARCH 波动率预测、Ledoit-Wolf 混合协方差和 Riskfolio 优化器；
资金全部分配给这 5 只股票，不包含核心组合的 BIL 安全资产。

```bash
broker/.venv/bin/python play_money_asset_risk_parity.py
broker/.venv/bin/python play_money_asset_risk_parity.py --format json > ouput/play_money_weights.json
# 可离线读取 momentum.py --save-prices prices.csv 保存的前复权价格：
broker/.venv/bin/python play_money_asset_risk_parity.py ouput/momentum.json --prices prices.csv --lookback 20
```

默认输出格式化表格，包含权重、目标风险预算、实际风险贡献和估计年化波动率；
`--format json` 输出相同信息的 JSON，可重定向保存。
默认使用截至动量结果 `as_of` 的最近 63 个收益交易日（约三个月，需 64 个共同价格日），
波动率预测窗口为 7 个交易日。
在线模式通过 Futu 获取历史价格；离线模式仅使用五股价格齐全的共同日期。
动量筛选保存的历史长度取决于筛选参数，可能不足 64 日，
此时需提供更长历史或显式调小 `--lookback`，不会静默缩短窗口或替换股票。
不足 5 只股票、价格缺失、历史不足或优化失败均报错。
读取文件后、请求行情前检查结果的 `as_of`：不得晚于本机今天，默认最多过去 7 个自然日，
包含第 7 天，以兼容周末和节假日；可用 `--max-age-days N` 调整。
判断依据是数据日期而非文件修改时间，重新复制旧文件不会延长有效期。

### 波动率目标模拟下单（3000 USD，50% 年化波动，上限 1.33 倍）

[run_play_money.sh](./run_play_money.sh) 统一调用
[play_money_orders.py](./play_money_orders.py)，通过
[strategy_ledger.py](./strategy_ledger.py) 同步订单、入账并对账。
周末准备 ensemble 选股及风险平价输入，默认每周一纽约开盘时才生成一次交易目标；
其他时段只对账或在后续开盘窗口继续已保存计划。
按已有策略持仓与目标持仓的差额提交模拟订单，不因每天运行而重新选股。
筛选中间文件在项目 `.cache/finacial/` 下按次隔离，成功或失败退出时清理，
不使用系统 `/tmp`，不覆盖已有选股输出。沿用 `default` 密钥库凭证、
默认上个美股交易日及 63 日收益窗口（与 `play_money_asset_risk_parity.py` 的
`--lookback` 默认值一致）；`--as-of YYYY-MM-DD` 必须与周末数据日期一致，
可用 `--lookback N` 修改准备数据时的窗口，交易时须沿用。
风险平价会为最终五只股票按该窗口单独下载更长的前复权历史，不复用动量筛选导出的
短历史；若首次下载的共同价格日不足，按 1.5 倍逐步放宽下载区间重试（上限约 5 倍
lookback 的自然日），但 N 日收益仍严格要求至少 N+1 个共同价格日，不会自动缩短窗口。
两个 Shell 入口已合并，旧 `run_play_money_orders.sh` 已移除；
账本默认使用项目目录下的 `ouput/play_money_ledger.sqlite3`，不受当前工作目录影响。
也可用 `--ledger` 指定路径，相对路径从项目根目录解析；后续每次必须复用同一账本，不再提供 `--journal`。
位置日期可与下单选项组合使用；
`bash run_play_money.sh --help` 查看模拟仓参数。

For an intentional simulation reset, stop the timer, wait for all strategy
processes to exit, verify no outstanding broker orders, and archive the old
ledger and its SQLite sidecars rather than deleting them. Initialize the fresh
ledger with `bash run_play_money.sh --reconcile-only --history-start now`
(or a timezone-aware ISO timestamp such as `2026-10-26T00:00:00-04:00`).
The cutoff is persisted; omit the option on later runs. Only terminal orders
last updated strictly before the cutoff are ignored, including today's orders.
Open orders and later updates still block. Existing holdings remain baseline
assets, not strategy holdings; new capital must be available separately.
This does not fix broker submission errors or enable automatic retries.

“风险预算 50%”指**目标年化波动率 50%**，不是资金投入比例、止损线或最大亏损保证。
The default `--stop-loss` is 25% times current gross leverage (floored at 1x
leverage, so an unlevered or under-invested book still gets a 25% cap): a
backstop exit cap that sells a ledger holding when it falls at least that far
below average ledger cost, or earlier when the saved per-stock risk estimate
(2 x stock forecast weekly volatility) is tighter; the risk estimate caps the
stop but never loosens `--stop-loss`, so it normally binds and the 25% cap only
overrides it for extreme-vol names or positions without a saved estimate.
Upcoming-week stop previews use the planned basket's leverage for this default.
`--stop-loss 0` disables exits. It is checked only on script runs after
reconciliation with no
outstanding orders, including between weekly rebalances; `--reconcile-only`
and `--prepare-only` do not submit exits, and `--plan-only` only previews them.
Simulation exits are ordinary LIMIT sells, not broker-held protection.
Stop-loss submissions use the same NY 09:30-17:30 trading window as
rebalances, with the window rechecked before each order. Outside it, triggered
exits are reported as waiting without creating submission intents; rerun in the
window to recheck prices and submit. This is not all-day stop protection.
Broker API failures report the error code and HTTP status; uncertain submission
events preserve these fields without storing raw broker response details.
Current holdings show the active fixed-stop price separately from the
risk-based estimate calculated by `play_money_asset_risk_parity.py`: average
ledger cost times `(1 - 2 * stock annualized forecast volatility * sqrt(5/252))`
(2 x stock forecast weekly volatility below cost).
The 5-session horizon and 2x multiplier are illustrative, not backtested settings.
Weekend preparation forecasts held-stock risk separately from the selected five
candidates and caches it with the saved allocation, including the forecast date.
Missing forecasts (for example on reconcile-only runs before preparation) and
distances at least 100% are explicitly shown as unavailable; no artificial stop
price is substituted. Tighter estimates cap the active stop; looser ones are ignored.
Stop alerts outside the window no longer hide the saved upcoming weekly plan.
The report lists all five selected target positions separately from phased
SELL/BUY actions; sells can include old holdings outside the new basket.
Weekend and pending-cycle previews include both SELL and BUY differences, even
while execution is in the SELL phase. Their order-type column distinguishes
LIMIT prices (current quotes rounded down to cents) from MARKET reference
prices, which are not execution limits. Buys are submitted only after sells
have reconciled; showing the full preview does not submit both phases together.
The separate protective fixed/trailing-stop preview describes SELL protection
for each nonzero target holding, not LIMIT entry orders. Fixed triggers use
the active stop distance (the tighter of `--stop-loss` and any saved risk
estimate) from current average cost for held stocks or
an estimated entry quote for new stocks (recalculate after actual fills).
Target quantities and triggers are estimates, not accepted protective orders.
Zero disables fixed protection; trailing stops are explicitly marked not enabled.
Simulation does not submit broker-held protective orders.
When a weekly rebalance is due or unfinished, its saved targets take precedence
over a separate stop-only batch. Between rebalances, the fixed exit remains active.
Forecast volatility/covariance still determine sizing, not this stop distance.
For future real-account trailing-stop support, see the
[broker client examples](broker/README.md#protective-stops-real-client-only).

使用最近 63 个交易日收益，通过 GARCH 预测未来 7 个交易日的波动，
结合 Ledoit-Wolf 混合协方差及风险平价权重，得到未加杠杆组合的预测年化波动率。
理论倍数 = `0.50 / 预测年化波动率`，目标倍数 = `min(理论倍数, 1.33)`；
初始本金为 3000 USD；后续按**策略当前净值**而非固定本金计算仓位，
股票目标名义金额最多为净值的 1.33 倍。净值为 3000 USD 时上限为 **3990 USD**。
预测波动超过 50% 时留现金；低于约 **37.594%** 时理论倍数超过上限，按 1.33 倍封顶。
五股之间仍为等风险贡献；订单按权重分配目标金额，**新增订单全部使用限价单 LIMIT**，
数量和持仓估值仍使用快照 `last_price`。提交阶段重新获取买一/卖一报价：
买单限价取 `ask_price` 向上取整到美分，卖单取 `bid_price` 向下取整到美分。
`update_time` 为毫秒时间戳，超过 60 秒、未来时间、缺失或无效买卖价均停止提交，
不回退到旧成交价。现金、费用及买入敞口检查使用新限价；每笔意向创建前再次获取报价，
若报价超出已检查的限价则停止，需重新运行规划；不自动追价或重试。限价单不保证成交。
旧 `--order-type MARKET` 参数会明确提示并转换为 LIMIT；旧 MARKET 周计划后续新增订单
也使用 LIMIT，不撤换已提交订单，不重试未知提交结果。
股数按未取整快照成交价计算后向下取整；新的买入限价若使目标敞口超过上限则停止，
不足一股明确标记跳过，不将余款重新分配。
默认 `--capital 3000 --target-volatility 0.50 --max-leverage 1.33`，不接受高于 1.33 的上限；
旧 `--risk-fraction` 参数已移除，防止沿用资金比例的含义。
输出预测波动、理论/目标倍数、是否触顶、取整前预计波动、取整后实际订单倍数、
名义金额及手续费前现金/融资额。取整前预计波动不是取整后实际权重的精确风险，
更不是未来实现波动；策略融资额也不等于券商账户的实际借款。
终端参考核心资产入口，采用分段标题和对齐文本：账户、当前持仓、风险与仓位、
目标权重、买卖差额及券商订单 ID，不输出 JSON 或边框表格。
有挂单时只显示账户与等待状态；达到目标时显示无需下单。SQLite 内部审计格式保持不变。
历史前复权收盘价只用于权重计算，不作为订单价格。
市价单存在滑点，实际成交金额、融资额及杠杆可能超过报价估算值，1.33 倍是下单前的目标限制，
不是成交后的硬保证；停牌、交易时段、权限或模拟撮合限制仍可能导致未成交。
切换类型只影响新订单，不会自动撤销或替换已提交的限价挂单。
若已有挂单阻塞，应先在券商模拟界面核对并撤单，再运行脚本对账；不要删除账本或重复下单。

```bash
# 对账后直接提交模拟调仓差额，默认复用项目 ouput/play_money_ledger.sqlite3：
bash run_play_money.sh
# 指定选股日期，仍须通过数据有效期检查：
bash run_play_money.sh 2026-10-20
```

### 每日对账、每周调仓

每天手动运行同一命令即可，不需要切换模式；代码不会启动后台服务或安装定时任务。
默认调仓日为纽约时间周一，可用 `--rebalance-day WED` 等设置工作日。
**只有纽约 09:30（含）至 17:30（不含）的交易窗口允许新增订单**；每笔提交前再次检查时间。
周一休市或错过窗口，会在本周之后首个交易日交易窗口补做；窗口外只对账。
该窗口包含常规收盘后时段，但不改变订单类型或启用盘后交易权限；券商仍可能拒绝或延后执行订单。
使用现有 pandas 日历规则排除美国股票常规节假日，包含耶稣受难日、六月节及补休日，
不把哥伦布日、退伍军人日当成股市休市日；未知临时停市、个股停牌仍以券商为准。

周六、周日运行同一命令会先获取截至上个交易日的数据、筛选并预测波动率，
将下周输入保存到同一 SQLite 账本，然后对账但不下单；同一天相同设置不重复抓取，周日会刷新周六输入。
也可运行 `bash run_play_money.sh --prepare-only`，只准备数据，不访问模拟交易账户；
`--reconcile-only` 则只对账，任何日期都不抓取数据、不提交订单。
If a ledger reset removed this week's saved inputs, run
`bash run_play_money.sh --recover-weekend-inputs` on a weekday, then rerun
`bash run_play_money.sh --plan-only`. Recovery screens and allocates using
K-lines ending on the last trading session before the current week (normally
Friday), never current-week bars. It records the actual recovery date, does
not replace existing inputs, and neither accesses the trading account nor
submits orders. Historical screening availability and data revisions still
depend on the existing data pipeline; this is reconstruction, not an exact
replay of a previously deleted selection.
`--plan-only` 打印本周计划的调仓订单及预计费用但完全不提交；
不受开盘窗口限制，账本不写入任何订单意向，三个选项互斥。
开盘时只读取前一个周末的缓存，按最新净值和实时价格确定股数，不在开盘时重新筛选。
缺少本周缓存、回看窗口不同、数据过旧或费用对账失败均停止，不回退到旧周数据。
首次升级也必须先准备对应周末数据，不能绕过既有现金差异。
当周计划未执行完（仍有挂单或部分成交）时，对账输出会附带“Planned actions (pending rebalance)”一节，
列出完成该计划仍需买入或卖出的股数，便于核对等待中的目标。
当周计划已完成且下周的周末数据已保存（lookback 一致）时，对账输出会附带
“Planned actions (week of …, from saved weekend data)”一节，预览下周开盘窗口将提交的订单，
包括当前持仓中被换出的 SELL、目标新仓的 BUY；若持仓已与目标一致则显示 HOLD 目标行。

SQLite 持久保存每周唯一交易目标，包括固定股数、波动设置及订单类型；
周内重跑、跨日或重启均复用目标，参数改变不会中途重建目标（杠杆安全上限仍可收紧）。
计划完成后本周只对账，即使排名或价格变化也不重新调仓，下周才重新按净值生成目标。
若旧计划或挂单跨周未结束，先处理旧计划，不覆盖；达到旧目标后才可生成新周计划。
周内可能分多天提交卖出与买入，这是同一次周调仓的执行阶段，不是新增每日调仓。
保存目标因价格变动超出当前净值的杠杆上限时停止买入，不自动改股数或放宽上限。
每日对账不是盘中风控，也不承诺自动在杠杆超限时减仓。

#### 可选自动调度（未安装）

提供用户级 systemd 配置：[服务](./deploy/play-money.service)、[定时器](./deploy/play-money.timer)。
先检查服务中的仓库路径（默认 `%h/workspace/finacial`）和模拟账户凭证。
定时器按 `America/New_York` 自动处理夏令时：周末 10:00 准备数据；
工作日 09:30–17:29 每分钟检查一次，跟进同一计划的卖出/买入阶段并对账。
周计划完成后不再调仓；挂单/提交结果不确定时不重复提交。若窗口内未完成，后续交易窗口继续原计划。
定时器不补跑休眠期间错过的触发点，不会为了赶时间自动撤单、重试未知结果或放宽风险限制。

```bash
mkdir -p ~/.config/systemd/user
cp deploy/play-money.service deploy/play-money.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now play-money.timer
systemctl --user list-timers play-money.timer
journalctl --user -u play-money.service
```

以上安装命令需用户自行执行；主机须在线，用户级 systemd 和已解锁的 Secret Service 须可用。
当前仍只支持模拟仓，不启用实盘。

#### 可读交易流水与美股费用预估

每次运行都从 SQLite 重建账本同目录的 `*.transactions.txt`，默认
`ouput/play_money_ledger.transactions.txt`；原子替换，不重复追加成交，失败退出时也导出已持久化记录。
包括账户、UTC 提交/观察时间、股票、买卖方向、订单类型、请求股数、参考/限价、
券商订单号、状态、累计成交股数/均价/金额、每次观察到的成交增量，以及单独列出的已核实费用和利息。
观察时间不是交易所逐笔成交时间；接口仅有累计成交快照，不能据此虚构精确的成交时刻。
提交成功不等于成交；未获得订单号的未知结果会明确标记，不自动重试。

费用按[富途香港美股固定式套餐](https://www.futuhk.com/en/support/topic2_283)估算，
假设无免佣优惠、NMS 正股/ETF、整股、同一订单在同一交易日成交：

| 项目 | USD 费率 |
| --- | --- |
| 佣金 | 每股 0.0049，最低每单 0.99 |
| 平台费 | 每股 0.005，最低每单 1 |
| 交收费 | 每股 0.003 |
| SEC，仅卖出 | 金额 × 0.0000206，最低 0.01 |
| FINRA TAF，仅卖出 | 每股 0.000195，最低 0.01、最高 9.79 |
| CAT，NMS 股票 | 每股 0.000003 |

佣金、平台费分别以成交额的 0.5% 封顶，但最低收费优先；每项向上取整到美分，属于保守估算。
SEC 使用 [2026-04-04 起的费率](https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2)，
TAF 使用 [FINRA 2026 费表](https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule)；
富途页面抓取的 TAF 显示为 `0.00`，不将其视作免费，而采用 FINRA 明确公布的费率。
示例：买入 2 股、成交额 478.17 USD，预估 2.01 USD；同金额卖出预估 2.03 USD。
模拟策略采用用户指定的简化模型：每个有成交的订单固定扣 **0.99 USD**，不按股数、
成交次数或买卖方向重复扣费。首次部分成交时自动记入费用，后续成交、撤单及重复对账
不再收费；未成交的拒单/撤单不收费。现金、净值和总盈亏均包含这项假定费用。
规划及购买力检查也使用每单 0.99 USD，不使用上表的完整套餐总额。
对账会补记本账本中已有成交的订单，包括升级前已同步的成交；稳定的订单意向费用 ID
避免重启后重复扣费。此规则仅用于模拟策略，不改变实盘费用模型。
若此前已手动登记相同佣金，先核对并修正重复费用，不能再次运行旧手工费用登记命令。
流水中的成交费用预测基于累计成交额、假设单日成交；跨日订单可能重复最低收费，预测会偏低。
费率不是在线自动更新的；其他地区账户、阶梯套餐、优惠、OTC、ADR 费用和融资利息不包含在内。
上表的完整套餐预测不写入费用账目；模拟账本自动登记的是上述固定 0.99 USD 假定费用，
不是券商返回的实际收费。其余未解释现金差异仍停止，不自动抹平。

**此策略入口只允许模拟仓下单，校验通过后直接提交**，不再提供观察、预览或执行开关。
`--mode` 仅接受 `sim`，`--mode live` 和 `--confirm-live` 会在联网前被拒绝；
提交函数也拒绝实盘客户端。行情客户端只用于行情查询，不复用其已保存的实盘账户。
通过 `SecretServiceStore("default").sim_client()` 获取模拟客户端，
默认调用 `sim.select_account(market=100)`；没有匹配或有多个匹配账户时停止，
可用 `--account-id SIM_ACCOUNT_ID` 明确选择。不会复用已保存的实盘账户 ID 或回退到实盘。
通用 `broker` 实盘接口不受此策略入口限制的影响。
账本审计事件保留预测波动、理论倍数及是否触顶，不单独生成观察日志或触顶频率统计。

默认解释器为项目的 `broker/.venv/bin/python`，可用 `PYTHON=/path/to/python` 覆盖，
可从其他工作目录调用脚本绝对路径。首次初始化需至少有完整初始本金的可用 USD 现金，
取 `balance - hold`，之后不要求账户一直保留 3000 USD 现金。
无融资买入逐股检查 `max_cash_buy_qty_round_lot`；买入额超过策略现金时，
还需 `max_power_long` 覆盖买入批次，各股通过 `max_margin_buy_qty_round_lot` 校验；
卖出检查 `max_sell_qty_round_lot`。数量限制及资金限制均在该批第一笔提交前检查。
缺少融资资格、字段为空或额度不足均停止，不自动降仓。本入口不允许任何实盘订单。
未知资金字段、缺失报价、过期数据、选股或优化失败均停止。

### 策略账本、对账与恢复

- 首次初始化把账户现金及已有多头持仓保存为**外部基线**，只给策略分配初始 3000 USD；
  已有持仓不自动归入策略，也不会被策略卖出。账本绑定账户和初始本金，不可中途更换。
  首次若存在挂单或带策略备注的旧订单则拒绝初始化；不支持空头账户。
- SQLite 保存订单意图、券商订单 ID/状态、累计成交增量、显式费用及审计事件。
  模拟接口没有独立成交 ID，使用订单的 `cum_qty × avg_fill_price` 计算增量；重复同步不重复入账。
  部分成交、撤单及拒单均按实际累计成交入账；订单响应不等于成交。
  持仓采用移动加权平均成本，卖出记录已实现盈亏；现金为负时展示策略账面融资。
- 每次先同步今日订单及分页历史，再核对“账户持仓 = 外部基线 + 策略持仓”和账户现金变化，
  模拟账户现金容差为每个有成交的 BUY/SELL 订单 0.01 USD（累计计算，最低 0.01 USD，含边界）；
  每个订单只计一次，不按股数或部分成交次数累加，未成交订单不计入。
  容差内差额写入对账审计事件，不修改账本现金或登记费用。
  无法归属的挂单、数量/金额回退、缺失订单或超出容差的现金差额均停止新增订单；持仓仍须完全一致。
  费用或均价精度造成更大差异也不会自动抹平。账户里新增的外部交易、出入金、分红或公司行动
  可能触发停止；不会把这些差异擅自归到策略。现阶段建议使用专用模拟账户。
- 有挂单或部分成交未结束时仅同步、输出账本并等待，不撤单、不追加覆盖订单。
  到本周调仓日且无旧计划时按策略净值计算一次目标整股数量；先提交需要减仓的卖单，
  下次运行确认卖单结束、对账通过后再按保存的目标计算买入差额。已达到目标则本周不再下单。
- 输出策略现金、融资、净值、持仓成本、市值、已实现/未实现盈亏、已记录费用及实际敞口。
  每次落库净值快照。收益核算不承诺未来波动；上限不包含费用，盘中价格变化仍可能使实际杠杆超限。
  这不是常驻自动交易服务，需手动再次运行以同步成交和执行后续阶段。
- 手续费及融资利息**不能由当前订单接口直接取得**，不按账户差额自动估算。
  报告中的费用包含每个有成交订单自动登记的 0.99 USD 模拟费用及其他显式登记费用，
  不代表接口验证的实际总费用。
  核实属于本策略的账单后可登记；同一费用 ID 重跑不会重复扣账，不同数值会报错：

```bash
bash run_play_money.sh --expense-id statement-charge-001 --fee-usd 1.50 --interest-usd 0.20 --expense-note '已核对的券商账单编号'
```

该命令仍执行正常对账和调仓，不是只记账；仅用于**账本初始化之后**发生且已经由券商扣除的费用。
外部费用、出入金或分红不应伪装为策略费用；若误记需先人工核对修复，不能靠新 ID 重复登记。

下单前先事务提交意图，并在订单 `text` 中携带唯一标记；超时或异常立即停止，不重试、不自动确认券商警告。
下次运行可按唯一标记从券商找回订单；找不到、找到多个或状态不明时保持阻塞，不推定“没有成交”。
只有**向券商核实确实未提交**后，才可显式作废报错中给出的意图：

```bash
bash run_play_money.sh --void-intent INTENT_ID --void-note '券商确认未提交的核查依据'
```

作废也留审计记录，随后继续正常调仓；之后若该订单又出现在券商侧，会再次阻止新增订单。
同一个数据库采用进程锁，禁止并发运行；不要删除账本、另建账本或换路径来绕过状态检查。

#### 账本迁移后的本地调试

账本复制到调度主机（如 GCP）后，策略只能在该主机运行；本地再跑默认流程会与线上账本分叉，
再次触发初始化/对账保护停机。本地可做的安全调试：

- 离线测试（不访问行情与券商）：`python3 -m unittest discover -s test -t . -p 'test_*.py' -v`。
- 选股与数据管线（动量筛选、风险平价研究、K 线缓存）不碰模拟账户，可随意运行。
- 需要走账本逻辑时用一次性账本隔离：`--ledger /tmp/debug.sqlite3`，
  搭配 `--prepare-only` / `--reconcile-only` / `--plan-only`（后两者只读券商、不提交订单）。
- 本地**禁止**：默认路径 `ouput/play_money_ledger.sqlite3` 上的任何写入，以及不带
  `--reconcile-only`/`--plan-only` 的完整运行（可能到达下单阶段）。
- 只读排查可直接 SQL 查询账本（读操作不占用进程锁）；线上问题先看
  `journalctl -u play-money.service` 与交易流水文件。
旧 JSONL 不自动当作成交导入；若默认旧 `ouput/sim-orders.jsonl` 存在，首次初始化会停止，
须先人工核对旧订单与资产归属（包括其他路径的旧日志），迁移完成后再初始化，不能直接删除旧日志盲目重跑。
账本及锁文件包含账户/订单信息，默认路径已忽略 Git；请妥善备份，不要提交版本库。

### 文件写入范围

除系统密钥环中的 TOKEN、密码等凭据外，项目运行产物保存在仓库内：
动量 JSON 和权重图表位于 `ouput/`，K 线缓存位于 `.cache/finacial/`，
Matplotlib 配置和字体缓存位于 `.cache/matplotlib/`，缓存及图表均不提交 Git。
Riskfolio、ARCH 等依赖延迟至实际计算时导入，避免仅导入研究模块就初始化绘图缓存。
CLI 的 `--output`、`--save-prices`、`--cache-path` 相对路径均从项目根目录解析；
仓库外的绝对路径、`..` 或符号链接目标会明确报错。原子保存 JSON 的临时文件
与目标 JSON 同目录，不使用系统临时目录。已存在的用户级第三方共享缓存不自动删除。

### K 线缓存与增量拉取

风险平价默认以 `America/New_York` 当天之前的上一工作日为下载上限，跳过周末；
若上限为美股休市日，实际数据截止于返回的上一个交易日，不补造节假日行情。
默认不下载纽约当天的数据，同日重跑可复用历史缓存。显式传入的日期保持不变。
一键脚本默认 `as_of` 复用风险平价截止日；两者均不随本机时区变化。
若默认日期为休市日，动量筛选会明确报错，需显式指定上一个交易日。

CLI 默认将 Futu 日线持久保存到项目根目录的 `.cache/finacial/kline.sqlite3`，按股票、
周期、复权方式和交易时段隔离；中断后重跑可复用已完成股票的行情请求。
行情请求、K 线校验、节流重试与缓存实现统一位于 `broker/market_data.py`，
动量及风险平价入口均经由该模块访问 Futu。
每个股票请求或风险平价日期分段完成并通过校验后，立即提交 SQLite 事务，
不等待全部下载或计算结束；中断后可复用已完成的请求。风险平价使用同一数据库，
显式日期区间单独缓存，按区间、复权方式和请求条数精确匹配，不跨分段拼接复权口径。
风险平价从固定起始日向后按 360 个自然日分段（更长窗口为 533 日），
截止日推进只更新末段，
已完成的历史分段不随截止日移动。旧版倒序分段的缓存键不同，首次切换需重新建立缓存。
默认路径不受当前工作目录影响，`.cache/` 已加入 Git 忽略规则，不提交行情缓存。
可用 `--cache-path .cache/custom/kline.sqlite3` 指定仓库内位置，`--no-cache` 完全绕过缓存，
或 `--refresh-cache` 强制拉取完整窗口并覆盖缓存。无需新增依赖。

相同历史截止日、相同或更小窗口直接读取缓存，不调用 Futu，也不等待限速间隔。
截止日向后推进时，底层 `get_history_kline(start=..., end=..., num=...)` 仅请求
上次最后一根日线到新截止日的数据，并保留一根重叠 K 线用于校验和更新。
`num` 根据日历跨度缩小（周末、节假日不生成假 K 线）；窗口扩大、日期缺口过大、
或响应缺少重叠日线时重新获取完整窗口。接口单次仍最多返回 370 根。

前复权历史会因拆股、分红变化：若重叠价格改变，自动重新拉取完整复权窗口，
不直接拼接不同复权口径的数据。按纽约日期，当天及未来截止日也即时保存快照，
但尚未收盘时保存的请求不会命中缓存，下次仍重新请求，避免复用未完成行情。
缓存不会主动获知更早历史的供应商修订，必要时使用
`--refresh-cache`。增量减少传输量；每只更新股票、每种复权方式仍需请求，
不保证消除服务端限速，现有请求间隔和退避重试仍保留。

CLI 使用一个 `Kline` 总进度条，阶段标签依次为 `ADV` 和 `Momentum`，
计数与耗时不重置。ADV 阶段先按两倍候选股票数估算总量，筛选完成后更新为
候选数加最终股票池数量；缓存命中也计入已处理任务，不代表网络请求数。
这只是合并进度显示：两阶段仍分别使用未复权和前复权日线，不改变请求数量或计算口径。

### 筛选规则

动态股票池规则位于 `momentum_core/core.py` 的 `build_dynamic_universe()`，
由 `Security`、`DailyBar` 和 `DynamicUniverseConfig` 描述输入及阈值。默认只允许
NYSE、NASDAQ、AMEX 上市的普通股、ADR 与 REIT，要求调仓日收盘价至少 5 美元，
并分别按过去 20 个交易日的平均成交额和市值排名前 1000；最终股票池是两个排名
集合的交集。排名同值时按代码升序，避免结果不确定。阈值可通过配置调整。
输入成交额使用未复权收盘价乘成交量；后续动量计算仍使用复权收盘价。
ADR 默认纳入并使用相同门槛参与排名；使用 `--exclude-adrs` 可恢复排除 ADR，
Python API 对应 `DynamicUniverseConfig(exclude_adrs=True)`。
REIT 同样默认纳入；使用 `--exclude-reits` 或
`DynamicUniverseConfig(exclude_reits=True)` 可排除。原 `--include-reits` 参数仍兼容，
无需再显式指定，且不能与 `--exclude-reits` 同时使用。

动态股票池在 ADV/市值排名前强制排除 Biotechnology 及所有 Pharmaceuticals 行业
（TradingView 中对应 GICS 352010/352020 的分类），并排除财报发布前两个至发布后一个
美国交易日内的股票，包含边界；同时检查最近及下次财报日期。
周末及美国交易所节假日不计入交易日；非交易日财报归入下一交易日。
排除原因记录为 `excluded_industry` 或 `earnings_blackout`，被排除股票不会参与
动量排名或 GARCH 权重计算。TradingView 必须返回行业及两个财报字段；
财报日期为空仅表示没有可用日程，不保证没有事件风险。直接使用 Python API 时，
需提供 `Security.industry` 和 `Security.earnings_dates` 才能执行相应检查。

`momentum.py` 通过 `tradingview-screener` 初筛美国交易所的普通股，
取代码、交易所、类型、现价、市值、行业及最近/下次财报日期，并按市值缩小到前 1000 名；默认纳入 ADR 与 REIT。
随后用 Futu 原始日线的收盘价×成交量计算精确 20 日 ADV，再用 Futu 前复权日线计算
动量，因此无需 holdings/prices CSV。此模式仍需可用的 Futu token；TradingView 是初筛
来源，并不提供这里所需的完整 20 日历史 ADV / 动量价格序列。

TradingView Screener 返回的是查询时可见的数据，不是历史时点证券目录或市值快照。
当前模式适合当前调仓日的现场筛选；若 `--as-of` 指向过去，Screener 元数据仍可能是当前值，
不能用于无幸存者偏差的历史回测。查询依赖 TradingView 字段及其可用性，服务端字段、
覆盖范围或限流变化会明确报错，不会静默回退到 CSV 股票池。

筛选核心仍是纯函数，不执行网络请求；TradingView 适配层位于
`momentum_core/tradingview.py`，Futu 行情获取和 CLI 编排位于 `momentum.py`。

动量排名默认按共同交易日序列计算
`raw_return = adjusted_close[t] / adjusted_close[t - 21] - 1`，
`daily_return[i] = adjusted_close[i] / adjusted_close[i - 1] - 1`，
`daily_volatility = stdev(daily_return)`（样本标准差，分母为 N−1），
`score = raw_return / daily_volatility`，按风险调整评分降序取前 20 名。
收益与波动率使用同一计算区间，波动率不年化、不扣无风险利率，不缩放仓位；
零波动股票明确以 `zero_volatility` 剔除，至少需要 2 个日收益率。
可用 `--method` 切换评分，所有方法使用相同的复权价格、交易日窗口及排除规则：

| 方法 | 评分（均不年化） | 零波动处理 |
| --- | --- | --- |
| `volatility_scaled`（默认） | 区间累计收益 / 日收益样本标准差 | 剔除 |
| `roc` | 区间累计简单收益 | 保留 |
| `sharpe` | 平均日简单收益 / 日收益样本标准差，无风险利率为 0 | 剔除 |
| `regression` | 对数价格对交易日序号的回归斜率 × R² | 保留，价格恒定时为 0 |

`--methods volatility_scaled roc sharpe regression` 会共享一次股票池和行情下载，
分别保存 `<output stem>_<method>.json`；默认输出路径对应 `ouput/momentum_<method>.json`。
批量模式下 JSON 标准输出是结果数组，表格模式依次显示各算法结果；
单算法 `--method roc` 保持原来的单对象输出和 `--output` 路径。
四种方法均为相对动量，不保证选股结果不同；`--positive-only` 均按区间累计收益筛选。
综合排名用 `--method ensemble`：在四项共同合格的完整股票池中分别按评分降序排名，
同分取平均名次；先将 `volatility_scaled` 与 `sharpe` 名次平均为风险调整组，
再与 ROC、回归趋势名次等权平均。等效四项权重为 1/6、1/6、1/3、1/3。
综合平均名次越小越好，同分按股票代码排序，最后才截取 `--top`，不融合单算法前五名。
零波动及其他任一算法不合格的股票不参与综合排名；各单项名次在共同合格池中重新计算。
JSON 保留各项评分、各项名次、风险调整组名次与综合平均名次；
`score` 为综合平均名次的相反数，以保持评分越高越好的约定。表格同时显示分项名次。
默认仍为最近约一个月，`--lookback 21 --skip 0`；
终点为 `--as-of` 指定的交易日，需要至少 22 个完整日线观测值。
如需「12–1」计算区间，使用 `--lookback 252 --skip 21`，同样按波动率调整。
同分按代码排序。评分不是收益百分比；表格的 Return 和 Daily vol 才以百分比显示。
默认相对排名可能包含负动量；`--positive-only` 只保留严格正动量，
不会为凑满名额补入负动量股票。不产生组合权重或订单，也不构成投资建议。

### 输出与边界

JSON 包含筛选日、动量参数、动态股票池设置、股票池大小、可排名数量、候选及排除原因。
每个候选包含排名、代码、交易所、小数分数、起止日期及对应复权价格。
缺行情、末日过期、历史不足或窗口内缺交易日会明确排除，不填充价格；
供应商返回不完整或无效行会报错。Futu 单次历史 K 线请求最多 370 根，不自动翻页。
CLI 的 ADV 与动量行情请求共用节流器，初始请求间隔默认 2 秒，可用
`--request-interval 3` 设置更保守的起始间隔。共享重试计数初始为 0，每次限流失败加 1，
每次成功减 1（最低为 0）；请求间隔为初始间隔加计数 × 0.5 秒。
两个下载阶段共享该计数，缓存命中不改变计数。
两个下载阶段分别显示进度条、完成数、当前股票、耗时和预计剩余秒数（ETA）；
进度写入 stderr，不影响重定向到文件的 JSON。等待限流重试的时间也计入耗时。
官方公布的同 IP 每分钟 100 次是总体限额，并非保证历史行情能达到该速率；
实际还可能受其他配额影响，其他进程也会消耗额度。
同一请求遇限流最多尝试 5 次（含首次请求），失败后按更新后的计数 × 2 秒等待重试，
计数从 0 开始时，前四次等待分别为 2、4、6、8 秒；正常请求间隔仍单独保留，
并在 stderr 显示重试信息；第五次仍失败则报错退出。
其他错误直接报错。大股票池需要较长时间，不应并行启动多个筛选进程。
股类代码保留点号，例如 `BRK.B` 以 `US.BRK.B` 请求 Futu 行情，不合并不同股类。
Futu REST 日线日期优先读取 `date`（YYYYMMDD）；`time_key` 毫秒时间戳按返回的
`time_zone` 分钟偏移转换，未提供时区时使用纽约交易所时区，不使用本机日期。
筛选日必须是已完成交易日，不自动退回最近一天。
有候选退出码为 0；没有候选仍输出排除信息，退出码为 1；输入错误退出码为 2。

TradingView 证券和市值数据是查询时快照，不能用当前股票目录回测过去；
当前不包含回测、风险控制或自动调仓。

## 离线测试

```bash
# 全部离线测试（不访问行情服务）
python3 -m unittest discover -s test -t . -p 'test_*.py' -v
```
