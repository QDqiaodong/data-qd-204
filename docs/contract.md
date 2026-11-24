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
