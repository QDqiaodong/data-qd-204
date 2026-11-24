# data-qd-204 · 有限词形转导与逆向分析

将显式无环词形规则、词元与不可覆写的特征约束编译为词形数据表及逆向分析；同形词和不同规则路径保留独立身份。

本仓主要交付为Python业务API与下游可消费的数据，不以通用CLI、报表或后台模板为产品。可观察字段、单位、身份、上限及失败边界见 [数据契约](docs/contract.md)。

## 独立安装与验证

Python >=3.11，实际验证CPython3.12.14、uv0.12.19。运行和测试仅标准库；构建固定setuptools80.9.0、wheel0.45.1。各仓独立 `.venv`，可以共享公共下载缓存，不共享site-packages。

从本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
```

以上命令在本地现有公共缓存上真实执行通过，无需新增下载。新机器缓存为空时，先准备匹配Python与锁定公共构建包，或在可联网安装环境移除安装命令中的offline；API、测试与样例运行不需要网络。示例创建 `run/sample.json`，目标已存在会拒绝覆盖；重复运行请先自行保留产物并选择新的演示工作副本。

## 离线分片发布与接收

发布端 `morph_paths.release.build_release(data)` 复用 `process` 的编译语义，把全部词形按「最终NFC词面UTF-8 SHA-256首字符」切成16个可单独传输的JSON数组，并给出清单字节与发布版ID；接收端 `morph_paths.receiver.Workspace(path)` 在本地目录维护当前视图与至多一个暂存切换，只订阅需要的分片。完整数据契约见 [数据契约](docs/contract.md)。

```python
from morph_paths import build_release, Workspace, LookupStatus

release = build_release(data)                       # 发布端
ws = Workspace("/path/to/local/workspace")          # 接收端，离线目录
ws.prepare_switch(release.release_id, release.manifest_bytes(), ["3", "4"])
for bucket, digest in ws.missing_shards():          # 只取订阅且缺失的分片
    ws.deliver([{"bucket": bucket, "bytes": release.shard_bytes(bucket)}])
ws.activate()
result = ws.lookup(release.release_id, "é")
assert result.status == LookupStatus.OK             # 另有 no_view/version_mismatch/not_covered/empty
```

状态与错误均为可辨识对象：`NoStagingError`、`StagingConflictError`、`NotCompleteError`、`DataIntegrityError`；`state()` 返回当前视图与暂存（含已收编号）的纯数据描述。清单与分片的字节级校验入口为 `parse_manifest` 与 `parse_shard`。

端到端离线演示（发布端只交付字节，接收端看不到源输入或完整产物）：

```sh
python -I examples/offline_delivery.py
```

`fixtures/scenario_a.json`、`scenario_b0.json`、`scenario_b1.json` 是验收用三版输入：A中`é`（分片4）同时有组合派生r1与预组词元r2两个分析，`b`在分片3、`a`在分片c；B0仅改分片c中`a`的规则路径，B1在B0基础上把`é`的r1改为r2（分片4变化，分片3不变）。

## 已审词形语料的训练/盲测切分

`morph_paths.build_corpora(data, documents, history=None)` 处理一批已人工签注的切词文档：用当前词典复核每条签下分析（词元、有序规则路径、完整特征完全一致才可沿用），再按文档 ID、NFC 词面、相关词元三类暴露键做隔离——盲测文档命中历史训练即排除，余下文档按最小总 `drop_cost` 选出互不泄漏的两侧交付集。返回交付语料、逐篇舍弃原因（`invalid`/`history`/`conflict`）和可直接并入历史的本批训练暴露记录。完整契约见 [数据契约](docs/contract.md)。

```python
from morph_paths import build_corpora

batch1 = build_corpora(dictionary, reviewed_docs)          # 首轮历史为空
# …实际使用 batch1["train"] 训练后，再把暴露记录并入历史…
batch2 = build_corpora(revised_dictionary, next_docs, batch1["exposure"])
```

从上一批训练输出接到下一批盲测筛选的离线演示：

```sh
python -I examples/offline_split.py
```

## 词面来源查证与勘误回写

编辑在词形表里看到错字时，`morph_paths.trace_analysis(data, lemma, rules)` 按（词元 ID，有序规则 ID 路径）查证一条实际产出的分析：返回该分析、最终 NFC 词面，以及每个词面位置来自哪个词元 `stem` 或规则 `prefix`/`suffix` 的哪个原输入代码点下标（按 Unicode 规范等价对齐，覆盖跨字段组合与组合标记重排）。`morph_paths.apply_errata(data, errata)` 把一批编辑确认的勘误（分析身份、原词面区间、替换串、确认后的完整 NFC 词面）共同生效：只有区间能安全映射回单一源字段切片、整批无相交冲突、且修订词典通过完整编译并保持全部未申请分析不变时才交付完整的原输入格式词典与实际写入记录，否则按类别拒绝，不交付部分修订。完整契约见 [数据契约](docs/contract.md)。

```python
from morph_paths import trace_analysis, apply_errata

traced = trace_analysis(dictionary, "L", ["tone"])      # 逐位置来源
revised = apply_errata(dictionary, [                    # 安全时交付修订词典
    {"lemma": "L", "rules": ["tone"], "start": 1, "end": 2,
     "replacement": "", "confirmed": "ä"},
])
process(revised["dictionary"])                          # 可直接交给原 process
```

离线演示（不写磁盘）：

```sh
python -I examples/offline_errata.py
```

## 已签注语料迁移到新版词形规则

规则被拆分或重编号后，用户看到的词面和特征不变，但已归档的签注仍引用旧规则路径。`morph_paths.migrate_annotations(...)` 接收旧、新两份完整词典、稳定引用登记表、编辑批准的候选替换、旧签注档案路径、获准时的 SHA-256 与新包路径：对全部登记引用解出**恰好一种**新版分析分配（不贪心、不猜测、不忽略未用到的引用），再把 UTF-8 JSONL 签注档案改写成一份可直接消费的迁移包（先新登记表、后迁移行，共用同一分配），同时返回新登记表。原词典、旧档案和现有 API 均不改动；失败时目标位置不留半成品。完整契约见 [数据契约](docs/contract.md)。

```python
from morph_paths import migrate_annotations

result = migrate_annotations(
    old_dictionary, new_dictionary,
    [{"ref": "A", "lemma": "L", "rules": ["ra"]},
     {"ref": "B", "lemma": "L", "rules": ["rb"]}],
    {"A": [["rx"], ["ry"]], "B": [["rx"]]},   # 批准路径仍须通过同词元/词面/特征核验
    "signed.jsonl", approved_sha256, "migrated.jsonl",
)
print(result["total_lines"], result["registry"])
```

错误可辨识：`NoSolutionError`（无解）、`AmbiguousAssignmentError`（多解）、`ArchiveError`（档案行错，附行号）、`DigestMismatchError`（摘要不符）、`PublishError`/`TargetExistsError`（发布失败/目标已存在）。

离线演示（不写网络）：

```sh
python -I examples/offline_migration.py
```

## 词形使用事实的可更正增量交付账本

`morph_paths.ledger.Ledger(path, data)` 在指定本地目录维护一份离线、持久化的交付账本：修订式词形使用事实（含撤回与更正链）、单调封存水位 `closed_through`、以及「当前封存事实 vs 下游已确认基线」的净差交付。工作区绑定的是按 `process` 完整语义编译的全部 `forms` 的内容身份（与 `queries` 和数组顺序无关）；实际词形、特征或分析路径变化的词典拒绝重开，词典非法时不创建工作区。

```python
from morph_paths import Ledger, DeliveryStatus

ledger = Ledger("/path/to/ledger", dictionary)
ledger.write_revisions([{"event": "e1", "revision": 1, "value": {...}}])
ledger.seal(10)
package = ledger.next_package()          # 冻结：净差行 + 起止检查点 + 包 ID
if package.status == DeliveryStatus.PACKAGE:
    ledger.confirm(package.package_id)   # 同 ID 确认可安全重放
else:
    ...                                  # NoDelivery：无净差，不推进检查点
ledger.current_facts()                   # 开放时期与合法更正立即可见
ledger.get_event("e1")                   # 最新修订与原拼写，供审计
```

整批修订先核后生效：同号异内容、缺号、未知事件撤回、未绑定分析均整批拒绝（业务冲突继承 `LedgerError` 并定位事件/修订号；结构错误是普通 `ValueError`），纯重放不推进 `state_version`。交付包按规范内容定 ID，确认前冻结，重复取得（含重开、新进程）字节等价；净差按已确认基线计算，多次修订回到已确认贡献时不产生交付。每个写入、封存、冻结、确认恰好对应一次 `ledger.json` 原子提交，冻结包先内容寻址落盘；故障注入钩子（`_CRASH_BEFORE_COMMIT`/`_CRASH_AFTER_COMMIT`）与子进程退出测试覆盖提交前后的可判定恢复。完整契约见 [数据契约](docs/contract.md)。

离线演示（临时目录，不写网络、不改词典）：

```sh
python -I examples/offline_ledger.py
```

## 使用与实际结果

```python
from morph_paths import process
from morph_paths.io import load, publish

result = process(load("fixtures/input.json"))
publish("run/another-sample.json", result)
```

2个自造词元形成6条派生、5个不同表面；daks有2个独立分析，受特征阻止的mini-dakss没有分析。

安装后的样例摘要：

```json
{"ambiguous_queries": 1, "derivations": 6, "distinct_surfaces": 5, "lemmas": 2, "queries": 3}
```

已通过 146 项测试：原 6 项编译业务测试，发布端 8 项（分片归属、清单与规范字节、顺序与身份、输入顺序无关性等）、接收端 10 项（四个验收场景、严格校验与新进程崩溃恢复）、语料切分 15 项（全局最优舍弃、跨批历史阻断、预组/分解拼写、小批穷举对照、6,000+6,000 篇内存上界等）、来源查证与勘误回写 27 项（组合标记重排归属、外内前缀次序、U+0344 共享源下标、共享词干整批约束、63 字符分支上限、16,384 条同形路径单次查证 48 MiB 堆峰值上界等）、已签注语料迁移 46 项（唯一/歧义/无解分配、批准不替代资格核验、末行签注错误定位、摘要不符、目标不覆盖、分解拼写保真、≥96 MiB 档案在 64 MiB 堆峰值内流式迁移等），以及增量交付账本 34 项（封存净差与冻结包字节等价、按已确认基线求净差而非逐条外发、迟到正/负补偿、原拼写保留与聚合身份、重放不推进版本、整批拒绝、检查点链接、同批创建后撤回、绑定身份与仅改 queries/顺序可重开、四类操作提交前后的子进程故障恢复等）。数值和排序语义见契约，不以文件哈希代替业务结果断言。

## 当前边界与来源

不做语言模型推断、翻译、全文检索或词典CRUD，不含真实语言规则库；无循环转导与正字法替换。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
