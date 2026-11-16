# 归档格式说明

封页台输出 **WARC/1.0 的一个确定性子集**。本文说明写入端与读取端共享的契约；未列出的 WARC 字段不保证兼容。

## 文件命名与分卷

- 归档目录内只有 `seg-NNNNN.warc.gz`（从 `00000` 起连续编号），每个文件是一个独立 gzip 成员。
- 分卷阈值是**解压后 WARC 字节数**（`segment_size_bytes`）。写入时按记录顺序累加，超过阈值先关闭当前卷；单条超阈值记录独占一卷。
- gzip 头由工具自行构造：magic `1f 8b`、method 8、flags 0、mtime 0、XFL 0、OS 255，deflate level 固定为 6。因此同一输入跨平台字节一致。
- 卷内记录首尾相接，没有额外填充；**CDX 的 `offset`/`length` 均指解压后的 WARC 字节区间**，不是 gzip 文件偏移。

## 记录序列化

每条记录为：

```text
WARC/1.0\r\n
Header-Name: value\r\n
...
Content-Length: N\r\n
\r\n
<恰好 N 字节的 block>\r\n
\r\n
```

头部顺序固定为：`WARC-Type`、`WARC-Record-ID`、`WARC-Date`、`WARC-Target-URI`、`WARC-Block-Digest`、`WARC-Payload-Digest`、`WARC-Refers-To`/`WARC-Profile`（仅 revisit）、`Content-Type`、`Content-Length`。日期为 `YYYY-MM-DDTHH:MM:SSZ`，由注入时钟产生。

`WARC-Record-ID` 是 UUIDv5：`uuid5(namespace, f"{urlkey}\n{timestamp14}\n{kind}")`，外层写作 `<urn:uuid:...>`。同一配置、时钟与 URL 集合下稳定可复现。

## 记录类型

| 类型 | block | Payload-Digest | 说明 |
| --- | --- | --- | --- |
| `warcinfo` | `application/warc-fields` 文本 | block 摘要 | 每卷集合首条；含 software、format、config-digest、crawl-window、record-namespace |
| `request` | `application/http; msgtype=request` | 空 body 摘要 | 仅 GET、Host、固定 UA、Accept、Connection；**不含 Cookie/Authorization** |
| `response` | 原始 HTTP 状态行 + 响应头 + body | HTTP body 摘要 | 保留原始头顺序与 body 字节，不解码、不改写 |
| `revisit` | 仅 HTTP 状态行 + 响应头，body 为空 | 被引用响应 body 摘要 | 含 `WARC-Refers-To` 与 `WARC-Profile: identical-payload-digest` |

每个抓取命中固定产生 request +（response 或 revisit）。warcinfo 只出现一次，位于整个 WARC 序列首条。

`Content-Encoding: gzip` 的响应体按线上压缩字节写入 response 记录，`Content-Encoding` 头原样保留；`WARC-Payload-Digest` 是对**压缩字节**的摘要，因此归档与回放都不依赖解码结果。链接发现使用解压后的完整文档，但不会改写记录。

## 链接发现跳过

已归档的 HTML 若因 gzip 实体不完整而放弃本轮链接发现，不新增 WARC 记录，也不把响应标成失败；跳过事件写入台账 `link_discovery_skips`，并在报告 `crawl.link_discovery_skipped` / `crawl.link_skip_reasons` 中单独计数。`fetches` 表中该响应仍是 `archived`，`reason` 为稳定的 `link_discovery_skip`。detail 取值：`GZIP_TRUNCATED`、`GZIP_CRC_MISMATCH`、`GZIP_ISIZE_MISMATCH`、`GZIP_INVALID`、`GZIP_TOO_LARGE`。按“完整文档才扩展”语义，一个坏实体贡献零个链接。

## 去重与 revisit

写入前按 `(urlkey, url)` 排序。对每条响应 body 计算摘要，摘要首次出现时写 response；后续相同摘要写 revisit，`WARC-Refers-To` 指向首条 response 的 `WARC-Record-ID`，`WARC-Payload-Digest` 与目标一致。回放读取 revisit 时会解析目标记录并返回其响应体。

## CDX 索引

`index.cdx` 是 UTF-8、空行可忽略的文本，每行 9 个以单个空格分隔的字段：

```text
urlkey timestamp url status mime digest offset length filename
```

- `urlkey`：SURT 风格，如 `http://1,0,0,127:18080)/zh/`；端口在主机后、`)/` 前。
- `timestamp`：14 位 UTC；CDX 行按 `(urlkey, timestamp, url)` 排序，相同三元组保留首行。
- `url`：规范化 URL（scheme/host 小写、去默认端口、去 fragment）。
- `digest`：`sha256:<hex>`，即 `WARC-Payload-Digest`。
- `offset`/`length`：解压后记录字节偏移与长度；`length` 包含记录头、CRLF 分隔与结尾空行。
- `filename`：该记录所在卷。索引只包含 response/revisit，不包含 warcinfo/request。

查询语义：给定 URL 与 `ts`，选择同一 urlkey 下 `timestamp <= ts` 的最新一条；没有则返回无快照。

## 报告与台账

`report.json`、`verify.json` 使用固定键序和相对文件名，不含绝对路径、主机系统时间或随机值。`ledger.sqlite3` 使用 DELETE journal，包含 `runs`、`fetches`、`rejected_targets`、`records`、`verify_results` 五表；同一 `run_id` 重写会先清理旧行。

## 兼容边界

不写入 WARC 扩展字段、不保证 HTTP/2/3、不记录 TLS 元数据、不展开压缩响应体、不支持 WARC 记录级压缩之外的打包格式。读取端对截断、尾随数据和格式错误给出稳定错误码，而不是静默修复。
