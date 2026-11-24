# 有限词形数据集

词汇数据维护者向process(data)提供lemmas、states、accepting、rules、queries，得到生成词形表和逆向分析索引。这是显式有限规则数据编译，不做自然语言模型推断、翻译、全文检索、代码片段管理或CRUD后台。

states为1..50个唯一ASCII id，accepting为1..50个其中状态。rules为0..300条唯一id，from/to引用状态，prefix/suffix为0..64个Unicode代码点，requires和adds为最多20对ASCII特征名/值。整个图（含unused状态）必须无环；不自动展开循环或隐藏去环。

lemmas为1..200项唯一id，stem为1..64代码点，state引用起态，features初始特征。每个状态可接受也可继续派生：当requires所有键值等于当前特征且adds不覆盖任何不同值时可走该边，否则该边不适用。新表面=prefix+当前表面+suffix，特征只增不改；顺序按规则路径。缺少requires键不等于任意值。完整特征在接受态输出，词元相同/表面相同/特征相同但路径不同仍保留独立分析；不能用表面字符串当业务身份。

Unicode输入做NFC规范化，拒绝Cc控制及Cs代理代码点，不做大小写折叠、空白修剪或语言猜测。每个中间表面<=64代码点，特征<=20项，否则整个编译失败而非静默裁掉；拼接跨边界的组合序列在最终索引前再做NFC。总访问派生节点<=100000、接受词形<=20000，超界拒绝，不按字典序截断。queries为0..500项唯一id/非空surface，查规范化后的精确表面；未知表面返回空分析。

forms按规范化surface、lemma、规则id路径排序；queries保留输入顺序，分析排序同词形。空接受结果合法。不支持正字法替换、加权分析、循环转导或外部词典；样例dak/daks为自造字符串，N/number等标签只是声明规则，不声称任何真实语言规律。后续正字法交替和词缀对齐可沿已有lemma/rule path接入。

输入错误抛ValueError，不发布部分数据。io.publish只创建新文件；examples固定演示API，不提供通用命令行工具产品。

# 词面来源查证与勘误回写契约（trace_analysis / apply_errata）

两个离线编辑API复用process的全部编译语义（可接受派生、特征兼容、大小上限、最终NFC、排序），不修改输入对象，不维护服务或磁盘状态。两者都先整份编译词典：词典非法抛ValueError，不因指定路径本身可走而放行。process、build_release、Workspace等既有接口保持原样。

## trace_analysis(data, lemma, rules)：逐位置来源查证

按（词元ID，有序规则ID路径）选择一条实际产出的分析；获接受的空路径与在中间状态被接受的路径均可选择。同形同特征的另一条分析不作替代，queries的选词不影响结果。lemma/rules结构非法抛ValueError；该身份未产出抛AnalysisNotFoundError。

返回恰好两个键：

- analysis：该分析的lemma、surface（最终NFC词面）、features、rules，与process的forms行一致。
- positions：按最终词面代码点顺序逐项给出index、char、sources。sources是该位置的全部来源，按（类别、属主、字段、下标）排序并去重；每个来源是`{"lemma": 词元ID, "field": "stem", "index": i}`或`{"rule": 规则ID, "field": "prefix"/"suffix", "index": i}`，index为**原输入字段**（未正规化拼写）里的零基代码点下标，不按UTF-8字节、可见字形或正规化后的字段计算。空字段不贡献位置。

来源按Unicode规范等价确定：参与该路径的原始字符按词面拼接次序（后应用规则的前缀在外、词干居中、后缀按应用次序）看成带来源的NFD分解原子，同一原字符分成多份时每份仍属该原字符；最终词面字符同理带各自输出位置；两侧分别按组合类稳定排序（同类组合标记保持原相对次序）后按对应原子归属，去掉重复来源。这同时规定了跨字段组合、组合标记重排与一个源字符影响多个最终位置的口径。查证不改变任何旧编译结果。

查证只计算被指定分析的逐位置来源，不为整份词典保留逐字符来源表。验收口径：64代码点词干、14层每层两条空变换规则、仅末态接受的图（16,384条接受路径）上，单次查证（含整词典编译与结果）的CPython tracemalloc堆峰值低于48 MiB。

## apply_errata(data, errata)：确认勘误写回并交付修订词典

errata为0..2000条申请的数组，针对同一份未修改词典。每条恰好含lemma、rules、start、end、replacement、confirmed：一个批内互不重复的分析身份；其**原最终词面**上的非空半开区间[start,end)；原样写入源字段的替换字符串（可以为空）；编辑确认的**完整**修订后NFC词面（1..64代码点、无Cc/Cs、已是NFC）。结构非法抛ValueError并拒绝整批。

每条申请先按trace_analysis的同一口径查证。只有同时满足以下条件的区间才可回写源字段，否则抛IntervalNotWritableError（定位申请序号与分析身份）：

- 区间内全部来源属于同一个源字段（同一属主的stem或prefix或suffix），且在原字符串中恰好构成一个连续切片；
- 该切片里的任何原字符都不再贡献给同一分析的区间外位置（例如源字符经规范分解同时喂给区间内外时，不能只改区间内那一份）。

不猜拆分、不换字段、不改另一条路径。所有切片以修改前的原字段为坐标：不同申请指定同一切片且写入同样的替换字符串时只写一次；同范围不同替换、或其他相交切片，抛ConflictingSlicesError（定位源字段与所涉切片）并拒绝整批。不相交切片共同生效，前一处替换的长度变化不移动后一处在旧源文本中的目标。除这些切片外保留词典全部内容，无关文本保留原始Unicode拼写（不正规化）。

整批共同生效后重跑现有完整编译：修订词典必须编译通过；每个申请身份仍存在、特征不变、实际NFC词面逐一等于confirmed；任何未申请的分析完整保留且词面、特征不变，不得出现新分析。不靠删行、只检查订正位置、只检查queries或只检查申请路径来放行。任一约束不满足抛RevisionRejectedError（至少定位一处申请、源字段或受影响分析），不交付部分修订词典。

成功返回恰好两个键：dictionary（完整的原输入格式词典，调用方可直接交给原process）与writes（实际写入记录，按属主类别、属主ID、字段、起点排序，每项含lemma或rule、field、start、end、old、new）。同样的申请事实不因申请次序改变交付的词典或写入记录。

错误类别均以ErrataError（继承ValueError）为基类，可区分：AnalysisNotFoundError（未产出的分析，附lemma/rules）、IntervalNotWritableError（不可回写的区间，附request/lemma/rules/detail）、ConflictingSlicesError（相交或分歧的切片，附field/slices/detail）、RevisionRejectedError（整批生效后不满足编译或词形约束，附detail及可定位的lemma/rules/request）；结构非法输入仍是普通ValueError。

# 分片发布契约（release.build_release）

build_release(data)复用process的全部编译语义（可接受派生、特征兼容、大小上限、最终NFC、排序），发布所有forms，与输入queries的选择和顺序、lemmas/rules数组顺序无关。process、load、publish接口保持原样。

分片编号：取最终NFC词面UTF-8字节的SHA-256，首个小写十六进制字符（0..f）即所属分片。每个分片是可单独传输的JSON数组，片内行保持forms的规范顺序（surface、lemma、规则路径），空分片也是实在的`[]`数组。行只含lemma、surface、features、rules四个字段；分析身份为（lemma id，有序规则id路径），同片内不得重复，词面与特征相同的不同分析各自保留。

清单对象恰好两个键：`version`（整数1）和`buckets`（16个分片编号到分片摘要的映射）。分片摘要=分片规范字节的SHA-256小写十六进制；发布版ID=清单规范字节的SHA-256小写十六进制。规范字节统一为UTF-8 JSON：对象键排序、非ASCII不转义、无额外空白、无结尾换行。Release对象的manifest_bytes()与shard_bytes(bucket)分别给出可直接交给接收端的字节；等价词形数据产生相同版ID与分片字节，源路径与生成时间不进入发布身份。编译失败时不产生发布版。

# 离线接收契约（receiver.Workspace）

Workspace(path)在指定本地目录保存状态，不依赖发布端进程、完整源输入或未订阅分片；同一时刻只要求一个写入者。一个工作区有零或一个当前视图，以及最多一个未完成切换。视图身份=（发布版ID，非空分片编号集合），编号传入顺序不影响身份。

存储布局：`current.json`为当前视图描述符（release_id、buckets、清单内嵌），`staging.json`为暂存描述符，`blobs/<release_id>/<bucket>.json`为按内容寻址的已验分片，`tmp/`为原子写临时目录。所有描述符为原子替换写入；启用切换只靠一次current.json原子提交，因此重开工作区只能识别到完整旧视图或完整新视图，不保留历史版本。

业务操作与可辨识错误：

- prepare_switch(expected_release_id, manifest_bytes, wanted_buckets)：先校验清单字段、version=1、16个编号、摘要格式、规范字节及与预期ID的SHA-256一致性，再登记暂存；初装走同一操作。同一目标重复准备保留已收分片；已有不同暂存目标抛StagingConflictError，须先abandon_switch()放弃，放弃不影响当前视图。预期ID由调用方单独取得并作为信任起点，不实现清单签名。
- missing_shards()：对暂存目标精确返回仍需的（编号，摘要）。复用规则：仅同编号、同摘要、已验证的当前视图分片，或同一暂存目标已成功接收的分片可复用；不能从摘要猜测覆盖，当前视图的同编号旧内容不算已收到。
- deliver(batch)：每批绑定精确暂存视图，项为`{"bucket", "bytes"}`，可含多个目标编号；全批校验通过才登记，单批失败不改变任何状态，此前成功批次仍可用。拒绝非目标编号、批内重复编号、摘要不符、非规范JSON、重复JSON键、非法字段值；分片内校验v1行形状、NFC、词面归属（哈希匹配但词面放错编号仍拒绝）、规范顺序及同片分析身份唯一。已收合法分片重放为空操作。
- activate()：目标全齐才允许提交，否则抛NotCompleteError；提交时版ID、覆盖范围与可查询数据一起切换，并清除暂存计划。暂存期间只能查旧视图。
- lookup(expected_release_id, surface)：用与输入相同的规范化口径，只查当前视图，不触发下载、不用旧版回答新版查询。LookupResult.status五态可区分：no_view（首次安装前）、version_mismatch、not_covered（词面所属分片不在覆盖中）、empty（覆盖内确实无分析）、ok（覆盖内返回该词面全部分析，顺序与编译一致）。已声明覆盖的分片缺失或损坏抛DataIntegrityError，而不是空结果。

崩溃恢复：重开路径时若current与staging同ID且分片齐全，识别为提交后状态并清理暂存；否则保留提交前状态，旧视图可查、已齐暂存可继续启用。提交成功但回报前退出，重开即确认新视图已启用，无需重新交付。

# 批次语料切分契约（split.build_corpora）

build_corpora(data, documents, history=None) 把一批已人工签注的切词文档分成互不泄漏的训练语料与盲测语料，并给出本批训练暴露键记录。词典编译完全复用 process 的解析、特征兼容、NFC 与上限语义；候选取词典产生的全部 forms，与输入 queries 的选择和顺序无关。process、build_release、Workspace 等既有接口保持原样。

## 输入

- data：完整的现有五字段词典输入；词典非法抛 ValueError，不返回部分结果。
- documents：0..12000 篇文档的数组，批内 id 唯一。每篇恰好含 id（原词条 ID 格式）、side（"train" 或 "holdout"）、drop_cost（1..1000000 的整数，布尔不算）、tokens（非空有序数组，全批合计 <=60000）。每个 token 恰好含 text（1..64 代码点、无 Cc/Cs 的原拼写）与 gold；gold 恰好含 lemma（词条 ID）、rules（0..300 个有序规则 ID）、features（<=20 对的完整特征映射）。
- history：缺省或 None 表示首轮。否则恰好含 doc_ids、surfaces、lemmas 三个数组，每类 <=1000000 项；doc_ids/lemmas 为词条 ID 格式，surfaces 为 1..64 代码点、无 Cc/Cs 且已是 NFC 的文本。三类键各自按集合处理，数组顺序与重复不改变语义。历史键是既成事实：词元或词面今天不存在也仍然有效，不能拿新词典重算、核销或替换。

结构错误（词典非法、字段缺失或多余、类型错误、重复文档 ID、超界、历史键格式非法等）抛 ValueError 并拒绝整个请求，不返回部分语料。语法合法但当前词典找不到的词元或规则引用、特征不符的签下分析不是结构错误，而是该文档标注不能沿用。

## 标注复核

token 按 text 的 NFC 在当前词典全部 forms 中查找，原拼写不改写。只有存在一条分析，其词元、有序规则路径与完整特征三者都与 gold 完全一致时才可沿用人工标注。同词面下不同规则路径是不同标注，特征相同也不合并；即使现在只剩一个看似等价的候选也不自动换签。任一 token 不能沿用则整篇文档不能入集，原因记为 invalid 并给出全部不能沿用的 token 位置（tokens 数组的 0 基下标）。不能沿用的文档不参与隔离、不触发冲突、不产生暴露键。

## 暴露键与隔离

每篇可用文档的暴露键有三类，互不混用：doc_ids 为文档 ID 本身；surfaces 为全部 token 的 NFC 词面；lemmas 为这些词面在当前词典任一分析（不只金标）涉及的词元 ID。盲测文档的任一类键命中历史训练记录即整篇排除，原因记为 history，并给出至少一个匹配的键及类别；历史训练不能通过舍弃本批某篇文档来撤回。训练文档与历史同侧，不受此限。

仅在余下可用文档上选两侧交付内容：任意保留的训练文档与保留的盲测文档不得共享同类键，同侧共享不限。可以不保留某些文档，但不得挪侧、截取部分 token，或为隔离而删减一个词面的候选。满足隔离的方案中被舍弃文档的 drop_cost 总和最小（按二部冲突图最小权点覆盖求解，共享键经键节点汇流，不按文档对展开）。两侧允许为空。多解时不指定哪套最优解，但相同输入事实不得因文档数组、历史键数组或词典中无意义的排列变化而选出不同结果。

## 输出

返回恰好四个键：

- train / holdout：交付语料，按文档 ID 排序；每篇保留 id 与有序 tokens，token 保留原拼写 text 与核准的 gold（lemma、有序 rules、完整 features）。
- dropped：未交付文档数组，按文档 ID 排序，每项含 id 与 reason。invalid 附 token_positions；history 附 match={category,value}，给出一个实际命中历史的键；conflict 附 with 与 key={category,value}，with 是一篇实际保留的另一侧文档 ID，key 是双方共享的键，不引用同样未交付的文档。原因优先级：invalid 先于 history，history 先于 conflict。
- exposure：本批训练暴露键记录，恰好是实际交付训练文档的三类键并集，去重排序后与历史输入同格式（doc_ids、surfaces、lemmas 三个数组），可直接并入历史。调用方只在实际使用这批训练语料后才合并；盲测、弃用文档及旧历史都不得混进这个增量。

# 已签注语料迁移契约（migrate_annotations）

编辑重构词形规则时可能拆分规则或更换规则 ID，用户可见的词面与特征不变，但已归档的签注仍引用旧规则路径。`migrate_annotations` 在离线、单次调用内把稳定引用重新绑定到新版分析，并把已签注 JSONL 档案改写成一份可直接消费的迁移包；不修改任何输入对象，不维护跨调用状态，不依赖网络。`process`、`build_release`、`build_corpora`、`trace_analysis`、`apply_errata`、`Workspace` 等既有接口保持原样。

## 输入

签名：

```python
migrate_annotations(old_data, new_data, registry, approvals,
                    archive_path, archive_sha256, target_path)
```

- `old_data`、`new_data`：两份完整的五字段词典输入，按同一份 `process` 编译语义各自整份编译，不使用 `queries` 限定范围。任一份编译报错即整体拒绝（普通 `ValueError`），即使登记项未涉及的路径有问题也不能放行。
- `registry`：登记表数组，每项恰好含 `ref`（唯一标识，原 ID 格式）、`lemma`、有序 `rules`。`ref` 批内唯一；每项必须指向旧版**确实产出**的一条分析（词元、有序规则路径），不同登记项不得指向同一条旧分析。同词元、同词面、同特征而路径不同的分析是不同身份。结构非法或指向未产出的旧分析抛 `ValueError`。
- `approvals`：映射 `ref` -> 有序规则路径数组，表示编辑批准该引用可继承的新版路径。每项至多 8 条且不得重复；只能为已登记引用给出候选（否则 `ValueError`）。未出现的 `ref` 只允许原路径；显式空列表表示不允许任何路径。路径即使出现在批准表中也仍须通过下面的资格核验，不得因路径 ID 恰好未变而跳过。
- `archive_path`：旧签注档案路径，见下节。
- `archive_sha256`：该档案获准时的 SHA-256，小写十六进制 64 位。格式非法抛 `ValueError`。
- `target_path`：要创建的迁移包路径，必须是尚不存在的文件，绝不覆盖。

## 稳定引用的继承分配

对每条登记项，候选新版分析满足：**同一词元**、**同一最终 NFC 词面**、**同一完整特征**，且其有序规则路径来自该 `ref` 的批准表（未列出时只能是原路径）。批准只限定可选路径，不能代替资格核验；未批准的路径从不猜测。

对**全部**登记项（含档案未用到的项）必须找到**恰好一种**完整分配：每项分到一个合格分析，且不同 `ref` 不得分到同一条新版分析。无合格分配或无法一一配对时抛 `NoSolutionError`；存在多于一种完整分配时抛 `AmbiguousAssignmentError`。两者可区分，且都不做逐项贪心、字典序猜测或只处理档案用到的项。新版允许有未分配给任何引用的分析。

分配实现：候选图上的 Hopcroft-Karp 最大匹配求出一种完整分配；随后检查是否唯一（任一引用持有未被匹配使用的候选，或匹配之外存在交替环，即说明存在第二种完整分配）；迭代实现，深度与登记规模无关。

## 档案校验与迁移

旧档案是 UTF-8 JSONL，每行一个对象，恰好含 `doc`、`pos`、`text`、`ref`、`analysis`：

- `doc`、`ref` 用现有 ID 格式；`pos` 为非负整数（布尔不算），行键 `(doc, pos)` 在全档案中严格递增，可不连续。
- `text` 按当前词面输入口径校验（1..64 代码点、无 Cc/Cs），但**保留其原始拼写**，不写回规范化后的文本。
- `ref` 必须已登记；`analysis` 必须完整等于该 `ref` 对应的**旧**分析（lemma、surface、features、rules 四个字段），且 `text` 的 NFC 必须等于旧分析词面。旧行即使碰巧也是新版的合法行，也仍按旧签注核对，不能跳过。
- 每行以 LF 结尾；拒绝重复 JSON 键、缺失或多余字段、非法字段值；单行（含换行）至多 16 KiB，档案至多 300000 行、256 MiB（均计入换行）。
- 不得删行、重排或重签任何行。

档案错误抛 `ArchiveError`，其 `line` 为一基行号。校验与迁移在**同一次读取**中完成：以实际用于迁移的原始字节流式计算 SHA-256，调用方给出的 `archive_sha256` 不符即抛 `DigestMismatchError`，且不发布任何包；不会先核对一遍再用变了的档案生成包。

## 迁移包

成功时 `target_path` 出现单个 UTF-8 JSONL 文件（按实际消费的登记分配生成，两部分同一分配）：

1. 先按 `ref` 排序输出每个登记项 `{"ref": ..., "analysis": {...}}`，`analysis` 为分配给该引用的完整新版分析（lemma、surface、features、rules）。
2. 再按原序输出全部签注行；每行只在 `analysis` 换成其 `ref` 所分配的新版分析，`doc`、`pos`、`text`（原拼写）、`ref` 保持不变。

每行是键排序、非 ASCII 不转义、无多余空白、LF 结尾的规范 JSON 对象。目标文件只在整个包合格后一次性出现（同目录临时文件 + 硬链接），不覆盖已有目标。分配失败、档案结构或签注错误、摘要不符、或发布前写入失败时，目标位置不留任何半成品或临时文件，也不预先创建目标父目录。

## 返回与错误

成功返回：`path`（包路径）、`registry`（新登记表数组，按 `ref` 排序，与包首部相同）、`annotation_lines`（签注行数）、`total_lines`（登记项数 + 签注行数）、`archive_sha256`（实际消费字节的摘要）。

错误边界均可辨识，且都继承 `MigrationError`（继承 `ValueError`）：

- `NoSolutionError`：无完整分配（附未获候选的 `ref` 列表）；
- `AmbiguousAssignmentError`：存在多个完整分配；
- `ArchiveError`：档案行有误（附 `line`）；
- `DigestMismatchError`：档案摘要不符（附 `expected`/`actual`）；
- `PublishError`：发布失败（`TargetExistsError` 为其子类，表示目标已存在）；结构非法输入仍是普通 `ValueError`。

# 词形使用增量交付账本契约（ledger.Ledger）

`Ledger(path, data)` 在一个离线本地目录里维护词形使用事实、封存水位与下游交付确认。它不修改词典、不发布词典分片、不迁移签注档案；`process`、`build_release`、`Workspace`、`build_corpora`、`trace_analysis`、`apply_errata`、`migrate_annotations` 等既有接口保持原样。同一时刻只要求一个写入者，全部状态只用标准库持久化。

## 词典绑定

构造时先按 `process` 的完整语义编译 `data` 的全部 `forms`（不受 `queries` 限定）；词典非法抛普通 `ValueError`，且在任何目录或文件被创建之前抛出。绑定身份 = 编译后完整 `forms` 规范 JSON 字节的 SHA-256（小写十六进制）。`queries` 的内容与顺序、`lemmas`/`rules`/`states` 数组的无意义顺序变化不产生不同 `forms`，因此不改变绑定；实际词面、特征或（词元，有序规则路径）分析身份变化则身份不同。

- 空路径：创建工作区并写入 `binding.json`（内嵌版本、身份与全部编译后 forms）与初始 `ledger.json`。
- 已存在路径：重开必须提供产生相同 forms 的词典，否则抛 `BindingMismatchError`（附 `expected_id`/`actual_id`），磁盘状态不改动。
- 持久描述符缺失、损坏、形状不符、内嵌 forms 与身份不符、已冻结包内容与包 ID 不符，抛 `LedgerIntegrityError`。

## 修订写入（write_revisions）

入参为非空修订数组，每项恰好含 `event`（1..64 代码点、无 Cc/Cs 的文本）、正整数 `revision`、`value`。结构错误（字段缺失或多余、类型错误、`revision` 非正整数、`period` 负或非整数、文本越界或含 Cc/Cs、analysis 四字段不完整、特征/规则 id 非法、同批 `(event, revision)` 重复、空批次）抛普通 `ValueError` 并拒绝整批。

`value` 为 `null` 表示撤回；否则恰好含非负整数 `period`、原拼写 `text` 与完整 `analysis`（`lemma`、最终 NFC `surface`、完整 `features`、有序 `rules`）。业务规则（冲突类均继承 `RevisionError`，且定位到 `event` 与 `revision`）：

- 每个事件首个修订必须是 1，后续编号必须连续；跳号或不从 1 开始抛 `RevisionGapError`。
- 已记录的同 `(event, revision)` 只能以完全相同内容重放，内容不同抛 `RevisionConflictError`；完全相同的重放安全，不新增事实、不推进 `state_version`。
- 撤回只能针对已存在事件（同批次先创建后撤回合法），否则抛 `UnknownEventError`。
- 非空 analysis 必须是绑定词典实际产出的同身份分析（词元、有序规则路径、词面、特征全部一致），且 `unicodedata.normalize("NFC", text)` 等于该分析词面；不满足抛 `UnboundAnalysisError`。同形同特征的另一条规则路径不是替代项。原拼写原样保留。
- 一个批次可含同一事件的多个连续新修订，数组顺序不影响结果；先核完整批次，再一次性生效。任一项失败则当前事实、封存水位、待确认包与下游基线全部不变。

返回 `accepted_new`、`replayed`、`state_version`。仅纯重放（无新增事实）时 `state_version` 不变。

## 事实查询与封存

- `current_facts()`：每个事件只取最新修订，撤回事件不计数；其余按 `period` 与完整分析身份聚合 `count`，排序固定为（period、分析词面、lemma、有序 rules）。开放时期事实照常出现，封存或下游确认不隐藏合法更正。
- `get_event(event_id)`：返回该事件最新修订号与完整值（含原拼写，撤回为 `null`）；未知事件抛 `UnknownEventError`。
- `sealed_facts()` / `confirmed_facts()`：分别给出当前封存事实聚合与下游已确认基线聚合，行形状与排序同 `current_facts()`。
- `seal(period)`：`period` 为非负整数，把 `closed_through` 单调推进到该值（中间时期一并视为封存），倒退抛 `MonotonicSealError`；同值重放不推进版本。封存不禁止迟到事件与更正；只有当前归属时期 `period <= closed_through` 的有效事实进入封存聚合，开放时期事实不能提前进入下游交付。

## 增量交付（next_package / confirm）

系统分别维护「当前封存事实」与「下游已确认基线」两份聚合。`next_package()` 的净差 = 两者按键（period、完整分析身份）聚合后的计数差：正数为增加、负数为撤销、零差不输出，行按与事实查询相同的固定次序排列。某事件经多次修订后回到已确认贡献时净差为零，不产生交付；实现按已确认状态求净差，不按修订日志逐条外发。

- 无净差时返回 `NoDelivery`（`status == "no_delivery"`），不生成空包、不推进检查点、不改任何状态。
- 有净差时冻结一个 `DeliveryPackage`（`status == "package"`），含稳定 `package_id`、`start_checkpoint`/`end_checkpoint`（两端聚合规范字节各自的 SHA-256）、冻结时 `closed_through` 与有序净差行。包 ID = 包规范字节（键排序、非 ASCII 不转义、无多余空白的 UTF-8 JSON）的 SHA-256，因此同一业务状态得到同一包；源路径与时间不进入身份。
- 包内容先以 `packages/<package_id>.json` 内容寻址落盘，再由唯一的 `ledger.json` 原子提交登记。包在确认前冻结：重复取得（含重开工作区、另起进程）返回字节等价的同一包；期间到达的新修订或新封存只进入后续包。
- `confirm(package_id)`：必须引用当前待确认包 ID；正确确认把净差应用到基线（计数归零的键移除）、清空待确认登记，之后下一份包基于新基线计算。正确确认可重放（同 ID 再次调用为空操作）；未知或过期 ID 抛 `UnknownPackageError`，不推进状态。纯负数包与最终抵消为零的更正遵守相同规则。

## 持久化与恢复

存储布局：`binding.json`（绑定描述符与完整 forms）、`ledger.json`（唯一可变状态描述符：版本、dictionary_id、单调 `state_version`、`closed_through`、每事件有序修订流、基线聚合、待确认/最近确认包 ID）、`packages/<id>.json`（冻结包字节）、`tmp/`（原子写临时目录）。所有写入为临时文件 fsync 后 `os.replace` 原子替换；每个写入、封存、冻结、确认操作恰好对应一次 `ledger.json` 提交，冻结包字节在该提交前 durable。

重开同一路径只能观察到完整旧状态或完整新状态：修订与聚合同处一个描述符，不存在「修订已记但聚合未更新」；封存与可交付事实由同一状态重算；确认与基线推进同一提交；已冻结包按内容 ID 持久保存，重试取回原包而不用新事实改造。成功提交但调用方未收到返回时，重开后按相同参数重试即为安全重放（修订内容幂等、封存同值幂等、`next_package` 返回同一冻结包、确认同 ID 幂等），无需猜测操作是否生效。

## 错误分类

结构非法输入是普通 `ValueError`；业务与运行错误继承 `LedgerError`（继承 `RuntimeError`）：`BindingMismatchError`、`LedgerIntegrityError`、`RevisionConflictError`、`RevisionGapError`、`UnknownEventError`、`UnboundAnalysisError`（四者均为 `RevisionError` 子类，附 `event`/`revision`）、`MonotonicSealError`（附 `current`/`requested`）、`UnknownPackageError`（附 `package_id`）。

## 故障注入

模块级 `_CRASH_BEFORE_COMMIT` / `_CRASH_AFTER_COMMIT` 钩子（签名 `hook(kind, ledger)`，`kind` 为 `"write"`/`"seal"`/`"freeze"`/`"confirm"`）仅供测试注入 `SystemExit`，分别在唯一描述符提交点前后触发；生产代码不设置钩子。

# 适配层重基契约（rebase_layers）

区域词典编辑团队在统一的有限词形词典上维护地区、机构或产品专用的增补与屏蔽层。父词典更新后，`rebase_layers(old_base, new_base, layers)` 把同一批层操作按原意图重放到新父版，交付新的有效分析集合、逐项冲突状态与可继续重基的层记录。它是纯离线单次调用：不修改任何输入对象，不发布词典、不改写父词典或层源文件，不维护跨调用状态；`process`、`build_release`、`Workspace`、`build_corpora`、`trace_analysis`、`apply_errata`、`migrate_annotations`、`Ledger` 等既有接口保持原样。

## 输入

- `old_base`、`new_base`：两份完整的五字段词典，各自按 `process` 的完整语义整份编译全部 `forms`，不使用 `queries` 限定范围。任一份编译报错即整体拒绝（普通 `ValueError`，`RebaseError` 为其子类）。
- `layers`：0..32 个层对象的有序数组，从父层向子层排列；数组顺序有业务意义。每层恰好含 `layer_id`、`base_id`、`additions`、`suppressions`：
  - `layer_id`：原 ID 格式，一次调用内唯一，且不得为保留名 `base`。
  - `base_id`：创建该层时父词典全部规范 `forms` 的规范 JSON（键排序、非 ASCII 不转义、无多余空白）SHA-256 小写十六进制。所有输入层都必须等于 `old_base` 的实际 `forms` 身份，否则整次拒绝。
  - `additions`：完整五字段词典，按现有编译规则产生该层新增分析；编译失败即拒绝。
  - `suppressions`：屏蔽操作数组。

每条屏蔽操作指定一个较早来源层（`base` 表示父词典，或此前按顺序出现过的 `layer_id`），并明确一种策略：

- `snapshot`：目标是完整分析行，含来源层与 `lemma`、最终 NFC `surface`、完整 `features`、有序 `rules`。只有整行仍完全相同才屏蔽。
- `follow`：目标含来源层、`lemma` 与有序 `rules`。父版改动表面或特征时仍按 `(lemma, rules)` 稳定身份屏蔽当前行；身份不存在时不得猜测替代。

`source_layer` 可作为 `source` 的同义键；目标行可平铺在操作上，或整体放在单个 `target`（或 `analysis`）对象下。

结构非法、层 ID 重复、`base_id` 不符、来源不是 `base` 或此前层（含指向自身或未来层）、同层出现重复屏蔽目标、目标在声明来源层中从未产出、词典编译失败，以及每次调用 `old_base` + `new_base` + 全部 `additions` 的编译分析总数或全部屏蔽操作总数超过 20,000，都拒绝整次调用，不返回部分结果。每个输入词典仍遵守原有大小上限。

## 分析身份

分析身份至少由（来源层，词元 ID，有序规则路径）共同决定。同表面或同特征不代表同一分析：不同来源层的分析不合并，不同规则路径的同形项不折叠。有效视图按 `(old_base, new_base)` 事实重算，不用旧事实替代新事实，也不用新事实撤销旧操作。

## 重放与状态

从 `new_base` 开始，按层顺序重放：每层先执行其屏蔽操作，之后加入该层 `additions` 产生的新增分析。更高层只可屏蔽此前已加入且当前可见的分析。对每条屏蔽操作：

- `snapshot`：完整目标仍存在且可见时屏蔽该行，报 `applied`。来源层仍有相同 `(lemma, rules)` 身份但完整行已变时报 `changed`，不屏蔽新行。身份已不存在时报 `missing`。若较早操作已屏蔽该目标，报 `already_hidden`，不得记作对另一行的成功删除。
- `follow`：来源层仍有相同 `(lemma, rules)` 身份且当前可见时屏蔽其当前完整行（即使表面或特征已变），报 `applied`。身份不存在报 `missing`。目标已被更早操作屏蔽时报 `already_hidden`。

`changed`、`missing`、`already_hidden` 均为可观察冲突状态；对应原操作必须原样保留在 `rebased_layers` 中，不得静默删除、改写目标或自动猜配。新父版中某目标暂时缺失不撤销屏蔽意图：只要保留记录被再次重基且目标重新出现，`snapshot` 在整行复原时、`follow` 在身份再现时自动重新生效。旧父版事实仅用于确认操作最初合法，新父版事实用于计算当前有效视图。

## 输出

成功返回恰好五个键：

- `forms`：有效分析行，按（最终 NFC `surface`、`source_layer`、`lemma`、有序 `rules`）排序；每行是完整编译分析加 `source_layer`（`base` 或产生它的层 `layer_id`）。
- `suppressions`：按层顺序、层内操作顺序逐项给出 `layer_id`、`index`（同层内 0 基操作下标）、`policy`、`target`（目标身份；`snapshot` 含完整行，`follow` 含 `(lemma, rules)`）与 `status`（`applied`/`changed`/`missing`/`already_hidden`）。
- `old_base_id` / `new_base_id`：各自全部规范 `forms` 的规范 JSON SHA-256。
- `rebased_layers`：与输入层同序、同 `layer_id`、同 `additions`、同 `suppressions` 的层记录，`base_id` 一律更新为 `new_base_id`。该数组可直接作为下一次 `rebase_layers` 的 `layers`：其绑定满足下一次调用中全部输入层绑定其 `old_base` 的要求，并保留全部原始增补与屏蔽事实（含未应用操作）。

相同业务事实不因词典数组中无意义排列（`states`/`rules`/`lemmas` 顺序、`queries` 选择、增补词典内部顺序）而改变结果；有序层的顺序、层 ID 与操作顺序具有业务意义。

## 错误

结构非法输入是普通 `ValueError`；`RebaseError`（`ValueError` 子类）标识整次拒绝。两者都不返回部分结果。重基结果由调用方决定是否接受；本 API 不发布词典、不改写父词典或适配层源文件。
