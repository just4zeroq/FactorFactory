# 模块审查 37 · `v5/runtime/envelope.py`

> 审查日期：2026-11-18
> 依据：源码 161 行全文通读；依赖 `contracts/digest.py`（environment_digest）、`contracts/errors.py`（E2004/E5002）。设计 `s0/03-runtime.md` §7（环境快照）、`16-security.md`（凭据禁令）。测试 `test_v5_storage.py:225-250,400-405`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_u4.py`（E 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Envelope 是**环境快照**："哪些环境事实值得记、哪些绝不能记"的边界（`:1-11`）：

1. **可记录但不进 parcel_digest**：换机器、升级无关依赖不该让历史产物失效。`digest` 属性只用于记录与跨环境比对（`:64-72`），明确不进 parcel_digest。
2. **三档跨环境处理**（`compare`，`:74-82`）：同环境完全可复现 / 跨环境产物相同记 `env_mismatch` / 跨环境产物不同再记 `reproduction_failed`，但**仍不判失败**——环境差异是事实，不是结论错误。
3. **凭据三条禁令（硬，`:7-10`）**：
   - 值绝不出现在 Blueprint / Parcel / 日志 / trace / 报告 / 指纹任何一处；
   - 取值失败必须 E5002，不得回退匿名或只读；
   - 只存引用名（环境变量名 / 密钥别名），因为引用名不泄露值。
4. **信封字段**（`to_dict`，`:48-62`，12 个键）：python_version、package_versions（排序）、platform、machine、cpu_count、blas_threads、clock_basis、rng_impl、resource_limits、device、data_root_resolved、credential_refs（排序）。

**引用名识别**（`:26-30`）：`_ENV_VAR_NAME = ^[A-Z][A-Z0-9_]{0,63}$`（全大写环境变量名）。注释明确这条比"含 = 或长度>128"可靠——`sk_live_ABCDEF…` 两个条件都不满足，所以靠全大写形态拦截传值的误用。

**参数分两路**：`build_envelope` 的 `credential_env_names` 只登记引用名（不读值，`:119-127,128-141`）；`require_credentials` 才取值并原样返回给调用方使用（`:144-161`），但调用方不得写入任何产物。取值失败（缺变量/空值）即 E5002（实测）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测，复现脚本 `probe_u4.py` E 段。

### 例 1：build_envelope 字段 + 只存引用名（真实输出）

输入：`build_envelope(ROOT, packages={"duckdb":"2.0.0"}, credential_env_names={"exchange":"BINANCE_API_KEY"})`
```
to_dict_keys = ['blas_threads','clock_basis','cpu_count','credential_refs','data_root_resolved',
                'device','machine','package_versions','platform','python_version',
                'resource_limits','rng_impl']
credential_refs = {'exchange': 'BINANCE_API_KEY'}     # 只有引用名，无值
clock_basis = 'utc'
digest = sha256:222426c3ecc9ea180...
```
读法：`credential_refs` 存的是环境变量名 `BINANCE_API_KEY`，不是它的值；`python_version=3.12.1`（本机 venv 真值）。

### 例 2：凭据引用名必须是环境变量名（真实输出）

| 误传值 | 输出 |
|---|---|
| `credential_env_names={"exchange":"sk_live_ABCDEF"}` | `E2004\|凭据引用必须是环境变量名（全大写）: exchange='sk_live_ABCDEF'` |
| `credential_env_names={"exchange":"binance_key"}`（小写） | `E2004\|凭据引用必须是环境变量名（全大写）: exchange='binance_key'` |

读法：把真实密钥值或小写名误传进来会被拦，防随快照落盘（禁令 1）。

### 例 3：require_credentials 缺失硬失败（真实输出）

```
missing          = E5002|凭据缺失: exchange 需要环境变量 NO_SUCH_VAR_XYZ
present_returns  = {'exchange': '/bin'}   （environ={"PATH":"/bin"} 时原值返回）
```
读法：缺凭据即 E5002（禁令 2），不回退匿名/只读；存在则原样返回供调用方使用（不落盘）。

### 例 4：跨环境比对三档（真实输出）

```
same_env            → {env_mismatch: False,  reproduction_failed: False, ...}
diff_env_same_parcel→ {env_mismatch: True,   reproduction_failed: False, ...}
diff_env_diff_parcel→ {env_mismatch: True,   reproduction_failed: True,  ...}
```
三档均**无失败结论**（compare 返回 dict，不抛异常），符合"环境差异不判失败"。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| E-1 | **`device` 写死 `"cpu"`**：无 GPU/CUDA 检测（`:138`）。加密因子挖掘若上 GPU 加速，环境快照无法区分 CPU/GPU 环境。 | P2 | `envelope.py:138` | **需确认意图**：当前系统 CPU-only（ml4t/cvxpy 等），无 GPU 路径。若将来接 GPU 应改此处并纳入 digest 子集；属扩展缺口非当前 bug。 |
| E-2 | **`resource_limits` POSIX-only**：`import resource` 在 Windows 失败→返回 `{}`（`:93-104`）。本机 Windows 实测该字段为空。 | P2（平台） | `envelope.py:93-104`；本机运行 | **已确认符合设计**：try/except 容错，跨环境比对时该字段差异不会误判（两环境都空则相等）。Windows 上少一项诊断信息，不影响可复现性。 |
| E-3 | **`credential_refs` 只校验形态不校验变量存在**：`build_envelope` 登记引用名时不检查环境变量是否真有值（`:119-127`）；存在性由 `require_credentials` 单独校验。 | P2（职责分离） | `envelope.py:119-127,144-161` | **已确认符合设计**：两个函数是两件事——快照只记名字，取值失败走 require_credentials 的 E5002。调用方须显式调后者，这是契约。 |
| E-4 | **`_SECRET_HINTS` 常量未被使用**：定义于 `:27` 但 `build_envelope` 只靠 `_ENV_VAR_NAME` 正则判断，未用 hints 做额外拦截。 | P2（死代码） | `envelope.py:27` vs `:119-127` | **轻微冗余**：_regex 已覆盖传值场景（`sk_live_...` 非全大写），hints 是双保险但未接。可删或用，不影响正确性。 |

**多了什么**：`_SECRET_HINTS`（E-4）是唯一冗余常量。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 环境快照不进产物指纹（只记录/比对） | 采纳 | 换机器/升级无关依赖不使历史产物失效（`:3-5`） |
| 只存凭据引用名不存值 | 采纳 | 引用名不泄露值；`_ENV_VAR_NAME` 正则比长度/符号检测可靠（`:28-30`） |
| 跨环境三档不判失败 | 采纳 | 环境差异是事实，`reproduction_failed` 只是诊断信号（`:74-82`） |
| 凭据缺失回退匿名/只读 | **拒绝（硬）** | E5002 硬失败，明确禁止回退（禁令 2，`:144-161`） |
| 把环境指纹混入 parcel_digest | **拒绝** | 会让"换台机器重跑"全部失效，违背可复现语义（`:66`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **12 信封字段齐全**：`to_dict` 输出 12 键（实测），排序稳定（`package_versions`/`credential_refs` 均 sorted，`:51,61`）。
2. **凭据只存引用名**：实测 `credential_refs={'exchange':'BINANCE_API_KEY'}`，无值。
3. **引用名形态校验**：传密钥值/小写名 → E2004（实测，`:119-127`）。
4. **require_credentials 硬失败**：缺失 → E5002（实测），不回退。
5. **跨环境三档不判失败**：`compare` 返回三档 dict，无异常（实测，`:74-82`）。
6. **digest 用契约唯一公式**：`environment_digest` 经 contracts/digest 调用（`:68-72`），不另写哈希。
7. **clock_basis 固定 utc**：`:135`，防本地时区漂移。
8. **`to_dict` 是纯投影**：无 I/O、无 now()，可复现。

**发现的问题**：E-1~E-4 均为 P2/需确认意图或轻微冗余，**无 P0/P1 级代码 bug**。测试覆盖凭据拒绝、缺失 E5002、跨环境比对（`test_v5_storage.py:225-250,400-405`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/03-runtime.md` §7（环境快照） | 描述三档处理 + 不进指纹 | 与实现一致，无需更新 |
| `16-security.md`（凭据禁令） | 三条禁令 | 与实现一致；可补"引用名用全大写正则识别"的实现细节（说明性） |
| `s0/03-runtime.md` §7 | 若将来上 GPU | device 当前写死 cpu（E-1），届时补 GPU 检测并更新本节 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| GPU/CUDA 环境记录 | ❌ 不支持 | device 写死 cpu（E-1）；需改 build_envelope 并纳入 digest 子集 |
| 新增环境字段（如 compiler 版本） | ⚠️ 需改代码 | Envelope dataclass 字段 + to_dict + digest 子集三处（`:33-62,64-72`）；非纯配置 |
| 密钥别名（非环境变量名）引用 | ❌ 不支持 | `_ENV_VAR_NAME` 只认全大写环境变量名（`:30`）；密钥库别名需扩展正则与校验 |
| 凭据存在性预检 | ✅ 已支持 | `require_credentials`（`:144-161`），调用方显式调用即可 |
| 凭据值泄露防护 | ✅ 强 | 正则拦传值 + 只记引用名 + E5002 硬失败（实测） |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为环境/凭据快照，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在环境层处理）。
- 间接支撑 **R2/R7**：环境差异不静默改写结论（compare 三档诚实记录），凭据不泄露。

## R1–R7 影响

- **R1 真数据跑通**：中性（环境记录是辅助，不阻塞执行）
- **R2 结论可信**：✅ 正向（reproduction_failed 信号让"跨环境不同果"有据可查，但不误判失败）
- **R7 fail-closed**：✅ 凭据缺失 E5002、传值 E2004（实测）；环境差异不静默放行为"可复现"

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 信封 12 字段、凭据"只存引用名不存值"（实测 `credential_refs` 仅名字）、传值/缺失双拒（E2004/E5002 实测）、跨环境三档不判失败（实测）——全部真实调用坐实。四项 P2 均为平台/扩展或轻微冗余：E-1 device 写死 cpu（当前 CPU-only 合理）、E-2 resource_limits POSIX-only 在 Windows 空（容错正确）、E-3 引用名不校验存在性（由 require_credentials 分离）、E-4 `_SECRET_HINTS` 未用（死常量）。**生产可用（能真挖因子）= 是**：凭据不泄露、环境可记录可比对，不阻塞真实运行。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 38 个模块 `charter.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 E-1（是否预留 GPU 字段）→ 告诉我
