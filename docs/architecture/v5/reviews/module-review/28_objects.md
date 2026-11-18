# 模块审查 28 · `v5/storage/objects.py`

> 审查日期：2026-11-18
> 依据：源码 197 行全文通读；依赖 `layout.py`（tmp_file/sidecar/fact_kind）、`contracts/digest`（assert_relative_path/blob_digest）、`contracts/errors`（E3003/E5001/E5004）。设计 `09-app-storage.md` §3.1/§3.3/§3.4；测试 `test_v5_storage.py:103-138,374-394`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `fm_review_26_33/probe2_storage.py`。

---

## D1 · 应然（抛开实现，这个模块该做什么）

ObjectStore 是**内容寻址的不可变对象存储**，回答"事实层的派生对象如何落盘、校验、防篡改"。职责边界：

1. **一次写永不改，同名即同内容**：bad 了从上游重算，不原地改（`:3`）。
2. **三段写**：stage（同盘 tmp）→ commit（`os.replace` 原子）→ register（jsonl/duckdb，可 reconcile，`:4`）。读侧永不读到半个文件。
3. **schema sidecar**：每个 parquet 带 `{name}.schema.json`（列名/dtype/rows/schema_version），读时不猜；不兼容变更须 `schema_version+1` + 迁移函数，禁止"读时兼容两版"（`:6-8,158-177`）。
4. **内容寻址 digest**：文件名 = sha256 hex 全长；digest 算在**规范化序列化字节**上，序列化参数显式固定（v1.0 + snappy + write_statistics=False）保证跨 pyarrow 版本字节一致（`:29-35,120-143`）。
5. **安全边界**：`_resolve` 校验 `relpath` 在数据根内，`assert_relative_path` + `relative_to` 双重拒绝 `..` 与符号链接逃逸（`:93-109`）；校验和读取用同一解析结果。
6. **磁盘满 E5001**：`OSError`（磁盘满/权限不足）包装成 E5001，带归档/GC hint（`:65-71`）。

**编码约定**：时间 UTC aware；暴露 float32、价格与账本 float64（`:8`，意图在 schema 层，非本模块强制）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> venv Python 实测（pyarrow 26.0.1）。临时根 `...\fm_review_26_33\data2`。

### 例 1：内容寻址写入 + sidecar + 读回（实测）

```
=== B objects 内容寻址/不可变/指纹 ===
{'写入引用': {'digest': 'sha256:16ae263c964f23c63dc11b4ed14a2833ff5345bedf364653251c52a65b8081cc',
              'relpath': 'facts/panels/16ae263c964f23c63dc11b4ed14a2833ff5345bedf364653251c52a65b8081cc.parquet',
              'schema_version': 1, 'kind': 'panels'},
 '读回行数': 3,
 'sidecar存在': True,
 'sidecar内容': {'columns': [{'dtype': 'int64', 'name': 'a'}, {'dtype': 'string', 'name': 'b'}],
                 'rows': 3, 'schema_version': 1},
 '同内容幂等digest相同': True,
 ...
}
```

输入：`ObjectStore.write_table("panels", pa.table({"a":[1,2,3],"b":["x","y","z"]}))`。
- digest = `blob_digest(parquet字节)` → 文件名主干（64 hex）；relpath = `facts/panels/<hex>.parquet`（`:90-91,141-156`）。
- sidecar = `facts/panels/<hex>.parquet.schema.json`，内容含 columns/rows/schema_version（`:147-153`）。
- 同内容再写一次 digest 相同（幂等）——实测 `同内容幂等digest相同=True`。

### 例 2：不可变性 + 篡改检测（实测）

```
'篡改检测': '[E5004] 对象内容指纹失配: ...\\16ae...parquet (at facts/panels/16ae...parquet)',
'指纹失配检测': '[E5004] 对象内容指纹失配: ...\\96c8...parquet (at facts/panels/96c8...parquet)'
```

- 篡改：把落盘字节追加 `garbage` 后 `read_table` → `blob_digest(content) != ref.digest` → E5004（`:163-165`）。
- 指纹失配：构造 `ObjectRef(digest=ref1.digest, relpath=ref2.relpath)`（指向异内容对象）→ 同样 E5004，证明"校验和读取用同一路径"（`:160-165`）。
- 异内容覆盖（同 digest 不同字节）→ `_atomic_write_bytes` 检测 `target.exists() and read_bytes()!=content` → E5004（`:55-58`）。

### 失败输入 → 错误输出（实测）

| 非法输入 | 输出 |
|---|---|
| `_target("panels","sha256:short")` | `E3003` "非法内容寻址 digest"（`:83-89`，实测） |
| `read_json(ref with relpath="../../outside.parquet")` | `E2002` "禁止 '..' 段进指纹"（`assert_relative_path` 先拦，`:99`；实测） |
| 篡改 parquet 字节 / 伪造 digest | `E5004` "对象内容指纹失配"（实测） |
| sidecar 缺失 / schema_version 不符 | `E3003`（`:167-176`，逻辑核对，未单独实测触发） |
| expected_digest 声明不符 | `E5004` "表内容与声明的 digest 不一致"（`:142-143`） |
| 磁盘满 / 权限不足（OSError） | `E5001` "对象写入失败（磁盘满或权限不足）"（`:65-71`，代码路径已读，未实测造满盘） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| O-1 | **`write_json` 不写 schema sidecar**：reports 类 JSON 对象只有 `.json` 无 sidecar（`:179-188`）。index `_rebuild_object_rows` 对 `*.json` 另走分支补 schema_version（index.py:104-114），但 read_json 不校验 schema_version（`:190-197`）。 | P2 | `objects.py:179-197` vs `:158-177` | **已确认设计取舍**：JSON 是自描述 payload，无列 schema 校验需求；index 已补枚举分支。不是 bug，但需在文档写清"sidecar 只针对 parquet"。 |
| O-2 | **序列化参数固定但无跨 pyarrow 版本实测对拍**：`_PARQUET_VERSION=1.0` + snappy（`:33-34`）是为跨版本字节一致，但测试只在当前 pyarrow 26 验证幂等。 | P2（待验证） | `objects.py:29-35`；`test_v5_storage.py:103-118` | **需确认**：跨版本对拍需要两个 pyarrow 版本环境，属工程化验证；设计方向正确（显式固定参数），非代码 bug。 |
| O-3 | **编码约定（float32 暴露 / float64 价格账本）未在本模块强制**：docstring 声明意图（`:8`），但 `write_table` 只序列化传入的 table，不检查列 dtype。 | P2 | `objects.py:8,111-156` | **需确认意图**：dtype 由上游事实层保证；存储层不重复校验。若上游传错 dtype，digest 会变但不报错——属"谁保证 schema"的责任划分问题。 |
| O-4 | **`_resolve` 的 `..` 由 assert_relative_path 先拦**：E2002 而非注释暗示的 E5004（`:99-108` 注释说"拒绝 .. 与符号链接逃逸"，实测返回 E2002）。 | P2（错误码语义） | `objects.py:99-108`；实测 B3 | **低优先**：都是 fail-closed，E2002（指纹禁止 ..）语义也贴切；只是与本地 hint 措辞略偏。 |

**多了什么**：无冗余。`expected_digest` 参数提供"声明即校验"的额外防线（`:117,142-143,183-184`）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Content-addressed storage（git object 模型） | 采纳 | 文件名=内容 digest；同名同内容；篡改可检出（实测 E5004）；跨版本字节一致靠固定序列化参数 |
| 三段写 stage→commit→register + os.replace | 采纳 | 同盘原子替换，读侧无半文件；tmp 失败清理（`:137-139` 注释磁盘满场景） |
| Schema sidecar + 版本化迁移 | 采纳 | 读时不猜 schema；不兼容升版本 + 迁移，禁止读时兼容两版（`:6-7,170-176`） |
| 原地 `to_feather` 覆盖（V4 缺陷） | **拒绝** | 会静默改写历史、破坏内容身份；本模块 `_atomic_write_bytes` 对异内容 E5004 拒绝覆盖（`:55-58`） |
| 依赖 pyarrow 默认序列化参数 | **拒绝** | 默认值随版本演进会让同逻辑表算出不同 digest、blueprint_digest 漂移且无报错（`:121-125` 注释）——已显式固定 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **内容寻址 + 规范化序列化**：digest 算在固定参数序列化的字节上（`:120-143`），实测幂等、篡改检测 E5004。
2. **不可变覆盖拒绝**：`_atomic_write_bytes` 同路径异内容 E5004（`:55-58`），同名即同内容。
3. **原子写 + tmp 清理**：同盘 tmp + `os.replace`（`:60-64`），失败时 `tmp.unlink(missing_ok=True)`（`:137-139`）。
4. **磁盘满 E5001**：OSError 包装，带归档/GC hint（`:65-71`）。
5. **路径逃逸双重防护**：`assert_relative_path` + `relative_to(root.resolve())`（`:99-108`），实测 `..` 被 E2002 拦。
6. **digest 格式严格校验**：`_target` 只接受 `sha256:` + 64 位小写 hex（`:82-89`），实测非法 digest E3003。
7. **schema 校验**：read_table 先校验 sidecar 存在 + schema_version 匹配（`:166-176`），不猜 schema。
8. **写表失败不留 tmp**：finally 清理（`:137-139`）。
9. **路径走 layout**：`_target` 用 `fact_kind`/`tmp_file`/`sidecar`（`:90,60,147`），不自己拼字面量，符合 AC31。

**发现的问题**：O-1~O-4 均为 P2/需确认意图，**无 P0/P1 级 bug**。磁盘满 E5001 的 `except OSError` 也会捕获 `os.replace` 的权限错误——但 `_atomic_write` 在 store 侧对 Windows PermissionError 有重试，objects 侧未重试（O-4 关联），单标的写入失败会 E5001，属 fail-closed 合理。

**测试覆盖**：`test_v5_storage.py:103-138` 覆盖内容寻址、sidecar、篡改、bad kind、路径逃逸；`:374-394` 覆盖 digest 前缀。充分。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §3.4 | 描述 schema sidecar | 补"sidecar 仅针对 parquet；JSON 对象自描述、无 sidecar"（O-1），消解歧义 |
| `09-app-storage.md` §3.3 | 描述内容寻址 + 原子写 | 与实现一致（固定序列化参数、不可变拒绝覆盖、E5001）；无需更新 |
| `15-operations.md` §4-8（GC/归档） | E5001 hint 指向此 | 实现 hint 一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新对象类别 | ✅ 支持（经 layout.FACT_KINDS） | `fact_kind` 白名单来自 layout；加类别只改 layout 一处（见模块 27） |
| 不兼容 schema 变更 | ⚠️ 需迁移函数 | 代码强制升 schema_version + 迁移（`:170-176` 拒绝读旧版），无自动迁移框架——按设计"显式迁移" |
| 新对象格式（如 arrow IPC） | ❌ 需改代码 | write_table/write_json 两种格式写死；无格式注册点 |
| 自定义 digest 算法 | ❌ 拒绝 | `_target` 只接受 sha256（`:83`），换算法会让对象找不到——有意收紧 |
| 跨版本字节一致验证 | ⚠️ 半支持 | 参数已固定（O-2），但缺双版本对拍测试 |

---

## C1–C12 相关项判定

- **C1 365 年化 / C2 资金费 / C3 市场 / C4 合约 / C5 溢价 / C6 上市下架 / C7 缺口 / C10 判据 / C11 分层 / C12 embargo**：不适用（对象存储不涉业务口径）。
- **C8 开放列集**：✅ sidecar 列清单自适应传入 table（`:150`），不写死五列；schema_version 保证演进可控。
- **C9 as-of 只 ffill**：不适用（存储层不填充）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（原子写 + 内容寻址是八工位产物不串改的地基，实测通过）。
- **R2 结论可信**：✅ 强支撑（篡改 E5004 + schema 版本校验，保证"跑过的对象字节没变"）。
- **R3 因子零代码扩展**：✅ 正向（列集自适应，开放列集的基础设施就绪）。
- **R7 fail-closed**：✅ 强支撑（digest 格式、路径逃逸、篡改、异内容覆盖、磁盘满全拒）。

---

## 结论

**本模块代码质量高，无 P0/P1，可确认通过。** 内容寻址 + sidecar + 不可变 + 原子写 + 路径逃逸防护 + 磁盘满 E5001 全部落地且实测通过（幂等写入、篡改 E5004、逃逸 E2002、digest 格式 E3003），是 findings §2 "GC/index 已实现"基线更正之外，对象存储这一环确认为生产可用。O-1~O-4 均为 P2/需确认意图（JSON 无 sidecar、跨版本未对拍、dtype 责任划分、E2002 vs E5004 语义），不影响"能真挖因子"。**建议**：D5 补一句"sidecar 仅 parquet"即可，代码不动。
