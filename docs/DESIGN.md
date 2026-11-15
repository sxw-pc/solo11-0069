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
| 抓取 | `clock`、`frontier`、`ratelimit`、`fetch`、`links`、`crawl` | 有界 BFS、按 origin 限速、并发、超时/体积/MIME 处理 |
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

1. 校验种子为回环 HTTP URL；首次接触某个 origin 时先预取该 origin 的 `/robots.txt`，解析 Allow/Disallow 与所选 user-agent 组的 Crawl-delay 后才调度该 origin 的页面，不用首个种子的规则覆盖其它域名。
2. 种子按顺序进入该 origin 的 FIFO frontier，深度 0；全局已处理集合避免重复。
3. 调度线程为每个 origin 维护独立限速器，按各 origin 的 next-allowed-at 只派发已到期任务（方案 B）：同 origin 相邻请求起始时间至少间隔 `max(request_interval_ms, robots Crawl-delay)`，不同 origin 的等待槽互不占用；长 delay 站点等待期间线程继续服务其它 origin。
4. `fetch` 在**连接前**再次检查 DNS/主机文本是否属于回环白名单；使用不自动跟随重定向的 opener，逐跳人工处理，每一跳都重新过白名单，并按最终请求 origin 的 robots 与限速规则执行。
5. 响应按上限流式读取；超限关闭连接并记为 `body_too_large`；超时记为 `timeout`；MIME 不在白名单记为 `mime_skipped`；robots 禁止记为 `robots_disallowed`，不建立连接。
6. 4xx/5xx 仍作为可归档响应保留，不视为抓取器故障。
7. 仅对 HTML 提取 `<a href>`，解析相对链接并规范化；非回环外链进入 `rejected`，在连接前拒绝并写入台账。
8. 达到 `max_pages`（跨 origin 共享预算）或全部 frontier 为空时停止；每个 URL 只归档一次。

并发只影响抓取完成顺序。归档写入前按 `(urlkey, url)` 重新排序，因此线程调度不改变 WARC/CDX 字节。限速等待由调度线程在派发前完成，工作线程只做网络 I/O；注入 `VirtualClock` 后可在零墙钟耗时下验证任意长的 Crawl-delay。

### Crawl-delay 的解析与容错

- 仅接受所选 user-agent 匹配组中的非负整数秒；小数（含科学计数法）记 `crawl_delay_decimal_unsupported`，负数记 `crawl_delay_negative`，其它非法 token 记 `crawl_delay_invalid`，空值记 `crawl_delay_empty`，均忽略该条并保留告警。
- 同组重复且取值冲突记 `crawl_delay_conflict`，最后一条有效值生效；重复同值不告警。
- 坏规则只影响 Crawl-delay 字段，绝不破坏同文件的 Allow/Disallow 判定，也不影响其它 origin 的采集。
- robots.txt 抓取失败或非 200 时对该 origin 失败放行（无额外规则），错误仍进入 crawl log。

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
