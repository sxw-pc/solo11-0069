# 封页台 / pagefreeze

封页台是一个**完全离线**的本地网站归档工具：用一条命令启动内置合成站点，完成有界抓取、WARC 子集封装、CDX 索引、完整性核验、SQLite 台账和 Markdown/JSON 报告；再用一条命令把归档离线回放出来。运行时只使用 Python 3.10+ 标准库，不需要安装任何第三方依赖。

**是**：可复现的“抓取 → 封装 → 索引 → 回放 → 核验”本地闭环。**不是**：通用爬虫、JS 渲染器、结构化抽取管道、WARC/1.1 全实现或 Web 管理界面。

## 安全与合规（先读）

- **仅允许回环目标**：`127.0.0.1`、`localhost`、`::1`。抓取、演示、回放与绑定地址都在**建立连接之前**校验；非回环目标直接拒绝并写入台账 `rejected_targets`。
- **全部数据为合成数据**：仓库中的站点、页面、URL、域名、公司名、人名、日志均为本地 mock 数据，不对应任何真实公司、真实域名、真实个人或真实业务。
- **不包含敏感值**：没有真实密钥、令牌、凭证、Cookie 或个人数据；占位符统一使用 `REDACTED_DEMO_VALUE`。
- **不做攻击性行为**：不支持登录爆破、Cookie 窃取、UA 池伪造、限流绕过、代理轮换或任何规避检测的能力；限速和 robots 规则是内置的约束而非可绕过的选项。
- **用途**：仅用于归档格式、离线回放和完整性核验的学习、评测与授权测试。

详见 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

## 快速开始

要求：Python 3.10 或更高版本；无需 `pip install`，在仓库根目录直接运行。

```bash
# 1. 一条命令跑通：启动 mock 站点 -> 抓取 -> WARC/CDX -> 核验 -> 台账/报告
python -m pagefreeze demo

# 2. 一条命令跑测试（开发期依赖，零外部网络）
pytest -q

# 3. 对已有归档启动离线回放（默认 127.0.0.1:18081）
python -m pagefreeze replay --archive runtime/demo/archive --port 18081
```

`pytest` 只作为开发期测试依赖；应用运行时零第三方依赖。所有测试均使用本地回环 mock 站点，不访问外部网络。

## 演示输出

`demo` 默认写入 `runtime/demo`，标准输出固定为四行，便于脚本断言：

```text
DEMO_OK records=33 requests=16 responses=15 revisits=1 rejected=1 skipped=3 segments=2 verify=pass
RUN_ID=run-135ca21f4eb157dd
ARCHIVE_SHA256=326396d2835b4eaa766c69623a80bf7d63c35fd4016805946de73fdfa181a4d4
INDEX_SHA256=682f964737c8e6cf75c60ca056a97efcbc19c444a1c83b0c21aa54afebc47645
```

失败时退出码非零，并将稳定错误码输出到 stderr，例如 `ERR CFG_DEPTH_RANGE ...`。

## 产物布局

```text
runtime/demo/
├── archive/
│   ├── seg-00000.warc.gz      # WARC/1.0 子集，固定 gzip 头
│   ├── seg-00001.warc.gz      # 按未压缩字节数分卷
│   └── index.cdx              # CDX 文本索引，偏移指向解压后的 WARC
├── ledger.sqlite3             # runs/fetches/rejected_targets/records/verify_results/resource_refs
├── reports/
│   ├── report.json            # 稳定键序、无绝对路径/系统时间
│   ├── report.md              # 中文人读报告
│   ├── verify.json            # 核验结论与逐条问题定位
│   └── verify.md
└── (archive 与 index 均可脱离运行时独立留存)
```

回放首页：`http://127.0.0.1:18081/`；按 URL 与时间戳取快照：
`http://127.0.0.1:18081/replay?url=http%3A%2F%2F127.0.0.1%3A18080%2Fzh%2F&ts=20240115000000`。
响应附带 `X-Archive-Ref`、`X-Archive-Timestamp`、`X-Archive-Digest`、`X-Archive-File`、`X-Archive-Urlkey` 溯源头。

## 其他子命令

```bash
python -m pagefreeze serve-site              # 单独启动 mock 站点，默认 127.0.0.1:18080
python -m pagefreeze crawl    --now 20240115000000 --out runtime/test
python -m pagefreeze index    --out runtime/demo --url http://127.0.0.1:18080/zh/
python -m pagefreeze verify   --out runtime/demo
python -m pagefreeze report   --out runtime/demo
python -m pagefreeze demo     --config config --now 20240115000000 --out runtime/demo
```

- `--now` 或环境变量 `PAGEFREEZE_CLOCK` 注入固定时钟（14 位 UTC 或 ISO-8601）；`demo` 未指定时默认 `20240115000000`。
- 相同配置与相同时钟重复运行，WARC 卷与 `index.cdx` **逐字节一致**。
- `index` 支持 `--urlkey`、`--from`、`--to` 过滤，输出稳定 JSON。
- 退出码：`0` 成功；`3` 配置错误；`4` 策略/运行错误；`5` 核验失败；`130` 中断。

## 配置

`config/crawl.json` 控制种子、深度、页数上限、并发、请求间隔、超时、响应体上限、MIME 白名单、robots 和 UA；`config/archive.json` 控制归档目录、分卷阈值、摘要算法、索引/台账文件名、报告目录与 UUID 命名空间。配置会做边界与路径逃逸校验，错误码稳定（如 `CFG_DEPTH_RANGE`、`CFG_PATH`、`CFG_UUID`）。

### 静态资源发现（`resource_depth`）

`crawl.resource_depth`（0..10，默认 0）决定是否把页面的静态依赖带入本次归档：

| 值 | 行为 | 归档内容 |
| --- | --- | --- |
| `0` | 关闭资源发现，只抓导航链接指向的页面 | 页面文档（历史行为） |
| `1` | 抓取 HTML 直接引用的资源（`rel=stylesheet`、`img`/`source` 的 `src` 与 `srcset` 候选） | 页面 + 直接样式表/图片 |
| `2` | 额外递归解析已抓 CSS 的 `url(...)` 与 `@import` | 页面 + 样式表 + 字体/背景图等间接依赖 |
| `3+` | 继续展开更深层 CSS，直到导入环或该上限 | 更大的资源集合 |

- 资源引用**不会**占用 `max_depth`（那是页面导航深度），但每一次资源请求都计入 `max_pages`。
- 同一规范化 URL 从多个父资源被引用时只请求、只归档一次；每条引用边仍单独保留，可用 `source_url` 回查。
- CSS 内的相对引用相对**该 CSS 自身**的 URL 解析，绝不相对最初 HTML 页面。
- 外链样式表在连接前被拒绝并记入 `rejected_targets`；`data:`、非法 scheme、无法闭合的坏 token 记为 `skipped`，不中断其余引用。
- 超过 `resource_depth` 的边记为 `skipped`/`resource_depth_limit`；导入环记为 `skipped`/`import_cycle` 且不重复请求。
- 不执行 JavaScript、不抓取 `<script src>`、不改写 HTML/CSS。

每条引用边的 `source_url`、原始 token、解析目标、类型、深度与结果写入台账 `resource_refs` 表，并汇总到 `report.json`/`report.md` 的“静态资源发现”一节。

## 文档

- [docs/FORMAT.md](docs/FORMAT.md)：WARC 子集、CDX 字段、摘要与偏移、revisit 规则、固定 gzip。
- [docs/DESIGN.md](docs/DESIGN.md)：模块分层、抓取状态机、并发与限速、确定性与错误模型。
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md)：回环白名单、合成数据、禁止行为、日志与产物卫生。
- [fixtures/expected/golden.json](fixtures/expected/golden.json)：由 `python scripts/gen_golden.py` 生成，测试只读。

## FAQ

**Q：为什么 `crawl` 抓不到公网页面？**
A：这是设计约束。封页台只归档本地回环站点，非回环目标在连接前即被拒绝并记录。

**Q：为什么两次运行的 `RUN_ID` 一样？**
A：运行 ID 由配置摘要、固定时钟和命名空间确定性派生，用于复现实验，不是随机标识。

**Q：能直接用现有 WARC 工具打开吗？**
A：这是 WARC/1.0 子集，使用标准记录头与 `application/http` 块，但未覆盖全部扩展字段。请按 FORMAT.md 理解子集边界。

**Q：归档中为什么有 `revisit` 而没有第二个响应体？**
A：内容摘要相同的响应会以 `revisit` 记录保存指向首个响应的 `WARC-Refers-To`，减少重复存储；回放时自动跟随。

**Q：`resource_depth` 应该选 1 还是 2？**
A：选 1 得到页面直接依赖的稳定闭包（样式表与图片），预算最可预测；选 2 会继续展开 CSS 中的 `@import`/`url(...)`，补齐字体与背景图等间接依赖，但资源集合更大并占用 `max_pages`。无论选哪个，超出上限的引用都会以 `resource_depth_limit` 留在台账里，而不是静默丢失。

**Q：核验失败如何定位？**
A：`verify.json`/`verify.md` 给出文件名、记录序号与稳定错误码（如 `GZIP_TRUNCATED`、`BLOCK_DIGEST_MISMATCH`、`INDEX_ENTRY_MISSING`）。

**Q：产物为什么没有绝对路径和生成时间？**
A：报告只使用相对文件名、固定时钟与内容摘要，保证可复现和可移植。

**Q：会实现 JS 渲染、分布式抓取、增量合并或 Web UI 吗？**
A：不在当前范围。见 [docs/DESIGN.md](docs/DESIGN.md) 的“范围外”一节。
