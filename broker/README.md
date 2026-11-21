# 富途免网关 Python 接口

基于[官方快速开始](https://open.futunn.com/zh-cn/api/overview/getting-started)，
直接通过 HTTPS 调用 `https://webapi.futunn.com`，**不需要 OpenD，也不依赖 `futu-api`**。
券商通信代码位于 `broker` 包，与官方 SDK 的 `futu` 模块互不重名。
Python 3.10+。核心 HTTP 客户端仅使用标准库；
Linux 系统密钥库及研究脚本依赖已包含在仓库根目录的统一 `requirements.txt` 中。

项目级文档：[架构说明](../docs/ARCHITECTURE.md) ·
[安全注意事项](../docs/SECURITY.md)。
首次授权、部署或准备提交交易前，请先阅读安全注意事项。

## 功能与边界

- OAuth 2.1 Public Client + PKCE：注册、授权 URL、回调 state 校验、换取及刷新 Token。
- 授权账户、账户资金、持仓、最大可买卖数量查询。
- 未完成/历史订单、当日/历史成交自动分页，批量订单详情。
- 行情快照、单页历史 K 线。
- 共用行情封装：K 线日期和行解析、请求节流/限流重试、SQLite 历史缓存及批量日线读取。
- 下单、改单、撤单、显式二次确认；数量和价格以十进制字符串发送。
- 兼容交易接口的 `s/d` 和行情接口的 `ret_code/data` 两种响应结构。
- 独立的 `SimTradingClient`：富途云端模拟账户、资金、持仓、订单、下单/改单/撤单。

这里只实现上述 REST 接口和官方推荐的 OAuth 认证，不包含传统 API Key 签名、
WebSocket 推送、组合多腿订单、数字货币交易和自动策略调仓。
不会改动或自动执行现有 risk_parity 回测脚本。

行情请求、历史 K 线缓存与限流策略集中于 `broker/market_data.py`。
动量筛选策略仍位于根目录的 [momentum.py](../momentum.py)，独立入口与输入格式见
[项目说明](../README.md#动量筛选入口)；风险平价也通过同一行情模块读取 Futu 日线。

目录及导入名已由 `FUTU` 改为 `broker`，例如 `from broker import FutuClient`；
凭证管理命令为 `python -m broker.credentials`。
为兼容已有安装和凭证，pip 分发名仍为 `finacial-futu`，
`application=finacial-futu` 密钥库标识及 `FutuClient` 等类名不变。
原有可编辑安装需按新路径重新安装；旧导入名不再提供。
目录内的虚拟环境也随目录移动，但激活脚本和 pip 启动脚本可能仍引用旧绝对路径，
不要直接复用旧激活状态；请退出环境，在新路径重新创建环境并安装依赖。

## 代码阅读指南

这些 Python 类是本项目对官方 HTTP 接口的封装，不是官方 SDK。
调用方法返回官方响应中的 `d` 或 `data`；不会将错误转换为空账户或虚假成功。

| 模块 | 职责 | 建议阅读顺序 |
| --- | --- | --- |
| [client.py](./client.py) | HTTP 传输、OAuth、Token 刷新及响应解包 | `TokenSet` → `AuthorizationSession` → `OAuthClient` |
| [futu_client.py](./futu_client.py) | 实盘账户与订单 | `FutuClient.get_accounts()` → `get_funds()` → `place_order()` |
| [sim_trading_client.py](./sim_trading_client.py) | 独立模拟盘端点、参数枚举及分页 | `SimTradingClient.get_accounts()` → `get_max_qty()` → `place_order()` |
| [credentials.py](./credentials.py) | 系统密钥库记录、读写和客户端创建 | `Credentials` → `SecretServiceStore.save/load` → `client/sim_client` |

一次查询的内部流程：

```text
sim.get_max_qty(...)
  → sim_trading_client.py：验证参数，将 LIMIT/BUY 等转换为模拟盘数字枚举
  → client.py：必要时刷新 Token → 保存刷新结果 → 发出一次 HTTP 请求
  → 解析 ret_code/data → 返回数据，或抛出 FutuAPIError
```

`_request`、`_write` 等下划线开头的方法是内部实现，业务代码应调用公开方法。
`_write` 决定仅预览还是发送订单；HTTP 方法为 POST 不一定意味着下单，
例如行情快照和订单详情也是 POST 查询。
实盘分页使用 `page_flag/completed`，模拟盘历史订单使用 `next_key/has_more`，
因此二者分别处理，不能直接混用。

几个容易混淆的名称：

- `sim` / `client`：示例里的普通变量名，分别指模拟盘/实盘客户端。
- `session`：`AuthorizationSession` 保存一次授权的 PKCE 校验值，不是 Python 解释器会话；
  实盘 `place_order(session=...)` 的同名参数则表示交易时段。
- `profile`：密钥库里一组凭证的名称，不是交易账户 ID。
- `code`：实盘/行情代码（例如 `US.VOO`）；`symbol`：模拟盘代码（例如 `VOO`）。
- `expires_at`：Token 到期的 Unix **秒**；订单有效期及历史订单时间参数使用 Unix **微秒**。
- `dry_run=True`：不发送交易写请求，并不等于进入富途云端模拟盘。

## Google Cloud / pip 部署（推荐用于 e2-micro）

无需 Conda。使用系统 Python 3.10+ 和标准库 `venv` 即可。
根目录 `requirements.txt` 安装本地 `broker` 包及项目其他模块所需依赖；
broker 核心运行时本身没有第三方依赖，也不安装 `futu-api` 或 OpenD。
默认使用普通安装，安装后从任意目录均可 `from broker import OAuthClient`。

在 Debian / Ubuntu 实例中，若缺少 Python 或 `venv`，先安装系统组件：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
```

上传或克隆仓库后，从仓库根目录执行：

```bash
python3 -m venv broker/.venv
broker/.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
broker/.venv/bin/python -m pip check
broker/.venv/bin/python -c "from broker import OAuthClient, FutuClient; print('broker import OK')"
```

清单中的 `.` 安装仓库根目录声明的本地券商包；安装时需同时提供
`pyproject.toml`、`README.md` 和源代码。这里使用 `broker/.venv`；
源码更新后重新执行安装命令。
`venv` 不复制整套 Conda 发行版，`--no-cache-dir` 避免保留 pip 下载缓存。

日后运行可先激活：

```bash
source broker/.venv/bin/activate
```

无人值守云实例不要依赖桌面 GNOME Keyring 自动解锁；建议使用 Google Cloud
Secret Manager 等秘密管理服务，并自行配置最小权限的凭证注入与刷新后持久化，
这部分不包含在基础安装中。不要把 Token 写入依赖清单、源码或镜像。
首次 OAuth 授权仍需人工完成；安装成功不等于账户已授权。

桌面 Secret Service 集成依赖已由根目录统一清单安装，仍需按下文配置系统服务。
只有需要小写 `import futu` 的官方 OpenD SDK 时才另行安装 `futu-api`，
它不是本 HTTPS 客户端的依赖。

## Conda 环境与官方 SDK

本机已新建独立 Conda 环境 `futu`，使用 Python 3.12，安装了官方
`futu-api==10.11.7108` 和本项目的系统密钥库依赖，未替换原有环境。
目录重命名后，需重新安装本地 `broker` 包，之后不必在仓库根目录才能导入。
从仓库根目录激活、重新安装并验证：

```bash
conda activate futu
python -m pip install -r requirements.txt
python -m pip check
python -c "import futu; from broker import FutuClient, OAuthClient; from importlib.metadata import version; print(version('futu-api'))"
```

在其他机器上重建：

```bash
conda create -n futu python=3.12 pip -y
conda activate futu
# 从仓库根目录执行，将本地包及密钥库依赖一起安装。
python -m pip install -r requirements.txt
```

若出现 `ModuleNotFoundError: No module named 'broker'`，注意 `broker`
是本仓库的包，不是 `pip install futu-api` 提供的小写 `futu`。
退出已有 Python 交互会话，在终端中执行：

```bash
conda activate futu
python -m pip install -e '.[keyring]'
python -c "import sys; from broker import OAuthClient; print(sys.executable)"
```

请在仓库根目录执行安装。重新启动 Python 后再导入；
VS Code 或 Jupyter 也需选择 `futu` 环境的解释器或内核。
可编辑安装依赖源目录保留在原位置，移动仓库后需重新安装。

两套接口互不替代：`import futu` 是官方 SDK，连接行情、交易接口需要另行安装、
运行并登录 OpenD；`from broker import FutuClient` 是本仓库的免网关 HTTPS 客户端，
使用 OAuth 授权，不需要 OpenD。安装 SDK 不代表已完成账户授权或实盘连通验证。
使用此 Conda 环境时，下文的 `broker/.venv/bin/python` 可替换为激活后的 `python`；
原有虚拟环境仍保留。

## 1. 首次授权

在仓库根目录运行 Python，按以下步骤执行。账户需要先开通相应 OpenAPI、
交易及行情权限；权限不足会明确抛出异常。

```python
from broker import OAuthClient

redirect_uri = "http://localhost:60355/callback"
registration = OAuthClient.register(redirect_uri, "My quant client")
# 将 registration["client_id"] 保存到自己的安全配置中；只需注册一次。
oauth = OAuthClient(registration["client_id"], redirect_uri)
session = oauth.authorize()
print(session.url)
```

在浏览器打开授权链接，登录、选择账户并授权。然后在**同一 Python 会话**内：

```python
# 粘贴浏览器最终跳转的完整地址，其中含 code 和 state。
callback_url = input("授权回调 URL: ").strip()
tokens = oauth.exchange_callback(callback_url, session)
```

这个最简流程不启动本地 HTTP 服务：浏览器跳转 localhost 后可能显示无法连接，
此时复制地址栏中的完整回调 URL 即可。也可以自行实现 `/callback` HTTP 路由，
把收到的完整 URL 传给同一个 `exchange_callback` 方法。
授权码有效期 5 分钟且只能使用一次。必须保留本次 `session`，不能重新生成后换码。

Token 不会自动写入磁盘或打印。请使用系统密钥库、秘密管理服务或外部安全配置保存
`tokens.access_token`、`tokens.refresh_token`、`tokens.expires_at` 和客户端 ID，
**不要提交到 Git，也不要将 Token 发给他人**。注册响应里的管理令牌同样是秘密。
本模块的 Token 和 PKCE verifier 不会出现在对象的 `repr` 中。

### Linux 系统密钥库（推荐）

使用当前 Linux 用户的 GNOME Keyring / Secret Service，而非仓库里的 `.env` 文件。
凭证以一个条目保存在默认持久密钥库中；条目名称为 `Futu API (default)`，
属性仅包含 `application=finacial-futu` 和 `profile=default`，不包含账户 ID 或 Token。
不会枚举其他应用的秘密，不会创建新的主密钥库、修改密码或自动解锁。
密钥库不存在、被锁定或加密会话不可用时直接报错，**没有明文文件后备方案**。

使用 Secret Service 需要当前用户会话提供已解锁的 GNOME Keyring / Secret Service；
无桌面的服务器不一定提供该服务。创建专用虚拟环境时可执行：

```bash
python3 -m venv broker/.venv
broker/.venv/bin/python -m pip install -r requirements.txt
```

从仓库根目录运行：

```bash
# 检查服务、锁状态和加密连接，不读取任何已保存的凭证。
broker/.venv/bin/python -m broker.credentials check

# 在本机交互终端录入。Token 和账户 ID 隐藏输入，不进入命令历史。
# 再次执行会替换该 profile 的旧凭证，不影响其他 profile。
broker/.venv/bin/python -m broker.credentials set

# 仅删除该 profile 的密钥库条目，需要输入 profile 名称确认。
# 不会撤销富途服务器端 Token；如凭证泄露，还需在富途侧撤销授权。
broker/.venv/bin/python -m broker.credentials delete
```

录入内容：OAuth 客户端 ID、回调 URI、可选账户 ID、access token、
可选 refresh token 和 access token 到期时间（Unix 秒）。
不要把富途登录密码或交易密码放进这里。
也可以在上面的首次 OAuth 授权流程完成后直接保存，不必复制 Token：

```python
from broker.credentials import Credentials, get_store

store = get_store("default")
store.save(Credentials(
    tokens,
    client_id=oauth.client_id,
    redirect_uri=oauth.redirect_uri,
    account_id=None,  # 查询授权账户并人工确认后，再保存选定的账户 ID。
))
client = store.client()
```

若出现 `org.freedesktop.secrets was not provided by any .service files`，
说明当前用户会话没有 Secret Service，并非 OAuth Token 保存格式错误。
`SecretServiceStore` 只使用本地密钥库；`get_store` 在本地服务不可用时改用
GCP Secret Manager，需要可解析的 GCP 项目、Application Default Credentials
及相应秘密的读写权限。非 GCP 主机也可显式使用
`SecretManagerStore("default", project="YOUR_PROJECT_ID")`，从
`broker.credentials` 导入；否则需先配置本地 Secret Service，再使用本地密钥库。
保存失败时可在同一 Python 会话中复用已有的 `tokens` 和 `oauth`，
无需重新交换已使用的授权码。继续使用原 profile 和后端，以便后续运行读到新凭证。

`store.client()` 始终创建默认预览模式的客户端，账户和行情查询仍访问真实 API。
访问令牌刷新成功后，会先将新 Token 和到期时间写回同一个 profile，再发送业务请求。
若写回失败则终止本次业务请求，不会悄悄继续使用未持久化的 Token。
多个账户/应用可用 `--profile another`、`SecretServiceStore("another")` 隔离。
不要让多个进程同时刷新同一个 profile，仍需外部协调。

**解锁与安全边界：** 使用登录用户的桌面会话运行，不要加 `sudo`。
GNOME Keyring 通常由 PAM 随桌面密码登录解锁；自动登录、SSH、cron、
systemd 后台任务或重启后未登录时不保证已解锁或能连接会话总线。
遇到锁定请打开“密码和密钥”（`seahorse`）解锁。不要把主密码写入脚本，
也不要为了无人值守把密钥库密码设为空。
此接入验证了 D-Bus 会话加密，但不检查或修改现有密钥库的主密码；
磁盘上的保护取决于密钥库是否设置了强密码及操作系统/磁盘安全。
已解锁会话内同一用户的其他程序可能也能访问密钥库，它不能隔离同一用户下的恶意程序。

当前保留的离线测试不接触真实密钥库或富途 API；
测试范围聚焦核心策略、行情缓存、实盘防误下单及模拟盘隔离，
不再提供独立的真实密钥库集成测试。

## 2. 查询账户与行情

```python
from broker import FutuClient

client = FutuClient(tokens, oauth=oauth)
account = client.select_account()  # 自动查询并选定唯一授权的实盘账户。
print(account)

funds = client.get_funds(currency="USD")
positions = client.get_positions()
snapshots = client.get_market_snapshot(["US.VOO", "US.QQQ", "US.VXUS", "US.BIL"])
orders = list(client.iter_orders("US"))
fills = list(client.iter_fills("US"))
history = list(client.iter_orders("US", history=True))
details = client.get_order_details("US", ["替换为订单 ID"])
max_qty = client.get_max_qty("US.VOO", price="600.00")
bars = client.get_history_kline(
    "US.VOO", start="2026-10-01", end="2026-10-16", num=370,
)
```

`select_account()` 仅在授权列表中有唯一账户时自动设置 `client.account_id`；
多个账户时可用 `client.select_account(account_id="选定账户 ID")` 明确选择。
无匹配、多候选或响应异常时明确报错，不改变之前的账户选择。
通过密钥库创建的客户端同样可以调用此方法；选择只保存在当前对象中，
不会改写密钥库的账户 ID，也不会改变 `dry_run` 或 `allow_live_trading`。
原有 `get_accounts()` 仍只查询，不自动选择。实盘市场/交易所继续按证券与接口指定，
不会从模拟盘 `market_id` 推断。

返回值是官方响应里的 `d` 或 `data`，保留原始字典/列表、字符串金额和字段，
不擅自转换精度、补数据或更改币种。改单/撤单成功但没有 `d` 时返回 `None`。
未完成订单接口也会包含过去 24 小时内已成交/撤销的近期订单，应检查 `order_status`。
可自行将 `positions`、`snapshots["snapshot_list"]` 等转为 pandas DataFrame。
行情快照可能只返回请求中部分合法的代码；使用前应将 `snapshot_list[].code`
与请求列表比对，不能把缺失代码当作零价格或零持仓。

订单/成交迭代器按 `completed` 自动终止，游标异常会报错而非静默截断。
`trd_market` 与 `exchange` 不是同一个枚举：例如港股查询市场为 `HK`，
改单/撤单/详情查询的交易所为 `SEHK`；美股两者均为 `US`。
历史查询支持 `code/start/end`，其中 `start/end` 为 **Unix 微秒时间戳**；
历史成交每页 10–50 条，其他订单/成交查询每页 10–100 条。

历史 K 线一次最多 370 条；本封装不自动翻页，不保证一次返回完整回测区间。
保留 `next_time` 原值供调用方处理，但官方当前文档中下一页 `end` 的毫秒游标说明与
日期字符串参数定义不一致，未在封装内猜测其转换规则。
注意行情的 `volume_precision`，特殊品种成交量需按官方说明还原；
K 线默认 `ktype=2`、`autype=1`，具体枚举见官方文档。

日本账户查询可传 `asset_category`；下单及数量查询可传 `sub_acc_id`、
`position_id`。具体券商限制和字段要求以官方接口为准。

## 先用富途模拟盘搭建（推荐）

模拟盘与实盘是两套不同的 REST 路径和参数。**不是给实盘客户端设置
`dry_run=False`，也不是把实盘 `account_id` 填进模拟盘。**

| 客户端/开关 | 效果 |
| --- | --- |
| `FutuClient`，默认设置 | 实盘查询，交易仅本地预览 |
| `SimTradingClient`，`dry_run=True`（默认） | 模拟盘查询，交易仅本地预览 |
| `SimTradingClient`，`dry_run=False` | 提交到富途云端模拟账户，不调用实盘交易路径 |

### 1. 读取已有凭证并查询模拟账户

在仓库根目录使用密钥库环境启动 Python：

```bash
broker/.venv/bin/python
```

在同一 Python 会话中执行：

```python
from broker.credentials import get_store

sim = get_store("default").sim_client()
selected_account = sim.select_account(market=100)  # 美股融资融券模拟账户。
print(selected_account)
```

此处复用 OAuth 凭证及刷新持久化，但**不会沿用密钥库里的实盘账户 ID**。
查询走 `/api/v1.0/sim-trade/accounts`；官方说明首次调用可能自动创建模拟账户。
`select_account(market=100)` 查询账户列表，选择唯一的美股融资融券模拟账户，
同时设置 `sim.account_id` 和 `sim.market`，不再需要手动复制账户号。
返回选中的账户记录，可核对 `account_title`、`account_id`、`market_id`。
如果只有一个模拟账户，也可完全自动配置：

```python
# 以下是其他选择方式，与上面的 market=100 方式择一使用。
# sim.select_account()  # 所有市场合计只有一个账户时自动设置账户号和市场。
# sim.select_account(account_id="选定的模拟账户 ID")  # 自动从账户记录补齐市场。

print(sim.get_funds())
print(sim.get_positions())
print(sim.get_orders())  # 今日订单
```

没有匹配账户或存在多个候选账户时明确报错，不会默认选择第一项，
也不会改变之前的选择。多个同市场账户须用 `account_id` 区分。
此方法不改变 `dry_run`，不启用交易，不读写实盘账户选择。
原有 `get_accounts()` 仍只返回列表，不自动更改当前配置。

账户选择兼容整数及数字字符串形式的 `market_id`（例如 `2` 和 `"2"`），
选中后统一设置为整数。指定市场或账户号时，其他账户的未支持整数市场值
不会阻断筛选；若候选账户本身的市场不受支持则明确报错，不擅自映射市场。
缺失字段或字段类型错误仍明确报错；
异常仅标明记录索引、字段和类型，不包含账户号或 Token。
升级代码后须重新启动 Python 会话；普通 pip 安装还须重新安装本地包。

模拟账户的 `market_id` 不能套用实盘或其他接口的市场枚举。
根据实际账户列表返回，目前观察到：

| market_id | account_title |
| --- | --- |
| 100 | 美股融资融券模拟账户 |
| 1 | 港股模拟账户 |
| 3 | A股模拟账户 |
| 16 | 日股模拟账户 |
| 11 | 美国期货模拟账户 |
| 9 | 港股期权模拟账户 |
| 10 | 香港期货模拟账户 |
| 12 | 新加坡期货模拟账户 |
| 13 | 日本期货模拟账户 |
| 102 | 空标题、用户确认未见内容；用途及可用性未确认 |

**已知异常（2026-10-18）：** 实际账户列表返回 `market_id=102`，
`account_title=""`、`account_type=0`，用户确认该项未见内容。
目前不能确定它是占位记录、未启用账户还是其他账户类型，不据此推断其用途，
也不建议用它测试下单。代码保留该编号，不自动映射或删除；
美股融资融券模拟账户应明确使用 `market=100`。
观察详情见 [架构文档的已知边界](../docs/ARCHITECTURE.md#模拟账户-market_id102-空内容记录)。

以上值均支持整数或数字字符串形式的账户选择；保留原有 `2`、`18`
支持以兼容已有调用，但不在此推断其账户类型。支持市场编号不代表已验证该市场
所有交易品种、合约格式及交易规则；期货、期权等仍需按对应接口要求单独核对。
应使用该账户返回的 `market_id`；`100` 不会映射成 `2`，
账户选择后的查询和下单预览/提交均保留原值。
实际可用账户及标的以服务器返回为准。资金响应是 `balance/total_asset` 等模拟盘字段，
不同于实盘的 `cash/total_assets`。

**鉴权边界：** 模拟盘页面示例未完整说明 OAuth scope 与登录态的关系；
本封装按快速开始统一使用已有 Bearer Token，不提取浏览器 Cookie，也不回退到实盘。
已验证本地请求协议，尚未使用真实用户凭证验证模拟盘连通性。
如果此处报 401/403 或业务权限错误，请保留不含 Token 的错误码和说明，
核对富途模拟交易权限/授权支持；不要将“实盘账户能查询”当作模拟盘授权已通过。

### 2. 查询数量并预览模拟订单

```python
# 模拟盘证券参数是无市场前缀的 symbol；实盘/行情接口才使用 US.VOO。
print(sim.get_max_qty("VOO", order_type="LIMIT", price="100.00"))
preview = sim.place_order("VOO", qty="1", side="BUY", price="100.00")
print(preview)
assert preview["environment"] == "SIMULATE"
assert preview["dry_run"] is True
```

`get_max_qty()`、今日订单查询 `get_orders()` 和历史订单查询
`iter_history_orders()` 都会将 `sim.market` 作为查询参数 `market` 发送，
历史订单的每一页请求都会保留该参数。
虽然官方当前参数表未列出它，实际服务会检查该必填参数；
先调用 `sim.select_account(...)` 即可同时配置两者；若手动配置，
必须将 `sim.market` 设为所选模拟账户返回的整数 `market_id`，不能只设置账户 ID。

上面的价格仅演示格式，不是实时价格或推荐价格。先查看行情、可买数量、
模拟账户资金和整手要求，再选择测试价格与数量。港股数量须符合每手倍数。
客户端和接口文档提供 `LIMIT/MARKET`，方向支持 `BUY/SELL/SELL_SHORT/BUY_BACK`；
模拟撮合和真实市场成交不能等同。

**Observed simulation limitation (2026-10-26 New York / 2026-10-27 Asia/Hong_Kong):**
On the tested US simulation account (`market=100`), direct MARKET submissions
for SELL 2 MRNA and BUY 8 HAS failed with HTTP 200, `ret_code=-5`, and nested
`error.code="backend_biz_error"` / `error.message="backend business error"`.
Position/capacity queries succeeded; reported sellable MRNA and cash-buy HAS
quantities were sufficient. The user reported a successful LIMIT submission
for BUY 1 VOO at price 1; acceptance does not establish execution.
This is an observed account/backend limitation, not proof that every simulation
account rejects MARKET orders or that the documented `MARKET=3` enum is wrong.
The broker mapping remains unchanged. Strategy submissions now explicitly use
LIMIT for all new orders, including remaining orders in old MARKET plans;
legacy strategy `--order-type MARKET` arguments emit a notice and use LIMIT.
Existing broker orders are not replaced, and uncertain submissions are not retried.
Check orders and fills after errors before another attempt.

### 3. 确认后提交到模拟账户

```python
# 这里只开启模拟盘写操作，不存在 allow_live_trading 开关。
sim.dry_run = False
# 自行确认测试数量、价格后取消下面注释：
# result = sim.place_order("VOO", qty="1", side="BUY", price="自行确认的测试限价")
# print(result)  # {"order_id": "..."}，仅表示接口成功，不代表订单已经成交。
print(sim.get_orders())  # 检查 status、cum_qty、avg_fill_price。
print(sim.get_positions())
```

模拟订单 `status`：`2=已提交`、`3=部分成交`、`4=全部成交`、`5=已撤`、`6=拒绝`。
官方说明只有 `status=2` 才能改/撤；已成交订单不能通过撤单还原。

For submission debugging, catch `FutuAPIError as exc` and print
`json.dumps(exc.diagnostics(), indent=2)` before re-raising. The diagnostic
includes the HTTP status when available, broker code, simulation endpoint
with the account ID masked, and whitelisted order parameters. Response values,
headers, tokens, account IDs, and order remarks are not included; response
field names and nested field types are provided instead. Do not print
`exc.details` wholesale, as it may contain sensitive values. This does not
retry a request; check orders and fills before another submission.

For a one-shot BUY 8 HAS LIMIT simulation test, run
`broker/.venv/bin/python test_sim_buy.py` from the repository root to preview,
then add `--submit` only when ready to send. Use `--account-id` if needed.
The script checks cash-buy capacity and never retries. Keep the strategy timer
paused: this test bypasses its ledger, so establish the fresh ledger baseline
only after all test orders are terminal. The default limit is the broker's
HAS last-price snapshot plus 1%, rounded up to cents. Use `--premium-percent 2`
for 2%, or `--price YOUR_LIMIT_PRICE` to override without fetching a quote.
The limit is the maximum acceptable buy price per share, not the expected fill
price. Last price is not the current ask; a LIMIT order may remain unfilled.
If the safe diagnostic reports a nested `error` object, opt in with
`--show-error-details` to print its raw `code` and `message` locally. These
messages may contain sensitive data: inspect and redact before sharing.
The flag does not replay or recover past responses; do not submit again just
to obtain logs before verifying the previous attempt's orders and fills.

```python
# 仅在确认订单可改/撤后执行；dry_run=False 时会改变云端模拟订单。
# sim.modify_order("模拟订单 ID", qty="2", price="新的测试限价")
# sim.cancel_order("模拟订单 ID")

history = list(sim.iter_history_orders(page_size=50))
# 可指定 time_begin/time_end，单位 Unix 微秒。
sim.dry_run = True  # 测试后恢复本地预览。
```

Simulation modification and cancellation bodies include the selected account's
`market_id` as `market`, as required by the running service. Select the account
and market before either operation, including dry-run previews.

所有模拟请求强制限制在 `/api/v1.0/sim-trade/` 路径，复用 HTTP 超时、
错误处理和 Token 刷新，不自动重试订单。
模拟历史订单按 `pagination.has_more/next_key` 自动翻页，与实盘游标不同。
历史响应中 `orders` 必须为列表；存在 `pagination` 时必须为对象，
`pagination.has_more` 必须为布尔值。实际服务也会只返回 `orders` 而省略
`pagination`：此时仅当订单数量小于请求的 `page_size`（包括空列表）才按末页处理。
此兼容行为假定服务遵守请求的分页大小；若返回满页或超出分页大小却没有分页信息，
封装会报错而不是猜测游标或宣称历史已完整获取。
显式 `pagination=null` 或其他错误类型仍会报错；错误不输出订单或账户内容。
模拟账户选择只在当前 `sim` 对象中保存；下次启动再次调用 `sim.select_account(...)`。
策略仍需自行实现仓位差额、风控、订单跟踪和调度，未自动连接 risk_parity 下单。

## 3. 交易：默认只预览

```python
from decimal import Decimal

preview = client.place_order(
    "US.VOO", qty=Decimal("1"), side="BUY",
    order_type="LIMIT", price=Decimal("600.00"), remark="strategy-preview",
)
assert preview["dry_run"] is True
print(preview)  # 仅 method/path/body/query，无 Authorization 或 Token。

client.modify_order("订单 ID", "US", qty="1", price="599.00")  # 同样仅预览
client.cancel_order("订单 ID", "US")                         # 同样仅预览
```

`dry_run` **不是富途模拟盘**：不会发送任何交易写请求、不会检查实时购买力，
也不会执行交易或返回虚假的订单 ID。账户和行情查询仍访问真实服务器。

确认权限、账户、币种、数量、价格、交易时段、最小价差/整手规则及资金后，
必须同时显式设置两个开关才会发送真实交易指令：

```python
live = FutuClient(
    tokens,
    oauth=oauth,
    account_id=client.account_id,
    dry_run=False,
    allow_live_trading=True,
)
# 以下操作会真实交易。请自行核对后才执行，不要直接运行示例价格。
# result = live.place_order("US.VOO", "1", "BUY", price="自行确认的限价")
# live.modify_order("订单 ID", "US", "1", "自行确认的限价")
# live.cancel_order("订单 ID", "US")
```

`side` 支持 `BUY/SELL/SELL_SHORT/BUY_BACK`。限价单必须给 `price`；
触发单必须给 `aux_price`；`GTD` 必须给未来的 `expire_time`（Unix 微秒）；
市价单只允许 `RTH`。`remark` 最多 64 个 UTF-8 字节。
建议价格和数量用 `str` 或 `Decimal`，避免浮点计算误差。
本地校验不代替券商风控，不会自动融资、计算订单数量或检查全部市场规则。

下单或改单遇到二次确认/网页操作时，会抛出 `OrderConfirmationRequired`，
不会自动接受风险提示、打开网页或重复下单：

```python
from broker import OrderConfirmationRequired

# try:
#     result = live.place_order(...)
# except OrderConfirmationRequired as exc:
#     # 先检查 exc.code、str(exc)、exc.confirm_id、exc.jump_url。
#     # 人工确认提示内容后，才显式调用：
#     # result = live.confirm_order(exc.confirm_id)
#     raise
```

## 4. 刷新、错误及量化接入注意事项

### Protective stops (real client only)

Risk-parity volatility/covariance forecasts control position sizing, not a
guaranteed loss limit. A fixed stop and a trailing stop are separate exit rules.
For an already filled long position, the real client can preview either:

```python
# Example only: 10 filled shares with average entry cost USD 100.
fixed = client.place_order(
    "US.MRNA", "10", "SELL", order_type="STOP",
    aux_price="90", time_in_force="GTC",
)
# Alternative, not an additional order: 10 means 10%, NOT 0.10.
trailing = client.place_trailing_stop("US.MRNA", "10", trail_percent="10")
assert fixed["dry_run"] and trailing["dry_run"]
```

`place_trailing_stop` uses `TRAILING_STOP`, `trail_type="RATIO"`, GTC by default,
and regular trading hours (RTH). General `place_order` also accepts
`trail_type="AMOUNT"` for a currency-distance trailing stop. The broker tracks
the peak after activation; this does not restore peaks before activation.
Verify account/instrument eligibility, validity and session rules with the broker.
Stop-market execution can be below the trigger; GTC does not mean 24-hour coverage.
Previews do not protect a position. Actual writes retain both existing live
trading guards; a submission response alone is not proof of an accepted active stop.

These methods do not automatically attach protection to buys. Before a real
strategy uses them, it must reconcile partial fills, verify protective-order
status, and cancel/resize protection before other sells to avoid duplicate exits.
Do not submit both alternatives for the same shares.
The play-money workflow remains simulation-only; its API supports only
LIMIT/MARKET orders, so its fixed loss check still requires script execution.

从授权响应得到的 `TokenSet` 带有到期时间。传入 `oauth` 且有 refresh token 时，
客户端会在下次查询/真实交易请求前提前 30 秒刷新。刷新后的值保存在 `client.token`。
通过 `SecretServiceStore.client()` 创建的客户端会自动写回密钥库；
直接创建 `FutuClient` 时需自行同步，或传入 `on_token_refresh` 保存回调。
回调失败会阻止业务请求；官方刷新响应可能不返回 refresh token，此时保留原值。
刷新失败会直接报错，不继续发送账户请求。

手工使用 `TokenSet(access_token)` 时无法推断到期时间；可提供 `expires_at`，
或调用 `oauth.refresh(refresh_token)` 取得完整 TokenSet。没有已知到期时间时，
服务器的 401 会直接报错，不自动重放原请求。

- `FutuAPIError`：HTTP/业务/响应协议错误，保留 `code/status/details`。
- `OrderConfirmationRequired`：需要二次确认或网页操作，是 `FutuAPIError` 子类。
- `FutuTransportError`：连接/超时等传输错误。
- `TradingDisabledError`：没有显式开启真实交易。
- `ValueError`：本地参数错误，不发送无效交易请求。

所有请求有超时，**不自动重试**。尤其订单超时不代表没有成交，应先查询订单/
成交确认状态再决定是否重试，避免重复下单。遇到 429 时调用方应控制频率，
对读请求可自行退避重试；不要盲目重放写请求。HTTP 重定向也不会自动跟随。
客户端同步执行、非线程安全；并发任务请各用独立实例并协调 Token 刷新及订单去重。
实盘策略需要另外实现风险限额、时区、汇率、整手处理、幂等及持久化订单跟踪。

现有 risk_parity 策略中的 `VOO/QQQ/VXUS/BIL` 在富途需写作 `US.VOO/US.QQQ/US.VXUS/US.BIL`。
回测目标权重**不能直接视为下单数量**：必须结合实际净资产、价格、可卖持仓、
可用现金、最小交易单位及已有未完成订单计算差额，先预览再决定是否实盘执行。

## 5. 离线测试

离线测试命令见[项目说明](../README.md#离线测试)。测试覆盖 OAuth、两类响应、
自动分页、真实 HTTP method/path/body、精度、异常和所有交易写接口的安全开关。
实盘连通性需要用户授权后的凭证验证，不能由离线测试替代。

## 官方参考

- [模拟账户](https://open.futunn.com/zh-cn/api/sim-trade/account-list)
- [模拟下单](https://open.futunn.com/zh-cn/api/sim-trade/input-order)

- [快速开始 / OAuth](https://open.futunn.com/zh-cn/api/overview/getting-started)
- [授权账户](https://open.futunn.com/zh-cn/api/trading/account/get-accounts)
- [下单](https://open.futunn.com/zh-cn/api/trading/trade/place-order)
- [订单分页](https://open.futunn.com/zh-cn/api/trading/order/get-open-orders)
- [行情快照](https://open.futunn.com/zh-cn/api/quote/realtime/market-snapshot)
- [历史 K 线](https://open.futunn.com/zh-cn/api/quote/basic-data/history-kline)
