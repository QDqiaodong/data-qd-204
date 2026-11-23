# 有限词形数据集

词汇数据维护者向process(data)提供lemmas、states、accepting、rules、queries，得到生成词形表和逆向分析索引。这是显式有限规则数据编译，不做自然语言模型推断、翻译、全文检索、代码片段管理或CRUD后台。

states为1..50个唯一ASCII id，accepting为1..50个其中状态。rules为0..300条唯一id，from/to引用状态，prefix/suffix为0..64个Unicode代码点，requires和adds为最多20对ASCII特征名/值。整个图（含unused状态）必须无环；不自动展开循环或隐藏去环。

lemmas为1..200项唯一id，stem为1..64代码点，state引用起态，features初始特征。每个状态可接受也可继续派生：当requires所有键值等于当前特征且adds不覆盖任何不同值时可走该边，否则该边不适用。新表面=prefix+当前表面+suffix，特征只增不改；顺序按规则路径。缺少requires键不等于任意值。完整特征在接受态输出，词元相同/表面相同/特征相同但路径不同仍保留独立分析；不能用表面字符串当业务身份。

Unicode输入做NFC规范化，拒绝Cc控制及Cs代理代码点，不做大小写折叠、空白修剪或语言猜测。每个中间表面<=64代码点，特征<=20项，否则整个编译失败而非静默裁掉；拼接跨边界的组合序列在最终索引前再做NFC。总访问派生节点<=100000、接受词形<=20000，超界拒绝，不按字典序截断。queries为0..500项唯一id/非空surface，查规范化后的精确表面；未知表面返回空分析。

forms按规范化surface、lemma、规则id路径排序；queries保留输入顺序，分析排序同词形。空接受结果合法。不支持正字法替换、加权分析、循环转导或外部词典；样例dak/daks为自造字符串，N/number等标签只是声明规则，不声称任何真实语言规律。后续正字法交替和词缀对齐可沿已有lemma/rule path接入。

输入错误抛ValueError，不发布部分数据。io.publish只创建新文件；examples固定演示API，不提供通用命令行工具产品。
