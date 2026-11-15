# 设计说明

## 目标与非目标

目标：在完全离线、零运行时依赖的约束下，用一条命令完成“合成站点 → 有界抓取 → WARC 子集 → CDX → 核验 → 台账/报告 → 离线回放”的闭环，并保证固定时钟下可复现。

非目标：JavaScript 渲染、完整 WARC/1.1、分布式抓取、增量合并、正文抽取或主数据、Web UI、OCR、登录/会话、代理池或任何规避限流的能力。

## 模块分层

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 入口 | `__main__`、`cli` | argparse 子命令、稳定退出码与错误码、stdout 摘要 |
| 配置 | `config` | 两文件 schema 校验、范围检查、路径逃逸阻断、配置摘要 |
| 策略 | `policy` | URL 规范化、SURT urlkey、回环白名单、robots 解析 |
| 抓取 | `frontier`、`ratelimit`、`fetch`、`links`、`css`、`crawl`、`crawl_audit` | 有界 BFS、并发、限速、超时/体积/MIME 处理、资源发现与引用审计 |
| 归档 | `warc`、`warc_build`、`store`、`archive_io` | 记录构造/解析、排序去重、分卷、容错读取 |
| 索引 | `cdx` | 从记录生成、排序去重、稳定写读、lookup/window |
| 核验 | `verify`、`verify_report` | 结构/截断/摘要/索引/revisit 交叉检查与报告 |
| 回放 | `replay` | 只读索引与归档，按 URL+时间戳返回快照 |
| 记录 | `ledger`、`report` | SQLite 台账、确定性 JSON/Markdown 报告 |
| 摘要 | `digest` | `sha256:<hex>`、文件流式摘要、多文件联合指纹 |
| 编排 | `pipeline` | 启动 mock 站点、抓取、收尾、运行 ID |
| 合成站点 | `mocksite` | 确定性内容与固定时钟 HTTP 响应 |

为满足单文件 ≤300 行：`warc` 只保留序列化/解析，构造器拆到 `warc_build`；`verify` 只做检查，报告序列化拆到 `verify_report`；摘要与文件指纹独立到 `digest`。

## 模块职责图与数据流

```text
cli ─► config/policy ─► pipeline ─► crawl ─► store/warc ─► cdx
                          │             │                    │
                          └─► mocksite  └─► verify ◄──────────┘
                                              │
                                     ledger + report
replay ◄─ cdx + archive_io（按 URL+时间戳定位单条记录）
```

数据流：CLI 先校验配置与策略，pipeline 启动 mock 站点并调用 crawl；抓取结果只保留原始响应，store 统一排序、去重、分卷，cdx 从记录生成索引，verify 交叉核验后写台账与报告。回放不依赖网络与台账，只读归档和索引。

## 抓取状态机

1. 校验种子为回环 HTTP URL；若有 `respect_robots`，先预取每个种子的 `/robots.txt`。
2. 种子按顺序进入 FIFO frontier，深度 0；记录已处理集合，避免重复。
3. 工作线程取任务，`RateLimiter` 在进程级按最小间隔串行发放请求许可。
4. `fetch` 在**连接前**再次检查 DNS/主机文本是否属于回环白名单；使用不自动跟随重定向的 opener，逐跳人工处理，且每一跳都重新校验。
5. 响应按上限流式读取；超限关闭连接并记为 `body_too_large`；超时记为 `timeout`；MIME 不在白名单记为 `mime_skipped`；robots 禁止记为 `robots_disallowed`。
6. 4xx/5xx 仍作为可归档响应保留，不视为抓取器故障。
7. 仅对 HTML 提取 `<a href>`，解析相对链接并规范化；非回环外链进入 `rejected`，在连接前拒绝并写入台账。
8. 达到 `max_pages` 或 frontier 为空时停止；每个 URL 只归档一次。

### 静态资源发现

导航链接与资源引用是两条独立通道，共享同一套 URL 规范化、回环白名单、robots 与预算检查：

- HTML 资源解析器（`links`）只识别 `link[rel~=stylesheet]` 的 href、`img`/`source` 的 `src`，以及 `srcset` 候选列表；`srcset` 按候选逗号切分，括号/引号内的逗号与 `data:` 内的逗号不切分，描述符（`1x`/`100w`）不会当作 URL。
- CSS 解析器（`css`）是保守的文本扫描器：先屏蔽注释（保留字符串内的注释字样），再读取 `url(...)` 与 `@import`，处理引号、括号、转义与 `data:`；字符串里的 `url` 字样不产生引用。坏 token（未闭合字符串/括号）记一次 `skipped` 并从声明边界继续。
- 用 `resource_depth` 选择策略：`0` 关闭、`1` 只抓 HTML 直连、`2+` 递归展开 CSS。HTML 直接引用记资源深度 1，CSS 内部引用逐层 +1；超过上限的边记为 `skipped`/`resource_depth_limit`。
- CSS 内部引用以**该 CSS 自身 URL** 为基准，绝不回退到最初 HTML 页面。`@import` 环用祖先链判定，命中时记为 `skipped`/`import_cycle`，且不会再次请求同一资源。
- 资源 URL 只入队一次；重复引用保留为 `duplicate` 边，因此“一个目标一次请求”和“所有来源可审计”同时成立。
- 资源请求不增加页面导航深度，但计入 `max_pages`，因此资源发现不会突破预算。

每条边的 `source_url`、原始 token、解析目标、类型、深度、`outcome`、`reason`、`duplicate`、`cycle` 汇总到 `CrawlResult.resources`，写入台账 `resource_refs` 表，并在报告中按类型与结果计数。归档仍按 `(urlkey, url)` 排序去重，所以资源发现不改变确定性来源。

并发只影响抓取完成顺序。归档写入前按 `(urlkey, url)` 重新排序，因此线程调度不改变 WARC/CDX 字节。

## 确定性的来源

- 注入时钟：`--now` > `PAGEFREEZE_CLOCK` > demo 默认值 > 当前时间；WARC-Date、HTTP Date 与所有 14 位时间戳均由它派生。
- 内容：mock 站点不读取随机数、不依赖真实时间或环境。
- ID：UUIDv5 由 namespace + urlkey + 时间戳 + 记录类型派生。
- 压缩：自定义 gzip 头与固定 deflate level、固定 mtime。
- 排序：URL 集合、CDX 行、报告键均有显式稳定顺序。
- 报告：只写相对文件名与内容摘要，不写绝对路径或 wall clock。

因此同一配置、同一时钟的两次运行，WARC 分卷与 `index.cdx` 逐字节一致。

## 有界资源

深度、页数、并发、请求间隔、超时、body 上限、MIME 白名单和分卷阈值全部来自配置并有硬边界。抓取池使用固定大小线程池；响应读取有字节上限；归档全量解析用于核验，规模由页数与 body 上限共同约束。

## 错误模型与退出码

所有可预期错误继承 `PagefreezeError(code, message)`，CLI 统一输出 `ERR <CODE> <message>`。配置错误退出 3；策略/运行错误退出 4；核验失败退出 5；中断 130。错误码是稳定字符串，测试据此断言，不解析人类描述。

核验不是“读得动就算过”：它重算 block/payload 摘要与 Content-Length，逐行核对 CDX 与记录位置、URL、时间戳、状态、MIME、摘要，并检查 revisit 目标存在、摘要一致、profile 合法。损坏定位到文件与记录序号。

## 回放

回放服务是只读的：启动时扫描归档、加载索引摘要；请求时按 URL+ts 查找 CDX 行，定位卷与解压偏移，解析单条记录。revisit 会沿 `WARC-Refers-To` 取回原响应体。响应保留原始状态与头，并附加 `X-Archive-*` 溯源。错误分支稳定：参数非法 400、无快照 404、索引/归档损坏 500。

## 取舍

- 使用全量内存记录换取简单、可核验的确定性；未做流式大归档。
- gzip 每卷一个成员，牺牲随机读性能换取可移植与可复现。
- 只实现 HTTP GET 与 robots 的常见子集，不实现完整 RFC 语义。
- 数据库仅作运行台账，不作为归档主数据或增量合并依据。
