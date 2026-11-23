# 有限词形数据集

词汇数据维护者向process(data)提供lemmas、states、accepting、rules、queries，得到生成词形表和逆向分析索引。这是显式有限规则数据编译，不做自然语言模型推断、翻译、全文检索、代码片段管理或CRUD后台。

states为1..50个唯一ASCII id，accepting为1..50个其中状态。rules为0..300条唯一id，from/to引用状态，prefix/suffix为0..64个Unicode代码点，requires和adds为最多20对ASCII特征名/值。整个图（含unused状态）必须无环；不自动展开循环或隐藏去环。

lemmas为1..200项唯一id，stem为1..64代码点，state引用起态，features初始特征。每个状态可接受也可继续派生：当requires所有键值等于当前特征且adds不覆盖任何不同值时可走该边，否则该边不适用。新表面=prefix+当前表面+suffix，特征只增不改；顺序按规则路径。缺少requires键不等于任意值。完整特征在接受态输出，词元相同/表面相同/特征相同但路径不同仍保留独立分析；不能用表面字符串当业务身份。

Unicode输入做NFC规范化，拒绝Cc控制及Cs代理代码点，不做大小写折叠、空白修剪或语言猜测。每个中间表面<=64代码点，特征<=20项，否则整个编译失败而非静默裁掉；拼接跨边界的组合序列在最终索引前再做NFC。总访问派生节点<=100000、接受词形<=20000，超界拒绝，不按字典序截断。queries为0..500项唯一id/非空surface，查规范化后的精确表面；未知表面返回空分析。

forms按规范化surface、lemma、规则id路径排序；queries保留输入顺序，分析排序同词形。空接受结果合法。不支持正字法替换、加权分析、循环转导或外部词典；样例dak/daks为自造字符串，N/number等标签只是声明规则，不声称任何真实语言规律。后续正字法交替和词缀对齐可沿已有lemma/rule path接入。

输入错误抛ValueError，不发布部分数据。io.publish只创建新文件；examples固定演示API，不提供通用命令行工具产品。

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
