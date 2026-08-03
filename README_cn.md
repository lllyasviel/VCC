# VCC: View-oriented Conversation Compiler

[English](README.md) | [简体中文](README_cn.md) | [日本語](README_jp.md)

VCC 将本地 agent 会话 JSONL 编译成易读、可搜索的视图，并提供稳定的 block 角色和行号范围引用。目前支持 GitHub Copilot CLI、Codex 和 Claude Code，可自动识别输入格式。

VCC 是论文 “View-oriented Conversation Compiler for Agent Trace Analysis” 的配套实现（[论文](https://arxiv.org/abs/2603.29678)）。学术复现实验位于 [VCC-experiments](https://github.com/lllyasviel/VCC-experiments)。

## 支持的客户端

| 客户端 | 常见本地输入 | 归一化内容 |
|---|---|---|
| GitHub Copilot CLI | `${COPILOT_HOME:-$HOME/.copilot}/session-state/*/events.jsonl` | 消息、reasoning、工具、结果、compaction |
| Codex | `${CODEX_HOME:-$HOME/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl` | 消息及 function/custom tool 事件 |
| Claude Code | `$HOME/.claude/projects/**/*.jsonl` | 消息、thinking、工具、结果、compaction |

原始 JSONL 始终是权威数据。生成视图是可再生派生数据；如果活跃会话继续追加，旧视图就会过期。

### 默认来源优先级

`searchchat` 和 `recall` 不会无条件同时搜索三个客户端：

1. 用户明确指定的客户端或客户端集合优先级最高。
2. 用户明确要求全局或跨平台搜索时，直接搜索所有存在的来源。
3. 其他情况下，先搜索当前运行该 skill 的 agent 客户端历史。
4. 只有当前来源没有可靠命中、命中含糊或不可用时，才扩展到其他客户端。
5. 如果无法从运行时上下文可靠识别当前客户端，则回退为搜索所有存在的来源，并明确说明该回退。

仅凭某个目录存在不能判断当前 agent。搜索结果会说明使用了哪个层级和哪些根目录。

## 四个配套 skill

四个目录必须一起安装：

| Skill | 用途 |
|---|---|
| `conversation-compiler` | 直接编译已知 JSONL 并检查产物 |
| `readchat` | 用精确 transcript 证据审查一个已知会话 |
| `searchchat` | 跨本地历史发现会话，不为每个候选生成文件 |
| `recall` | 恢复历史决策，并与当前工作区状态核对 |

客户端安装位置和验证方法见 [INSTALL.md](INSTALL.md)。

## 快速开始

编译单个会话，并把视图写入 VCC 私有 managed cache：

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/session.jsonl"
```

批量搜索但不写入 transcript：

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/**/*.jsonl" \
  --grep "literal-or-regex" --search-only
```

自动化调用应优先使用明确的查询语义和结构化输出：

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/**/*.jsonl" \
  --term VCC --term cache --match all --ignore-case --format ndjson --search-only
```

按照当前客户端优先级确定性搜索本地历史：

```bash
python "skills/conversation-compiler/scripts/VCC.py" history-search "VCC cache" \
  --current-client codex --format json
```

只把选中的会话保存到私有 cache：

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/selected.jsonl" \
  --grep "literal-or-regex"
```

未使用 `-o` 时，VCC 依次选择 `${VCC_CACHE_DIR}`、`${XDG_CACHE_HOME}/vcc`、Windows 本地应用数据 cache 或 `~/.cache/vcc`。`--cache-dir` 只用于覆盖这个私有位置。只有用户明确要求导出时才使用 `-o <dir>`；共享导出目录会在写入前拒绝同 stem 输入。

## 输出视图

| 产物 | 生成条件 | 用途 |
|---|---|---|
| `.txt` | 实体化编译 | 高保真语义视图，也是行号引用目标 |
| `.min.txt` | 实体化编译 | 按时间顺序的简版视图，工具调用折叠为引用 |
| `.view.txt` | 实体化编译并使用 `--grep` | 保留对话结构的匹配 block |
| stdout 匹配 | `--grep` | 逆时间顺序、带角色标签的匹配列表 |
| `metadata.json` | managed cache | 源路径、大小、时间戳、生成参数和产物 hash |

`--search-only` 不写入任何上述文件。输出中的 `::rendered` 行号只是发现阶段的虚拟引用；在引用或打开精确范围前，应重新实体化选中的会话。

## 生命周期策略

VCC 不维护记忆数据库，也不会上传会话内容；但实体化视图时会创建本地派生文件。

- 显式编译：默认使用私有 managed cache，绝不修改源会话目录。
- `readchat` / `recall`：复用选中会话的 cache，以支持连续追问。
- 大范围 `searchchat`：使用 `--search-only`，不保留未命中的候选。
- 用户明确导出：使用 `-o`，把输出视为用户持久产物。

Cache 是可再生的。源 JSONL 变化或 VCC 升级后应重新生成；不再被引用的旧 cache 可以删除。
有效的 full/brief cache 默认会被复用。源文件大小、mtime、ctime、截断参数以及 VCC 版本共同组成有效性条件；使用 `--cache-policy refresh` 可强制重新生成。

## 结构化搜索与排序

单个精确文本使用 `--literal`；多锚点查询重复使用 `--term` 并通过 `--match all|any` 指定语义；只有真正需要正则时才使用 `--grep`。`--format json|ndjson` 会输出带 schema 版本的 block 记录，包括来源、角色、full-view 范围、命中 pattern、命中行和确定性相关度分数。用户和 assistant 命中高于无上下文的工具输出；排序只用于选择候选，不能替代证据核验。

`history-search` 会枚举 Copilot、Codex 和 Claude 历史目录，先搜索显式传入的当前客户端，默认只在无命中或弱命中时扩展。当前客户端未知时会搜索全部来源并报告回退。`--current-session` 为上下文压缩恢复增加一个精确的第一层级。

Diagnostics schema v2 将源记录计数与规范化输出分开：`source_records_supported + source_records_ignored + source_records_unknown` 始终等于 `source_records_total`；一条源事件可能产生多条规范化记录，因此 `normalized_records_emitted` 可以不同。`recall_selection` 会直接给出压缩前和最新 brief view，使 agent 默认跳过更早 chain。

Recall 应传入 `--chain-window 2`，只实体化选中的两个 chain。VCC 默认拒绝长度超过 4096 字符，以及常见嵌套无限重复／回溯引用风险正则；应优先使用 literal 或 term。`--allow-unsafe-regex` 只是可信输入的显式逃生口，不提供超时保证。

## 为什么不只是 grep

VCC 会识别输入格式，将其归一化成统一会话模型，解析带角色的 block，分配稳定的 full-view 行号坐标，再生成 full、brief 和 focused 视图。因此搜索结果能区分用户消息、assistant 输出、reasoning、工具输入和工具结果，并提供用于回读上下文的完整 block 范围。

## 实现原理和算法

VCC 对每个输入文件执行确定性流水线：

1. **Lex**：逐行读取 JSONL，并识别 Copilot、Codex 或 Claude 格式。
2. **Normalize**：把客户端特有的消息和工具事件转换为统一 record。
3. **Merge / split**：合并流式 assistant chunk，并按 compaction boundary 拆分 chain。
4. **Parse**：把消息、reasoning、工具、结果和媒体引用解析成中间表示（IR）。
5. **Assign lines**：只在 full IR 上分配一次行号，使所有派生视图共享同一坐标。
6. **Lower**：在不改变 full-view 行号的前提下生成 brief 和 focused 选择。
7. **Emit**：写出实体文件，或者流式输出 search-only 匹配。

可执行入口保持极薄，实际实现位于 `scripts/vcc/`，依赖方向保持单向：

| 模块 | 职责 |
|---|---|
| `common.py` | 共享版本、限制、错误类型和文本工具 |
| `normalizers.py` | Codex 和 GitHub Copilot 客户端专用 schema adapter |
| `parser.py` | 客户端识别、JSONL 校验、chain 处理、媒体处理、诊断和 IR 构建 |
| `renderer.py` | 稳定行号分配，以及 full、brief、focused 视图 lowering |
| `query.py` | Block 匹配、确定性评分、按来源限额和 text/JSON/NDJSON 输出 |
| `cache.py` | 原子写入、cache key、manifest、完整性校验、清理和权限 |
| `compiler.py` | 连接 parser、renderer 和存储的单会话应用流水线 |
| `cli.py` | 参数校验、glob 展开、多输入隔离、cache 策略和退出状态 |

`scripts/VCC.py` 只负责可执行环境和分派到 `vcc.cli`；`history_search.py` 仍是独立的历史发现服务，通过同一个公开 CLI 协议调用编译器。内部模块不依赖入口文件，parser、renderer 和 query 也不依赖 CLI 策略。

Base64 图片和文档只在实体化视图时解码；`--search-only` 仅保留占位符，不解码媒体。工具调用与结果通过 tool ID 关联。Full view 是 VCC 行号引用的目标，但对于不支持或主动省略的事件，原始 JSONL 仍是权威数据。

## 时间和空间复杂度

对单个文件定义：

- `C`：解码后的文本和 JSON 内容大小；
- `R`：JSONL record 数量；
- `B`：IR node/section 数量；
- `L`：渲染输出大小；
- `M`：解码媒体总字节数；`Mmax`：最大的单个解码 payload；
- `F`：输入文件数。

| 阶段 | 时间复杂度 | 峰值内存／磁盘说明 |
|---|---|---|
| 展开输入并按 mtime 排序 | `O(F log F)` | `O(F)` 路径 |
| Lex + normalize | `O(C + R)` | `O(R)` 已解析记录，加当前原始行 |
| Merge + chain split | `O(R)` | `O(R)` |
| Parse + 构建 IR | `O(C + B + M)` | 瞬时内存 `O(C + B + Mmax)`；媒体输出最多 `O(M)` |
| 分配行号 + emit | `O(B + L)` | 渲染 buffer 最多 `O(L)` |
| Brief/focused lowering | `O(C + B)` | 每个 IR 只构建一次 section 和 visibility 索引 |
| 正则匹配 | 取决于 pattern | 普通 literal/simple regex 通常接近 `O(C)`；病态 Python `re` 可能发生超线性回溯 |

因此，除取决于 pattern 的正则行为外，单个实体化文件的复杂度为 `O(C + B + L + M)`。当前文件的峰值工作内存为 `O(C + B + L + Mmax)`。VCC 会在处理下一个文件前显式释放当前结果，所以峰值由最大单文件决定，而不是所有输入之和。

单个实体化文件的持久磁盘量为 `O(Lfull + Lbrief + Lview + M)`；多文件总量是上述各项逐文件求和，再加 `O(F)` 的小型 cache metadata。`--search-only` 的持久输出空间为 `O(1)`，且不会解码嵌入媒体。

## Token 消耗

执行 `VCC.py` 本身消耗 **0 个 LLM/API token**：它是本地确定性 Python 程序。只有 agent 读取生成文本或搜索 stdout 时，才会消耗模型上下文 token。

控制台显示的 `words` 不是 OpenAI、Anthropic 或 GitHub 模型的 token 数。VCC 的轻量 tokenizer 会合并连续字母/数字、单独计算标点并忽略空白，因此只能用于相对大小估计。

设 `U` 为保留的用户 block 数，`A` 为保留的 assistant 文本 block 数，`S_tool` 为实际输出工具摘要的词法总长度，`tu` 为 `-tu`，`t` 为 `-t`：

- Full view 的上下文量大致与所有可见 transcript 文本成正比：`Θ(Cvisible)`。
- Brief view 大约受 `O(U·tu + A·t + S_tool + headers)` 个 VCC 词法单位约束；thinking 和工具结果正文通常不会进入 brief。路径、pattern 等部分摘要字段没有固定长度上限。
- Focused/search 输出只与匹配行及 block metadata 成正比，而不是完整 transcript。

最低 token 工作流：先用 `--search-only`，只实体化选中会话，先读 `.min.txt`，最后只打开被引用的 `.txt` 范围。精确模型 token 必须使用实际消费该视图的模型 tokenizer 测量。

## 当前状态和后续方向

VCC 2.3.0 已适合个人工作流、本地团队使用和公开 beta 发布，但定位不是集中式、多租户的会话历史服务。在当前已验证范围内，没有已知会阻塞发布的 P0/P1 问题。

当前版本的验证依据包括：

- 可确定性解析和搜索 Codex、Claude Code 与 GitHub Copilot CLI 日志；
- 42 项自动化测试、4 个 skill package validator，以及覆盖三个客户端的代表性脱敏 fixture；
- 已用包含多次上下文压缩边界的真实 Codex 会话验证；
- Linux、macOS、Windows 和所支持 Python 版本的 CI，以及可复现 benchmark 工具；
- 有界媒体解码、cache 完整性校验、保守的正则防护和来源感知的 recall 选择。

源 JSONL 始终是正本。生成视图和 cache 都是派生产物：可以用完删除；只有重复检索的收益足以抵消存储和隐私成本时，才应私有保留。`--chain-window` 会降低后续 IR、渲染、磁盘和 agent 上下文成本，但 normalize 阶段仍会保留当前输入已解析的 record，因此超大单会话的内存占用仍与该文件规模成正比。

后续优化按优先级排列如下：

1. 为每个客户端实现有状态的单遍 streaming normalizer，在不改变确定性输出的前提下降低超大日志的峰值内存。
2. 随客户端格式演进，增加真实 schema fixture、schema drift 检查、坏输入测试和 fuzz 覆盖。
3. 增加跨操作系统的正则执行隔离或硬超时；当前防护有意采取保守策略，`--allow-unsafe-regex` 仍是显式逃生口。
4. 在更大 benchmark 档位持续跟踪性能回归，并记录 peak RSS 和长时间 cache 行为。
5. 如果某些部署认为 size/mtime/ctime 校验不足，再增加高完整性的源文件 hash 模式。
6. 只有实测负载证明值得时，才考虑可选、隐私友好的增量内容索引。VCC 默认不会把原始会话文本复制进永久索引。

未来客户端 schema 在被 fixture 和测试覆盖前，不视为自动兼容。VCC 不上传会话数据，也不依赖云服务。

## 隐私和限制

会话日志和生成视图可能包含源码、命令、文件路径、工具输出、凭证或其他敏感信息。

- Cache 目录应保持私有，并排除在版本控制和云同步之外。
- `--cache-dir` 会在 POSIX 系统上尽力设置仅属主可访问的权限。
- 发布生成视图前必须人工检查。
- 默认情况下，未以换行结束且格式不完整的最后一条 JSONL 会被视为活跃会话尾部，在给出警告后忽略；中间坏行仍会令该输入失败。
- 多文件任务会隔离坏输入、继续处理健康文件，并在任一输入失败时返回非零状态。使用 `--strict` 可启用遇错即停，并拒绝不完整尾行。
- 嵌入媒体扩展名会被净化，Base64 会严格校验；每个解码后对象默认限制为 64 MiB。
- 大规模实体化搜索会消耗大量磁盘和内存，应优先使用 `--search-only`。
- 共享 `-o` 目录会在写入前拒绝同 stem 输入。
- 生成视图只反映编译时的源日志，不能证明当前工作区或运行状态。

## CLI 参数

```text
VCC.py INPUT [INPUT ...]
  --grep REGEX       搜索带角色的 block
  --search-only      必须配合 --grep；逐文件搜索且不写视图
  --cache-dir DIR    覆盖私有 managed cache 根目录
  --cache-policy P   复用有效 cache 或强制刷新
  --strict           拒绝不完整尾行，并在首个输入错误处停止
  --literal TEXT     搜索一个字面字符串
  --term TEXT        增加字面锚点，可重复并由 --match 组合
  --match all|any    多关键词查询语义
  -i, --ignore-case  忽略大小写
  --format FORMAT    text、json 或 ndjson 搜索输出
  --max-matches-per-input N  每个输入只保留分数最高的 N 个 block
  --diagnostics      输出解析覆盖率、压缩边界和未知事件类型
  --max-media-bytes N  限制每个解码媒体对象；0 表示不限
  --chain-window N  只实体化最新 N 个 chain；0 表示全部
  --allow-unsafe-regex  绕过保守的正则安全检查
  -o, --output-dir   导出到用户指定目录
  -t N               brief 视图中 assistant/tool 截断上限，默认 128
  -tu N              brief 视图中用户消息截断上限，默认 256
```

`--grep` 使用 Python 正则表达式。搜索普通文本时应转义正则特殊字符。

来源选择、当前会话精确搜索、扩展策略、评分和数量限制见 `VCC.py history-search --help`。

运行 `python benchmarks/benchmark_vcc.py` 可得到 search-only、最新两 chain 实体化和 cache hit 的确定性 JSON benchmark。使用相同机器和参数比较不同版本。

## 引用

```bibtex
@article{zhang2026vcc,
  title={View-oriented Conversation Compiler for Agent Trace Analysis},
  author={Lvmin Zhang and Maneesh Agrawala},
  year={2026}
}
```
