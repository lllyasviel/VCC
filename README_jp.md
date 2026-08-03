# VCC: View-oriented Conversation Compiler

[English](README.md) | [简体中文](README_cn.md) | [日本語](README_jp.md)

VCC はローカルの agent セッション JSONL を、読みやすく検索可能な transcript view にコンパイルします。block の役割と安定した行範囲参照を保持し、GitHub Copilot CLI、Codex、Claude Code の形式を自動判別します。

VCC は “View-oriented Conversation Compiler for Agent Trace Analysis” の実装です（[論文](https://arxiv.org/abs/2603.29678)）。学術実験の再現用コードは [VCC-experiments](https://github.com/lllyasviel/VCC-experiments) にあります。

## 対応クライアント

| クライアント | 代表的なローカル入力 | 正規化する内容 |
|---|---|---|
| GitHub Copilot CLI | `${COPILOT_HOME:-$HOME/.copilot}/session-state/*/events.jsonl` | message、reasoning、tool、result、compaction |
| Codex | `${CODEX_HOME:-$HOME/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl` | message、function/custom tool event |
| Claude Code | `$HOME/.claude/projects/**/*.jsonl` | message、thinking、tool、result、compaction |

元の JSONL が常に正本です。生成 view は再生成可能な派生データで、進行中のセッションが追記されると古くなります。

### Default source priority

`searchchat` と `recall` は常に 3 client を同時検索するわけではありません。

1. ユーザーが明示した client または client set を最優先。
2. global/cross-platform が明示された場合は全ての既存 source を検索。
3. それ以外は、この skill を実行している現在の agent client の履歴を先に検索。
4. 最初の tier に信頼できる match がない、曖昧、または利用不能な場合だけ他 client に拡張。
5. Runtime context から現在の client を判定できない場合は、全 source 検索へ fallback し、その事実を報告。

Directory の存在だけでは current agent を判定しません。結果には使用した tier と root を記載します。

## 4 つの skill

4 つをまとめてインストールしてください。

| Skill | 用途 |
|---|---|
| `conversation-compiler` | 既知の JSONL を直接コンパイルする |
| `readchat` | 既知の 1 セッションを正確な transcript で確認する |
| `searchchat` | 全候補を保存せずローカル履歴を検索する |
| `recall` | 過去の判断を復元し、現在の workspace と照合する |

インストールと検証は [INSTALL.md](INSTALL.md) を参照してください。

## クイックスタート

1 セッションを VCC の private managed cache にコンパイルします。

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/session.jsonl"
```

大量のセッションをファイル生成なしで検索します。

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/**/*.jsonl" \
  --grep "literal-or-regex" --search-only
```

自動処理では明示的な query semantics と structured output を推奨します。

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/**/*.jsonl" \
  --term VCC --term cache --match all --ignore-case --format ndjson --search-only
```

Current client 優先で local history を決定的に検索します。

```bash
python "skills/conversation-compiler/scripts/VCC.py" history-search "VCC cache" \
  --current-client codex --format json
```

選択したセッションだけを private cache に保存します。

```bash
python "skills/conversation-compiler/scripts/VCC.py" "path/to/selected.jsonl" \
  --grep "literal-or-regex"
```

`-o` がない場合、VCC は `${VCC_CACHE_DIR}`、`${XDG_CACHE_HOME}/vcc`、Windows local app-data cache、`~/.cache/vcc` の順に選択します。`--cache-dir` はこの private location の override にだけ使います。`-o <dir>` は明示的な export にだけ使用してください。共有出力先は同じ stem の入力を write 前に拒否します。

## 出力

| Artifact | 生成条件 | 用途 |
|---|---|---|
| `.txt` | materialized compile | 高忠実度の意味 view と行参照先 |
| `.min.txt` | materialized compile | tool を参照に畳んだ短縮 view |
| `.view.txt` | `--grep` 付き materialized compile | 会話構造を保った matching block |
| stdout matches | `--grep` | role 付きの逆時系列 match |
| `metadata.json` | managed cache | 入力 path、size、timestamp、生成 parameter、artifact hash |

`--search-only` はファイルを書きません。`::rendered` の範囲は探索用の仮想参照です。正確な範囲を読む前に、選択した入力を materialize してください。

## 保存期間の方針

VCC は memory database を維持せず、セッションを upload しません。ただし view を materialize するとローカル派生ファイルを作成します。

- 明示的な compile: private managed cache を使い、source history directory を変更しない。
- `readchat` / `recall`: 選択した session の cache entry を follow-up で再利用。
- 大規模な `searchchat`: `--search-only` を使い、不一致候補を保存しない。
- 明示的な export: `-o` を使い、永続的なユーザー成果物として扱う。

Cache は再生成できます。入力 JSONL の更新や VCC の upgrade 後は再生成し、不要な entry は削除できます。
有効な full/brief cache は既定で再利用します。canonical source path、size、timestamp、file identity、truncate parameter、VCC version が一致する場合だけ有効で、`--cache-policy refresh` で強制再生成できます。

## Structured search と ranking

単一 literal は `--literal`、複数 anchor は反復 `--term` と `--match all|any`、regex が必要な場合だけ `--grep` を使います。`--format json|ndjson` は source、role、full-view range、matched pattern、matching line、決定的 score を含む versioned block record を出力します。user/assistant match は説明のない tool output より上位ですが、ranking は候補選択用で結論の証拠ではありません。

`history-search` は Copilot、Codex、Claude root を列挙し、明示された current client を最初に検索し、no/weak match の場合だけ既定で拡張します。current client が不明なら全 source fallback を報告します。`--current-session` は compaction recovery 用の exact first tier です。

Diagnostics schema v2 は source accounting と normalized output を分離します。`source_records_supported + source_records_ignored + source_records_unknown` は常に `source_records_total` と一致し、1 source event が複数 record を生成できるため `normalized_records_emitted` は異なる場合があります。`recall_selection` は pre-compaction と latest brief view を直接示します。

Recall では `--chain-window 2` を渡し、選択された 2 chain だけを materialize します。VCC は 4096 文字超と一般的な nested unbounded repeat/backreference regex を既定で拒否します。literal/term を優先し、`--allow-unsafe-regex` は trusted input 用の明示 override としてだけ使用します。

## 実装原理とアルゴリズム

VCC は各入力に対して次の決定的 pipeline を実行します。

1. JSONL を逐次 lex し、Copilot、Codex、Claude の形式を判定。
2. client 固有の message/tool event を共通 record に normalize。
3. streamed assistant chunk を merge し、compaction chain を split。
4. message、reasoning、tool、result、media を IR に parse。
5. full IR に一度だけ行番号を割り当て、全 view の座標を固定。
6. 行番号を変えず brief/focused view に lower。
7. file を emit、または search-only match を stream。

Executable entry point は意図的に薄く保ち、実装は一方向依存の `scripts/vcc/` に置きます。

| Module | 責務 |
|---|---|
| `common.py` | 共通 version、limit、error、text utility |
| `normalizers.py` | Codex と GitHub Copilot 固有 schema adapter |
| `parser.py` | client 判定、JSONL validation、chain/media、diagnostics、IR 構築 |
| `renderer.py` | stable line assignment と full/brief/focused lowering |
| `query.py` | block match、deterministic score、source limit、text/JSON/NDJSON output |
| `cache.py` | atomic write、cache key、manifest、integrity validation、cleanup、permission |
| `compiler.py` | parser、renderer、storage を接続する single-session pipeline |
| `cli.py` | argument validation、glob 展開、multi-input isolation、cache policy、exit status |

`scripts/VCC.py` は executable setup と `vcc.cli` への dispatch だけを担当します。`history_search.py` は同じ公開 CLI protocol を使う独立した history-discovery service です。内部 module は entry point に依存せず、parser/renderer/query は CLI policy に依存しません。

Base64 media は materialized view の場合だけ decode され、`--search-only` は placeholder のみを保持します。tool call と result は tool ID で関連付けられます。full view は VCC 行参照の基準ですが、未対応または意図的に省略した event については元の JSONL が正本です。

## 時間・空間計算量

1 file について `C` を text/JSON サイズ、`R` を record 数、`B` を IR node/section 数、`L` を出力サイズ、`M` を media 総 byte 数、`Mmax` を最大の単一 decoded payload、`F` を入力 file 数とします。

| Stage | Time | Peak memory / disk |
|---|---|---|
| Input 展開 + mtime sort | `O(F log F)` | `O(F)` path |
| Lex + normalize | `O(C + R)` | `O(R)` parsed record と current raw line |
| Merge + chain split | `O(R)` | `O(R)` |
| Parse + IR | `O(C + B + M)` | transient memory `O(C + B + Mmax)`、media `O(M)` |
| Line assignment + emit | `O(B + L)` | buffer `O(L)` |
| Brief/focused lowering | `O(C + B)` | section/visibility index を IR ごとに一度構築 |
| Regex match | pattern 依存 | 単純 pattern は通常 `O(C)` に近いが、病的な Python `re` は超線形 backtracking の可能性あり |

Regex pattern 依存の挙動を除き、materialized file 1 件は `O(C + B + L + M)`、peak working memory は `O(C + B + L + Mmax)` です。各結果を次の file の前に明示的に解放するため、peak は合計ではなく最大 file に依存します。

1 materialized file の persistent disk は `O(Lfull + Lbrief + Lview + M)` です。複数 file では各項の合計に `O(F)` の小さな metadata が加わります。`--search-only` の persistent output は `O(1)` で、埋め込み media を decode しません。

## Token 消費

`VCC.py` の実行自体は **LLM/API token を 0** 消費します。ローカルの決定的 Python program であり、agent が view または stdout を読むときだけ model context token が消費されます。

Console の `words` は OpenAI、Anthropic、GitHub の model token 数ではありません。VCC 独自の軽量 tokenizer による相対サイズです。

`U` を user block 数、`A` を assistant text block 数、`Stool` を出力された tool summary の lexical 総量、`tu` を `-tu`、`t` を `-t` とすると：

- Full view は概ね `Θ(Cvisible)` の lexical content。
- Brief view は概ね `O(U·tu + A·t + Stool + headers)`。thinking と tool-result 本文は通常除外。path や pattern など一部 summary field には固定上限がありません。
- Focused/search output は全 transcript ではなく matching line と block metadata に比例。

Token を最小化するには、`--search-only` → 選択 session だけ materialize → `.min.txt` → 必要な `.txt` range の順で読みます。正確な token 数は実際に view を読む model の tokenizer で測定してください。

## 現在の状態とロードマップ

VCC 2.3.0 は個人 workflow、local team での利用、public beta に利用できる状態です。ただし、集中型 multi-tenant conversation-history service を目的としていません。現在検証済みの範囲では、release を妨げる既知の P0/P1 issue はありません。

現在の release 根拠：

- Codex、Claude Code、GitHub Copilot CLI log の deterministic な parse と search。
- 42 件の自動 test、4 件の skill-package validator、3 client を対象とする代表的な sanitized fixture。
- 複数の compaction boundary を含む実際の Codex session による検証。
- 対応 Python 範囲での Linux、macOS、Windows CI と、再現可能な benchmark tool。
- media decode 上限、cache integrity check、conservative regex guard、source-aware recall selection。

Source JSONL が常に正本です。生成 view と cache は派生成果物であり、使用後に削除できます。反復検索の利点が storage と privacy cost を上回る場合だけ、非公開で保持してください。`--chain-window` は後段の IR、render、disk、agent context cost を削減しますが、normalize は現在の入力の parsed record を保持するため、非常に大きな単一 session では file size に比例した memory が必要です。

優先する今後の改善：

1. 各 client に stateful single-pass streaming normalizer を実装し、deterministic output を維持しながら巨大 log の peak memory を削減する。
2. Client format の変化に合わせ、実際の schema fixture、schema-drift check、malformed-input test、fuzz coverage を拡充する。
3. OS に依存しない regex execution isolation または hard timeout を追加する。現在の guard は意図的に保守的で、`--allow-unsafe-regex` は明示的な escape hatch です。
4. より大きな benchmark tier、peak RSS、長時間の cache behavior で performance regression を追跡する。
5. Deployment で size/mtime/ctime validation が不十分な場合に high-integrity source-hash mode を追加する。
6. 実測 workload が必要性を示した場合だけ、opt-in かつ privacy-preserving な incremental content index を検討する。既定では raw conversation text を permanent index に複製しません。

将来の client schema は fixture と test で確認されるまで互換とはみなしません。VCC は session data を upload せず、cloud service に依存しません。

## Privacy と制限

ログと view には source code、command、path、tool output、credential などの機密情報が含まれる可能性があります。

- Cache を非公開にし、source control と cloud sync から除外してください。
- `--cache-dir` は POSIX 上で owner-only permission を best effort で設定します。
- 生成 view を公開する前に内容を確認してください。
- 改行されていない不完全な JSONL 最終行は、既定では live-session tail として警告後に無視します。途中の malformed record はその入力を失敗させます。
- 複数入力では失敗した file を隔離して正常な file を続行し、いずれかが失敗すると非ゼロで終了します。`--strict` は fail-fast と不完全 tail の拒否を有効にします。
- Embedded media extension を sanitize し、Base64 を検証します。decoded item は既定で 64 MiB に制限されます。
- 大規模な materialized search は disk と memory を消費するため、`--search-only` を優先してください。
- 生成 view は compile 時点の記録であり、現在の workspace や runtime 状態を証明しません。

## CLI

```text
VCC.py INPUT [INPUT ...]
  --grep REGEX       role-aware block を検索
  --search-only      --grep 必須、view を書かず逐次検索
  --cache-dir DIR    private managed cache root を override
  --cache-policy P   valid cache を再利用、または強制 refresh
  --strict           不完全な最終 record を拒否し、最初の入力 error で停止
  --literal TEXT     1 つの literal string を検索
  --term TEXT        literal anchor を追加、反復して --match で結合
  --match all|any    multi-term query semantics
  -i, --ignore-case  case-insensitive search
  --format FORMAT    text、json、ndjson output
  --max-matches-per-input N  input ごとに score 上位 N block を保持
  --diagnostics      parser coverage、compaction boundary、unknown event type を出力
  --max-media-bytes N  decoded media item の上限、0 は unlimited
  --chain-window N  newest N chain のみ materialize、0 は all
  --allow-unsafe-regex  conservative regex safety check を bypass
  -o, --output-dir   指定ディレクトリへ export
  -t N               assistant/tool の短縮上限（default: 128）
  -tu N              user message の短縮上限（default: 256）
```

`--grep` は Python 正規表現です。通常文字列を検索するときは正規表現の特殊文字を escape してください。

Source selection、exact current session、expansion、score、limit は `VCC.py history-search --help` を参照してください。

`python benchmarks/benchmark_vcc.py` は search-only、latest-two materialization、cache-hit path の deterministic JSON benchmark を出力します。同じ machine/parameter で version を比較してください。

## Citation

```bibtex
@article{zhang2026vcc,
  title={View-oriented Conversation Compiler for Agent Trace Analysis},
  author={Lvmin Zhang and Maneesh Agrawala},
  year={2026}
}
```
