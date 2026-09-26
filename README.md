# cline-router 维护手册

> 本机常驻的「一个 Base URL → 多个上游 × 多个模型」路由器。
> 交接文档 · 最后更新：2026-09-25
> 面向：接手维护的 AI（Cline / CodeBuddy 等）或人。

---

## 0. 它解决什么问题

Cline（VS Code 扩展）的 **OpenAI Compatible** provider 只能填**一个** Base URL + **一个** API Key，所以只能挂一家上游。本程序在本机起一个 127.0.0.1 服务，把**多个上游**（火山引擎 Agent Plan、Cline 官方 API、以及后续任意 OpenAI 兼容网关）聚合成**一个** Base URL。

**推荐用法（auto 模式）**：Cline 的 Model ID 固定填 `auto`，实际用哪个模型由路由器侧的「默认模型」决定——在配置面板或菜单栏随时切换、立即生效，全程不用动 Cline。开了 `only_auto` 后 `/v1/models` 只暴露 `auto`，Cline 下拉里就只有它。

```
Cline（Model ID = auto）──> http://127.0.0.1:4000/v1 ──┬──> https://ark.cn-beijing.volces.com/api/plan/v3   (火山 Agent Plan)
                                                       ├──> https://api.cline.bot/api/v1                     (Cline 官方)
                                                       └──> 任意其它 OpenAI 兼容网关（配置里加一个 upstream 即可）
         auto ──按 default_model──> volc-deepseek41（面板/菜单栏可随时切）
```

产物三件套：**命令行控制脚本** + **网页配置面板** + **macOS 菜单栏图标**（含开机自启）。

---

## 1. 文件清单与职责

| 路径 | 职责 |
|---|---|
| `router.py` | **核心**。HTTP 服务：配置加载 / 路由 / 流式透传 / 图像生成 / 配置面板接口 / 热加载 |
| `codebuddy.py` | CodeBuddy 官方协议的适配层（专用请求头 / 强制流式 / SSE→非流式聚合 / 多 Key 轮换） |
| `admin_ui.py` | 配置面板的 HTML+CSS+JS（纯字符串常量，无模板引擎） |
| `volc_fuel.py` | 火山方舟 Agent Plan「燃料」余额查询（AK/SK 签名）+ daemon 定时刷新 + 缓存 |
| `workbuddy_credits.py` | WorkBuddy / CodeBuddy「积分」查询（令牌发现 + 签到）+ daemon 定时刷新 + 缓存 |
| `models.json` | **唯一配置真源**（上游、对话模型、图片模型、端口）。权限 600 |
| `models.json.bak` | 面板每次保存前自动备份的上一版 |
| `selftest.py` | 自检：假上游 + 真路由，验证模型列表/别名改写/密钥注入/UA 透传/SSE 透传/鉴权拦截/监控端点。**必须 PASS** |
| `cline-router.sh` | 控制脚本：`start/stop/restart/status/log/models/test/ui`（launchd 感知） |
| `make_icon.sh` | 把方形 PNG 打包成 macOS `.icns` |
| `images/` | 生成图片的落盘目录（按真实格式 `.jpg/.png/.webp` 命名） |
| `router.log` | 服务日志（含 `OK/FAIL/AUTH/IMG` 打点），launchd 重定向 |
| `bar/Sources/main.swift` | 菜单栏 App：状态项、菜单、动作、CLI 参数 |
| `bar/Sources/RouterService.swift` | 菜单栏 App 的服务层：配置读取/健康检查/launchd 控制/自启管理/拉起路由器 |
| `bar/Sources/Fuel.swift` | 菜单栏「燃料」一行：轮询 `/api/fuel`，展示火山套餐余额 |
| `bar/Sources/Credits.swift` | 菜单栏「积分」一行：轮询 `/api/workbuddy`，展示 WorkBuddy 积分与签到 |
| `bar/build.sh` | 编译打包 `Cline 路由.app`（冻结路由器 + 编译菜单栏 App）：`build / run / install / clean` |
| `router.spec` | pyinstaller 规格：把 `router.py` 冻成单文件可执行 `router` |
| `models.template.json` | **脱敏**配置模板（所有 `api_key` 已清空），首次启动时拷到 AppSupport |
| `make_dmg.sh` | 把 `.app` 打成可分发的 DMG：`bash make_dmg.sh --verify` |

外部产物（不在本目录）：

| 路径 | 说明 |
|---|---|
| `~/Library/LaunchAgents/com.wangcheng.cline-router.plist` | 开机自启：路由服务（launchd 托管，KeepAlive） |
| `~/Library/LaunchAgents/com.wangcheng.cline-router-bar.plist` | 开机自启：菜单栏图标 |
| `~/Cline 路由/Cline 路由.app` | 菜单栏 App（双击=确保服务在跑并打开配置面板）。桌面上有它的 Finder 别名 |
| `bar/build/Cline 路由.app` | 构建产物（自包含：内置冻结的路由器二进制） |
| `bar/dist/Cline 路由.dmg` | 可分发安装包（拖进 Applications 即装） |
| `~/Library/Application Support/ClineRouter/` | **.app 模式的数据目录**：`models.json` / `router.log` / `router.stderr.log` / `images/` |

---

## 2. 日常操作

```bash
bash ~/Cline 路由/cline-router.sh ui        # 打开配置面板
bash ~/Cline 路由/cline-router.sh status    # 查看 launchd/服务状态
bash ~/Cline 路由/cline-router.sh log 60    # 看最近 60 行日志
bash ~/Cline 路由/cline-router.sh models    # 列出全部可用模型 ID
bash ~/Cline 路由/cline-router.sh test      # 跑自检（不需真实 Key）
bash ~/Cline 路由/cline-router.sh restart   # 改代码/改端口后重启
bash ~/Cline 路由/cline-router.sh stop      # 停止并取消开机自启（start 可装回）
```

菜单栏 App 的 CLI（便于脚本/无界面诊断）：

```bash
BIN="$HOME/Cline 路由/Cline 路由.app/Contents/MacOS/ClineRouterBar"
"$BIN" --check          # 状态 + 模型清单 + 最近请求
"$BIN" --restart        # 重启路由服务
"$BIN" --start / --stop
"$BIN" --login-on / --login-off   # 开关开机自启（两个 LaunchAgent 一起）
```

改菜单栏 App 后重新编译安装：

```bash
bash ~/Cline 路由/bar/build.sh install   # 编译并覆盖程序目录里的 App
launchctl kickstart -k gui/$(id -u)/com.wangcheng.cline-router-bar   # 让新版本生效
```

### 2.1 打包 DMG（可分发的安装包）

```bash
bash ~/Cline 路由/bar/build.sh           # 1) 构建 .app（冻结路由器 + 编译菜单栏 App + ad-hoc 签名）
bash ~/Cline 路由/make_dmg.sh --verify   # 2) 打 DMG 并自检（挂载 → 校验结构/签名/模板脱敏 → 卸载）
open ~/Cline 路由/bar/dist               # 3) 产物：bar/dist/Cline 路由.dmg
```

DMG 里只有三样：`Cline 路由.app` + `Applications` 快捷方式 + `安装说明.txt`。用户拖进去即装，无需任何 Python 环境。

首次构建约 30~60 秒（pyinstaller 冻结是大头），之后增量构建约 15 秒。

改了上游密钥后，模板要同步重新生成（**别手改**，容易漏）：

```bash
bash ~/Cline 路由/make_template.sh   # 从 models.json 生成脱敏模板，自带「0 密钥残留」断言
```

### 2.2 两种运行模式（别混淆，这是最容易踩的坑）

| | 源码模式（旧） | .app 模式（新） |
|---|---|---|
| 路由器怎么起 | launchd 跑 `/usr/bin/python3 router.py` | launchd 跑 `.app/Contents/MacOS/router`（冻结二进制） |
| 配置真源 | `~/Cline 路由/models.json` | `~/Library/Application Support/ClineRouter/models.json` |
| 日志 | `~/Cline 路由/router.log` | `~/Library/Application Support/ClineRouter/router.log` |
| 图片 | `~/Cline 路由/images/` | `~/Library/Application Support/ClineRouter/images/` |
| 依赖 Python | 需要 | **不需要**（自带） |

**切换方式**：首次启动 `.app` 时，若 AppSupport 里还没有 `models.json`，会自动从 `~/Cline 路由/models.json` 拷一份过去（**密钥不用重填**）；若旧目录不存在，则用 `.app` 内的脱敏模板初始化。

切换后**两个配置各管各的**——在 App 面板里改的是 AppSupport 那份，源码模式读的还是 `~/Cline 路由/models.json`。想彻底只用一种模式，就别同时维护两份；建议日常用 `.app` 模式，源码目录只留着开发。

要点「开机自启」让 launchd 指向 `.app` 内二进制时，**每次都会整份重写两个 plist**（这样从源码模式切过来时，旧 plist 里写死的 `/usr/bin/python3 <旧路径>/router.py` 会被自动纠正）。

---

## 3. 配置格式（`models.json`）

```jsonc
{
  "host": "127.0.0.1",          // 只监听本机，别改成 0.0.0.0
  "port": 4000,
  "auth_key": "",               // "" = 不校验任何 Key（当前状态）；填值则须与客户端一致
  "default_model": "volc-deepseek41",  // Cline 填 auto 时实际路由到的模型（面板/菜单栏可切）
  "only_auto": true,            // true = /v1/models 只暴露 auto，Cline 下拉里只有它

  "upstreams": {
    "<上游名>": {
      "mode": "openai",                 // "openai"（默认，标准兼容网关）| "codebuddy"（官方协议，见 §5.10）
      "base_url": "https://...",        // 必填，末尾不带 /
      "path": "/chat/completions",      // 对话端点路径；留空按 mode 取默认（codebuddy→/v2/chat/completions）
      "api_key": "sk-...",              // 上游密钥；支持 "${ENV_VAR}" 从环境变量读
      "api_keys": [],                   // 密钥池，一行一把；有值则与 api_key 一起参与轮换（见 §5.10）
      "rotation_count": 1,              // 每 N 次请求换下一把 Key（单 Key 时无意义）
      "timeout": 900,                   // 秒
      "proxy": "",                      // 可选，如 http://127.0.0.1:7890；留空=不走代理
      "user_agent": "",                 // 可选，留空=透传客户端 UA（行为与直连一致）
      "headers": {},                    // 可选，附加请求头（部分网关校验 UA/Referer）
      "unwrap_data": false              // 见 §5.1，非流式响应拆 {"data":{...}} 信封
    }
  },

  "models": [                            // 对话模型：出现在 /v1/models 与 Cline 下拉里
    { "id": "按意-好记的别名", "upstream": "上游名", "model": "上游真实模型名" }
  ],

  "images": [                            // 图片模型：不进 /v1/models，只用于绘图
    { "id": "seedream-pro", "upstream": "volc", "model": "doubao-seedream-5.0-pro",
      "size": "1024x1024", "path": "/images/generations" }
  ]
}
```

**约定**：
- `id` 是 Cline 下拉里看到的名字 → **命名即文档**：`cline-free-*`（实测不花钱）/ `cline-paid-*`（按量计费）/ `volc-*`（火山套餐内）。见 §7。
- `model` 必须是上游真实 ID（大小写、连字符都可能敏感）。
- 对话与图片的 `id` 不能重名。
- **`auto` 是保留名**：请求 `model=auto`（或空 model）时自动路由到 `default_model`；`default_model` 缺省/非法时回退到清单第一个对话模型。
- 面板保存时会校验（缺字段/引用不存在的上游/重名 → 拒绝保存），并先写 `models.json.bak` 再原子替换。

---

## 4. 对外接口契约

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/ui` | 无 | 配置面板（含绘图） |
| GET | `/api/config` | 无 | 读配置（面板用） |
| POST | `/api/config` | 需头 `X-Router-UI: 1` | 保存配置 + 热加载（防跨站） |
| POST | `/api/default` | 需头 `X-Router-UI: 1` | 切换默认模型（`{"id":"volc-kimi-k3"}`），写盘 + 热加载 |
| POST | `/api/test` | 需头 `X-Router-UI: 1` | 测试单个对话模型连通性 |
| POST | `/api/image` | 需头 `X-Router-UI: 1` | 生成图片并把结果**下载落盘** |
| GET | `/images/<文件>` | 无 | 查看已生成图片（仅 basename，防目录穿越） |
| GET | `/health` | 无 | `{ok, uptime_s, config, default_model, models[], images[]}` |
| GET | `/api/fuel` | 无 | 火山 Agent Plan 燃料余额（菜单栏用，只读内存快照，不发网络请求） |
| GET | `/api/workbuddy` | 无 | WorkBuddy / CodeBuddy 积分余额（菜单栏用，只读内存快照） |
| POST | `/api/workbuddy/refresh` | 需头 `X-Router-UI: 1` | 立即刷新积分缓存 |
| POST | `/api/workbuddy/checkin` | 需头 `X-Router-UI: 1` | 每日签到并刷新积分 |
| GET | `/v1/models` | 无 | `only_auto=true` 时**只返回 auto**；否则列全部对话模型（图片模型始终不列） |
| POST | `/v1/chat/completions` | `auth_key` 非空时才校验 | 按 `model` 路由；SSE 逐块透传 |
| POST | `/v1/images/generations` | 同上 | 按 `model` 路由到上游 images 接口 |

鉴权：`Authorization` 支持 `Bearer <key>` 与裸 `<key>`，大小写不敏感；`auth_key` 为空时**全部放行**。
401 会写日志：`AUTH 拒绝：客户端发来 Bearer xxxx…（令牌 N 字符）`（只记录前 4 字符，防泄露）。

---

## 5. 关键实现机制（改代码前必须理解）

### 5.1 `unwrap_data`：非标准响应信封
Cline 官方 API（`api.cline.bot`）的**非流式**响应是 `{"data": {...choices...}, "success": true}`，不是标准 OpenAI 格式；**流式是标准的**。上游开 `unwrap_data: true` 后，路由器在「非流式」请求上走缓冲区模式，检测到 `data` 内含 `choices` 就拆掉外层。**流式请求绕过此逻辑**（本来就是对的，拆了反而坏）。

### 5.2 SSE 透传
上游无 `Content-Length` 时用 **chunked 编码**逐块转发（`_relay`），否则客户端会等不到结束。响应头/状态码原样透传，不做任何改写，因此 tool calling、reasoning_content 等字段天然支持。

### 5.3 请求头策略
- 默认**透传客户端 UA**（`build_headers(up, client_ua)`），使经过路由器与直连行为一致（部分订阅制接口校验 UA）。
- 注入上游 `api_key` 为 `Authorization: Bearer`，并叠加 `headers`（可覆盖 UA）。
- 强制 `Accept-Encoding: identity`（避免 gzip 二次封装）。

### 5.4 图片生成
- 上游返回的是**临时签名 URL**（会过期），因此面板路径（`/api/image`）会把图片**下载到 `images/`**，并按真实字节头决定扩展名（JPEG→.jpg 等），UI 通过 `/images/<文件>` 展示。
- `/v1/images/generations`（外部工具用）则原样返回上游 JSON，不落盘。

### 5.5 配置热加载
面板保存 → 写盘（备份+原子替换）→ `reload_config()` 重建 Config 对象并替换 `Router.config`。**失败时保留旧配置**，不退出进程。手工编辑 `models.json` 后必须 `restart`（服务启动时读一次）。

### 5.6 菜单栏 App
- `.accessory` 策略 + `LSUIElement` → 不占 Dock。
- 单实例保护：启动时若发现同 bundle id 多实例则自动退出。
- 菜单每打开时重建（模型清单/日志尾部都是实时的）。
- 开机自启是**两个 LaunchAgent**：路由服务 + 菜单栏 App，由 App 菜单里的「开机自启」一并开关。
- **拉起路由器的两条路径**：装了开机自启 → `launchctl kickstart`（launchd 有 KeepAlive 托底）；没装 → 直接用 `Process` 拉起 `Contents/MacOS/router --data-dir <AppSupport>`，并把 pid 写进 `router.pid`（停止时按 pid 杀，避免留孤儿进程）。

### 5.7 路由器冻结（pyinstaller）
- `router.py` 只用标准库（`urllib`/`json`/`http.server`），所以冻结很干净：`router.spec` 里只额外声明 `hiddenimports=["admin_ui"]`，并把常见的第三方大块（numpy/playwright/…）排除掉。
- 产物是 **onefile**：单文件 3.7M，内嵌 Python 3.9 运行时，目标机不需要装 Python。
- **启动需要 2~4 秒**（onefile 要先把自己解压到 `/var/folders/...` 再执行）；首次运行新签名的二进制时，macOS 还要做一次代码签名校验，**可能长达 15 秒**。所以双击 App 后菜单栏可能先显示 `⇄ ⏸`，等一会儿会自动变`⇄ 15`——这是正常的，不是没起来。
- 冻结模式下 `sys.stderr` 不一定能传回父进程的管道，所以 `log()` 除了写 stderr，**还会追加一份到 `~/Library/Application Support/ClineRouter/router.log`**，保证任何时候都有日志可看。
- 架构：默认 `native`（本机 Intel → x86_64）。要出通用包（Intel + Apple Silicon 都能跑）：`BUILD_ARCH=universal bash bar/build.sh`。

### 5.8 菜单栏监控（燃料 / 积分）
- 两个模块结构一致：**daemon 线程每 300s 刷新 → 写内存快照 + 磁盘缓存（原子写）**；HTTP 端点只读快照，**永不发网络请求**（菜单栏打开瞬间就有值，不会卡）。
- `/api/fuel`：查火山 Agent Plan 燃料余额，走 `GetAFPUsage` + **AK/SK 的 HMAC-SHA256 V4 签名**（Agent Plan 的 `ark-` API Key 无效）。凭据优先级：`--ak/--sk` > `VOLC_ACCESS_KEY_ID`/`VOLC_SECRET_ACCESS_KEY` > `volc-fuel.json`（数据目录优先，回退程序目录）。
- `/api/workbuddy`：查 WorkBuddy / CodeBuddy 积分。令牌发现顺序：`~/.workbuddy-status/config.json` 的 `accessToken` > 环境变量 `WORKBUDDY_ACCESS_TOKEN` > 桌面端登录目录（`~/.workbuddy/auth`、`~/.codebuddy/auth`）。与 WorkBuddyStatus 小工具共享 `~/.workbuddy-status/` 的令牌指纹偏好与签到标记。
- 无凭据时端点仍返回 **200 + `ok:false`**（附原因文案）而非报错——自检对这两条端点有断言（只要求 200 + 结构正确）。

### 5.9 `mode: "codebuddy"`：CodeBuddy 官方协议
CodeBuddy 官方服务**不是** OpenAI 兼容的，直接用通用 OpenAI 通道打会失败。`mode: "codebuddy"` 的上游走 `codebuddy.py` 适配层，做四件事：
1. **专用请求头**：`Authorization` + `X-API-Key` 双写密钥，并带上 `X-Conversation-ID`/`X-Request-ID`/`X-IDE-Type`/`x-stainless-*` 等一堆必带头；会话类 ID **每次请求现生成**（模拟 CLI 的独立会话，避免被按会话聚合限流）。
2. **强制流式**：上游只认 `stream: true`。客户端要流式 → 直接透传 SSE；客户端要非流式 → 在本层把 SSE 聚合成标准 `chat.completion` 对象（含 content / tool_calls 分片合并 / usage / finish_reason）后再返回。
3. **消息数兜底**：只有 1 条 user 消息时上游会拒，自动补一条最简 system。
4. **多 Key 轮换**：`api_key` + `api_keys[]` 组成密钥池，每 `rotation_count` 次请求换下一把；轮换计数按上游名存内存，**配置热加载不丢**（只有一把 Key 时零开销直接用）。
错误翻译：上游的 `{"code":11102,"msg":...}` 会被转成 OpenAI 形态的 `{"error":{"message":...,"code":...}}`，并把 `displayTips` 里的中文提示拼上去，Cline 里能直接看懂。

### 5.10 连接健壮性（启动与断连）
- 客户端在读请求行前/写响应中途断开（Cline 取消请求、健康探针提前关闭）由 `RouterHTTPServer.handle_error` 降噪：只记一行 `CLIENT-DROP`，不再打整段 Traceback。
- 启动 bind 撞 `EADDRINUSE`（.app 的 launchd KeepAlive 与手动启动抢端口）时**重试 3 次 × 1.5s**（每次重建 server 对象），仍失败则打印占用提示（含 `lsof` 命令）后退出。

---

## 6. 上游档案（重要！踩过的坑都在这）

### 6.1 火山引擎 Agent Plan（`upstreams.volc`）
| 项 | 值 |
|---|---|
| Base URL | `https://ark.cn-beijing.volces.com/api/plan/v3`（**Agent Plan 专属端点**） |
| Key | **Agent Plan 专属 API Key**（形如 `ark-<uuid>-xxxxx`）。官方明确：与「方舟平台 API Key」**不通用**，混用会 401 |
| 可用模型 ID | 小写连字符：`doubao-seed-2.0-mini`、`deepseek-v4.1-flash`、`kimi-k2.7-code`、`kimi-k2.8-preview`、`kimi-k3`、`minimax-m3` |
| 图片 | 同一 Base URL + `/images/generations`；`doubao-seedream-5.0-pro`（1024×1024 可用）、`doubao-seedream-5.0-lite`（**尺寸必须 ≥3686400 像素**，用 2048×2048） |
| 计费 | 套餐内，**不按次收费** |
| 坑 | `/models` 返回 404（不提供列表）；标准端点 `/api/v3` 用这把 Key 会 401；Coding Plan 端点是 `/api/coding/v3`（若账号无订阅会 400 `InvalidSubscription`） |

### 6.2 Cline 官方 API（`upstreams.cline`）
| 项 | 值 |
|---|---|
| Base URL | `https://api.cline.bot/api/v1`，需 `unwrap_data: true` |
| Key | app.cline.bot → Settings → API Keys 新建（`sk_...`） |
| 模型 ID | `provider/model`，如 `deepseek/deepseek-v4.1-flash` |
| 模型清单接口 | **`GET /api/v1/models` 可用**（Bearer 认证），返回全量 458 个 ID，查模型名不用再靠猜 |
| 混元系 | 清单里有 `tencent/hy3`、`tencent/hy3-preview`、`tencent/hy4-preview`、`tencent/hy-mt2-*`、`tencent/hunyuan-a13b-instruct`，但**全部要 Cline Credits**（无 `:free` 版），实测余额不足报 `insufficient_credits` → 想白用混元请走 CodeBuddy 侧 `cb-hy3` |
| 计费 | **看响应里的 `usage.cost`**：为 0 才真免费；非 0 即按量计费（**Cline 客户端里的 FREE 档不适用于 API**，实测客户端 `$0.0000`、API 同模型 `$0.0006+`） |
| 坑 | ① 扩展专属模型（如 `deepseek/deepseek-v4-flash`）用 API 调会 **403 `only available via Cline product surfaces`**；② 官方免费模型多为**推理型**，`max_tokens` 给小了会因"思考吃光 token、正文为空"返回 `500 empty response content`（≥1200 才稳，Cline 里建议 Max Output Tokens ≥8192）；③ 网关对**并发**敏感，同时打多路会出现 SSL reset（`URLError(SSLEOFError)`）；④ 免费档常 429/500（上游限流） |

### 6.3 `upstreams.myapi`
占位上游（`base_url` 是假地址，当前无模型引用它）。加新上游照抄这块结构即可。

### 6.4 CodeBuddy 官方接口（`upstreams.codebuddy`，`mode: "codebuddy"`）
| 项 | 值 |
|---|---|
| Base URL | `https://copilot.tencent.com`（国内版）；海外版是 `https://www.codebuddy.ai`（同一把国内 Key 打海外端点是 401） |
| 路径 | `/v2/chat/completions`（**非标准**，由 codebuddy 适配层自动拼） |
| Key | 控制台生成的 `ck_...`；请求时需 `Authorization: Bearer` 与 `X-API-Key` 同时给（适配层已处理） |
| 可用模型 ID | 实测通过：`glm-5.1`、`glm-5.0`、`glm-5.0-turbo`、`glm-5v-turbo`、`deepseek-v3`、`deepseek-v3.2`、`deepseek-r1`、`kimi-k2.5`、`deepseek-v4.1-flash`、`deepseek-v4-flash`、`deepseek-v4-pro`、**`hy3`**、**`hy3-preview`**、**`hy4-preview`** |
| 实测不可用 | `claude-*`、`gpt-5*`、`gemini-2.5-*`、`o4-mini`、`glm-4.6/4.5`、`deepseek-v4`（裸版本号无此模型）、`deepseek-v4-turbo`、`deepseek-v4.1`、`deepseek-v4.1-pro`、`qwen3-*`、`hunyuan-3/4`、`hunyuan-t1`、`hunyuan-turbos-latest`、**`hy4`**（裸 ID 无）、**`hy3-turbo`/`hy3-pro`/`hy4-flash`/`hy4-turbo`/`hy4-pro`/`hy4-lite`/`hy4-air`** → `11102 service info not found`；`gemini-2.5-pro`、`gpt-5.1` → `only available for authorized users`（没开白名单） |
| 命名坑 | ① deepseek 系 ID **必须带 flash/pro 后缀**（`deepseek-v4.1-flash` ✓，`deepseek-v4.1` ✗）；② 混元系在 CodeBuddy 里叫**短名 `hyN`**，不叫 hunyuan（`hy3` ✓、`hunyuan-3` ✗），且 hy4 **只有 preview 版**（裸 `hy4` 不存在） |
| 计费差异 | 响应体 `usage.credit` 字段可判收费：`hy3` / `hy3-preview` / 全部 cb 系 = **0（含在订阅内，不额外扣分）**；**`hy4-preview` = 0.03~0.07/次（按量扣积分）**，与其它 cb 模型不同，注意别当免费模型跑批量 |
| 计费 | 走 CodeBuddy 账号额度/订阅，**不另按 API 次收费** |
| 坑 | ① **只支持流式**（`stream: false` 会失败，适配层强制 true 再本地聚合）；② `messages` 只有 1 条 user 时会被拒 → 自动补 system；③ 没有 `/v1/models` 列表接口（GET 返回 404），模型 ID 只能靠试；④ 模型不存在时 HTTP **400**（不是 404），错误体是 `{code, msg, displayMsg, displayTips}` 结构 |

> 模型 ID 没有清单接口，上述清单是逐个实测得到的。想扩 model 时按 6.4 的命名加一行再点面板「测试」即可（不可用会得到中文化的 11102 提示）。

---

## 7. 当前模型清单（2026-09-25 快照）

**对话模型 29 个**（`bash cline-router.sh models` 可随时核对）：

| ID | 真实模型 | 计费 |
|---|---|---|
| `cline-free-bunny` | stealth/space-bunny-alpha | 🆓 cost=0 |
| `cline-free-mimo` | xiaomi/mimo-v2.6-flash | 🆓 cost=0 |
| `cline-free-minimax` | minimax/minimax-m3 | 🆓 cost=0 |
| `cline-free-nemotron` | nvidia/nemotron-3-super-120b-a12b:free | 🆓 cost=0 |
| `cline-paid-deepseek41` | deepseek/deepseek-v4.1-flash | 💰 7.5e-05 |
| `cline-paid-deepseek-pro` | deepseek/deepseek-v4-pro | 💰 6e-04 |
| `cline-paid-deepseek-v32` | deepseek/deepseek-v3.2 | 💰 1e-04 |
| `cline-paid-gemini38` | google/gemini-3.8-flash | 💰 3.9e-04 |
| `cline-paid-muse` | meta/muse-spark-1.3-contributor | 💰 1e-04 |
| `volc-deepseek41` | deepseek-v4.1-flash | 📦 套餐内 |
| `volc-doubao-mini` | doubao-seed-2.0-mini | 📦 |
| `volc-kimi-code` | kimi-k2.7-code | 📦 |
| `volc-kimi-k28` | kimi-k2.8-preview | 📦 |
| `volc-kimi-k3` | kimi-k3 | 📦 |
| `volc-minimax` | minimax-m3 | 📦 |
| `cb-glm-51` | glm-5.1 | 🧊 CodeBuddy 额度 |
| `cb-glm-50` | glm-5.0 | 🧊 |
| `cb-glm-50-turbo` | glm-5.0-turbo | 🧊 |
| `cb-glm-5v-turbo` | glm-5v-turbo | 🧊 |
| `cb-deepseek-v3` | deepseek-v3 | 🧊 |
| `cb-deepseek-v32` | deepseek-v3.2 | 🧊 |
| `cb-deepseek-r1` | deepseek-r1 | 🧊 |
| `cb-kimi-k25` | kimi-k2.5 | 🧊 |
| `cb-deepseek-v41-flash` | deepseek-v4.1-flash | 🧊（与 `volc-deepseek41` 同模型、不同额度） |
| `cb-deepseek-v4-flash` | deepseek-v4-flash | 🧊 |
| `cb-deepseek-v4-pro` | deepseek-v4-pro | 🧊 |
| `cb-hy3` | hy3（腾讯混元 3） | 🆓 credit=0（cb 系里唯一确认不扣分的混元） |
| `cb-hy3-preview` | hy3-preview | 🆓 credit=0 |
| `cb-hy4-preview` | hy4-preview（hy4 只有 preview 版） | 💰 credit≈0.03~0.07/次 |

**图片模型 2 个**：`seedream-pro`（1024×1024）、`seedream-lite`（2048×2048）。

> 命名约定：`cline-free-*` 随便用；`cline-paid-*` 用之前知道在花钱；`volc-*` 走已付费套餐（**推荐主力**，同一模型优先用 volc 版，例如 `volc-deepseek41` 对 `cline-paid-deepseek41`）；`cb-*` 走 CodeBuddy 账号额度，不另按次收费。

---

## 8. 改代码的标准流程（必做，缺一步不算完成）

```bash
cd ~/Cline 路由

# 1) 语法检查（Py 文件）
python3 -c "import py_compile; [py_compile.compile(f, doraise=True) for f in ('router.py','admin_ui.py','codebuddy.py')]; print('语法 OK')"

# 2) 配置 JSON 合法性
python3 -c "import json; json.load(open('models.json')); print('JSON OK')"

# 3) 自检（必须 PASS）
python3 selftest.py

# 4) 重启服务
bash cline-router.sh restart

# 5) 真实调用验证（不能只看端口/进程）
bash cline-router.sh models
curl -s -m 60 http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer x" -H 'Content-Type: application/json' \
  -d '{"model":"volc-doubao-mini","messages":[{"role":"user","content":"回两个字：收到"}],"max_tokens":800}'

# 6) 涉及菜单栏 App 时重新编译安装
bash bar/build.sh install && launchctl kickstart -k gui/$(id -u)/com.wangcheng.cline-router-bar
```

**验证标准**：真实发一次请求拿到正常正文；改了面板要打开 `/ui` 看渲染；改了菜单栏要看菜单项。端口监听 / `curl` 200 / 进程存在都**不算**验证通过。

---

## 9. 排错手册

| 现象 | 根因 | 处置 |
|---|---|---|
| Cline 报 `invalid api key` | 客户端 Key 与 `auth_key` 不一致 | 面板口令清空（当前即如此，任意 Key 都过） |
| Cline 报 `未知模型 'x'` | ID 写错或已改名 | 菜单栏 `⇄` →「模型」子菜单点按复制；或直接改用 `auto`（走默认模型） |
| `model=auto` 报"没有任何对话模型可路由" | 清单里没有对话模型 | 面板里先添加对话模型并保存 |
| `HTTP 500 empty response content` | 推理模型 + `max_tokens` 太小 | Cline 里该模型 Max Output Tokens 调 ≥8192 |
| `403 only available via Cline product surfaces` | 插件专属模型 | 只能走 Cline 客户端，无法用 API |
| `401` 打火山 `/api/plan/v3` | 用了平台 Key | 换 Agent Plan 专属 Key |
| `400 InvalidSubscription` | 账号无 Coding Plan 订阅 | 用 Agent Plan 端点 + 专属 Key |
| 图片报 `size must be at least 3686400 pixels` | lite 要求大尺寸 | 尺寸填 2048×2048 |
| 免费模型 `429` / `500` | 上游限流 | 重试或换 `volc-*` |
| `URLError(SSLEOFError/ConnectionReset)` | 上游对并发敏感 | 避免同时多路请求；重试 |
| 服务没起来 / 502 | 端口占用或配置非法 | `launchctl print gui/$(id -u)/com.wangcheng.cline-router`；`tail router.log` |
| 面板报"配置未生效" | 保存的配置校验失败 | 看返回的 errors 文案，修正后重存 |

---

## 10. 禁止事项（会出事的）

1. **不要把任何 API Key 写进代码、文档、日志、Git**。密钥只存在 `models.json`（权限保持 `600`）。
   - 特别地：`models.template.json` 会被打进 DMG 分发给别人，**里面绝不能有任何真实密钥**（生成方式见 §2.1 旁注：所有 `api_key` 置空）。`make_dmg.sh --verify` 会做这道检查，发现残留就报错。
2. **不要改两个 LaunchAgent 的 label**（`com.wangcheng.cline-router` / `com.wangcheng.cline-router-bar`），改名会破坏开机自启与菜单栏控制链路。
3. **不要移除**：菜单栏单实例保护、配置写接口的 `X-Router-UI` 头校验、`/images/` 的 basename 防穿越。
4. **不要把图片模型加进 `/v1/models`**：Cline 会把它当对话模型选，必然报错。
5. **不要监听 `0.0.0.0`**：本服务无强鉴权，必须保持 127.0.0.1。
6. **不要手工编辑 VS Code 的 `state.vscdb`**（会损坏 VS Code 全局状态）。
7. 改 `models.json` 结构时，**同步更新面板的保存/校验逻辑与本文档**。
8. **不要把程序目录移动到 `~/Desktop`、`~/Documents`、`~/Downloads`**。macOS TCC 保护这三个目录，**launchd 启动的进程无法执行其中的文件**：表现为 `launchctl print` 显示 `last exit code = 78: EX_CONFIG`，而手动运行却完全正常（这是 2026-09-25 实际踩过的坑）。程序目录保持在 `~/Cline 路由`；桌面只放 Finder 别名。

---

## 11. 已知限制 / 可扩展点

- **图片模型不能进 Cline 下拉**（协议不同），只能通过面板绘图区或 `/v1/images/generations` 使用。
- **语音模型（TTS/ASR）未接入**：需要新增音频端点转发（可照 `images` 的结构扩展）。
- **Seedream 默认带「AI生成」水印**：上游支持 `watermark: false`，路由器会把未知参数原样透传，但面板尚未暴露该开关。
- **Cline 官方 API 的 FREE 档只对客户端生效**：无法通过 API 免费使用。
- 面板的模型表**没有"计费"列**（目前靠命名体现）；如需要可在 `models` 项里加 `tag` 字段并在 `admin_ui.py` 渲染。
- Cline 官方 API 偶发连接重置（网关侧），无重试逻辑；如需可在 `do_POST` 里加一次自动重试。

---

## 12. 变更历史（关键决策）

| 日期 | 变更 |
|---|---|
| 2026-09-24 | 从零搭建：stdlib-only 路由器（`router.py`）+ 配置模板；自检脚本；菜单栏 App（Swift/AppKit）；LaunchAgent 开机自启；桌面 App + 图标 |
| 2026-09-24 | 接入火山 Agent Plan（`/api/plan/v3` + 专属 Key）；确认 6 个模型全支持 tool calling |
| 2026-09-24 | 发现 Cline 官方 API 非流式响应带 `{"data":...}` 信封 → 增加 `unwrap_data`；接入 Cline 免费档可用模型 |
| 2026-09-24 | 新增 Seedream 图像生成（`/images/generations` + 面板绘图区 + 本地落盘） |
| 2026-09-25 | 修正鉴权语义：`auth_key` 留空 = 不校验（本地自用）；401 日志只记前 4 字符 |
| 2026-09-25 | 按计费实测重命名：`cline-free-*` / `cline-paid-*`；确认 **Cline FREE 档不对 API 生效** |
| 2026-09-25 | **auto 模式**：新增保留名 `auto`（路由到 `default_model`）、`only_auto` 开关（/v1/models 只暴露 auto）、`POST /api/default` 端点；面板加「默认模型」下拉与开关；菜单栏 App 加「默认模型」切换子菜单。Cline 里从此固定只填 auto |
| 2026-09-25 | **打包成可分发的 .app / DMG**：`router.py` 增 `--data-dir`（数据目录可外置）；pyinstaller 把路由器冻成单文件二进制，与 Swift 菜单栏 App 一起打进 `Cline 路由.app`；首次启动自动迁移旧配置到 `~/Library/Application Support/ClineRouter/`；开机自启 plist 改为指向 .app 内二进制并整份重写；新增 `make_dmg.sh`（打 DMG + 挂载自检）。实测：从 `/tmp` 启动 .app → launchd 拉起内置冻结二进制 → 接管 4000 → 真实请求返回正文 ✓ |
| 2026-09-25 | **菜单栏燃料 / 积分监控**：新增 `volc_fuel.py`（Agent Plan 燃料，AK/SK 签名）与 `workbuddy_credits.py`（积分 + 签到）；新增 `/api/fuel`、`/api/workbuddy`、`/api/workbuddy/refresh`、`/api/workbuddy/checkin` 四个端点；菜单栏新增「燃料」「积分」两行；配置面板删除上游时同步统计图片模型引用 |
| 2026-09-26 | **补入腾讯混元 hy 系**：实测 CodeBuddy 上 `hy3` / `hy3-preview` / `hy4-preview` 可用（裸 `hy4` 不存在，混元在 CodeBuddy 里叫 `hyN` 不叫 hunyuan），新增 `cb-hy3` / `cb-hy3-preview` / `cb-hy4-preview`；响应 `usage.credit` 判明 hy3 系不扣分、hy4-preview 按量扣分（0.03~0.07/次）。对话模型 26 → 29。另发现 Cline 侧 `GET /api/v1/models` 可拉全量清单，其中 `tencent/hy*` 全需 Cline Credits |
| 2026-09-26 | **补入 deepseek v4 系**：实测确认 CodeBuddy 有 `deepseek-v4.1-flash` / `deepseek-v4-flash` / `deepseek-v4-pro`（裸 `deepseek-v4.1`、`deepseek-v4-turbo` 均为 11102），新增 `cb-deepseek-v41-flash` / `cb-deepseek-v4-flash` / `cb-deepseek-v4-pro`，对话模型 23 → 26 |
| 2026-09-26 | **接入 CodeBuddy 官方接口**：新增 `codebuddy.py` 适配层与上游 `mode` 字段（openai/codebuddy）；支持多 Key 轮换、流式强制与 SSE→非流式本地聚合、上游 11102 错误中文化；加入实测可用的 8 个 `cb-*` 模型；面板上游卡片新增接口类型/对话路径/密钥池/轮换周期；`router.spec` 把新模块加进 hiddenimports |
| 2026-09-25 | **健壮性打磨**（体检后修复）：`RouterHTTPServer` 重写 `handle_error`（RST/EPIPE 只记一行 `CLIENT-DROP`，消除日志 Traceback 噪音）；启动 bind 撞 `EADDRINUSE` 重试 3 次后清晰退出；自检补 `/api/fuel`、`/api/workbuddy` 用例并用临时 `--data-dir` 隔离缓存 |
