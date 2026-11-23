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

已通过 24 项测试：原 6 项编译业务测试，以及发布端 8 项（分片归属、清单与规范字节、顺序与身份、输入顺序无关性等）和接收端 10 项（四个验收场景、严格校验与新进程崩溃恢复）。数值和排序语义见契约，不以文件哈希代替业务结果断言。

## 当前边界与来源

不做语言模型推断、翻译、全文检索或词典CRUD，不含真实语言规则库；无循环转导与正字法替换。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
