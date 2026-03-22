# Control Loop and Memory Architecture

## Overview

boiled-claw v2 では、AI エージェントを単なるチャットボットや tool-using assistant としてではなく、**計画・実行・検証・反省・記憶昇格**を備えた閉ループの実行基盤として扱う。

この設計の土台には Google ADK の `Session` / `State` / `MemoryService` を使う。ただし、それらをそのまま使うのではなく、責務を明確に分離する。

- **Session / State** は短期的な実行制御のための作業メモリ
- **Event history** は実行トレース
- **MemoryService** は昇格済みの長期知識
- **boiled-claw 独自層** は plan / verify / curate / policy を担う制御ループ

この分離により、boiled-claw は **chat + tools** から **closed-loop agent runtime with curated semantic memory** へ進化する。

---

## Design Goals

### 1. Control before generation

エージェントの中心を「応答生成」ではなく「制御」に置く。
何をするか、何が許可されるか、何をもって成功とするかを先に定義する。

### 2. State is not memory

ADK の `session.state` は長期知識の保存場所ではない。
state は現在の実行に必要な短期データだけを持ち、長期的な知識は curated memory として別管理する。

### 3. Memory is not raw transcript

会話ログ全体をそのまま memory に流し込まない。
記憶は candidate → review → promote のライフサイクルを経て昇格させる。

### 4. Verification is first-class

実行後の検証を必須の段階として扱う。
成功条件を満たしていない場合は self-repair loop に戻す。

### 5. Policy is explicit

許可・不許可・承認条件を prompt の暗黙知にせず、明示的な policy layer として持つ。

---

## Layered Architecture

boiled-claw v2 は以下の5層で構成する。

### 1. Interface Layer

- WebSocket Gateway
- HTTP API
- Telegram / Discord / CLI
- Typed protocol

役割:
- ユーザー入力の受信
- セッションへのルーティング
- 承認要求の提示
- 実行結果の返却

### 2. Control Loop Layer

- Planner
- Policy Judge
- Executor
- Verifier
- Repair Controller
- Memory Curator

役割:
- タスクを計画する
- 実行の可否を判定する
- approved plan を実行する
- 結果を検証する
- 必要なら再計画する
- 記憶候補を審査する

### 3. Session / State Layer

- ADK Session
- ADK Session state
- Event history

役割:
- 実行中タスクの短期状態保持
- agent 間の一時データ共有
- 実行トレースの保存

### 4. Memory Lifecycle Layer

- Candidate Memory Store
- Conflict Detector
- Dedup Engine
- Promotion Engine
- Retrieval Planner

役割:
- 記憶候補の蓄積
- 重複・矛盾検出
- trust / confidence / sensitivity の評価
- promoted memory の選定
- task-conditioned retrieval

### 5. Persistence Layer

- ADK MemoryService
- SQLite / Postgres
- Artifact store
- Audit log

役割:
- 長期知識の保存
- 永続セッション
- 重い artifact の保存
- 監査ログの保管

---

## Role Model

単なる sub-agent 分割ではなく、責務と権限を分けた control system として構成する。

### Planner

責務:
- ユーザー要求を goal / constraints / subgoals / success criteria に変換する
- 実行計画の草案を生成する

入力:
- ユーザー入力
- relevant memory
- policy summary
- prior session context

出力:
- `temp:planner_draft`

権限:
- plan 作成
- read-only memory summary

禁止:
- shell 実行
- file write
- external mutation
- memory promote

---

### Policy Judge

責務:
- plan の危険度と許可可否を判定する
- required capabilities が妥当か判定する
- human approval が必要か判定する

入力:
- `temp:planner_draft`
- policy memory
- capability rules

出力:
- `plan:approved`
- `approval:status`
- `plan:risk_level`

権限:
- policy evaluate
- approval grant / deny

禁止:
- tool 実行
- artifact 編集

---

### Executor

責務:
- approved plan を実行する
- tool を呼び出す
- artifact を生成する

入力:
- `plan:approved`
- allowed capabilities
- runtime budget

出力:
- `temp:executor_outputs`
- `temp:artifact_refs`

権限:
- approved capabilities の範囲での tool 実行

禁止:
- policy override
- memory promote
- unrestricted actions

---

### Verifier

責務:
- success criteria を満たしているか判定する
- evidence と最終結果の整合性を確認する
- repair action を提案する

入力:
- `plan:approved`
- `temp:executor_outputs`
- `temp:artifact_refs`

出力:
- `verify:last_report`
- `temp:repair_patch`

権限:
- read-only trace / artifact access
- scoring / result evaluation

禁止:
- tool 実行
- file write
- memory write

---

### Repair Controller

責務:
- 失敗分類に応じて再試行戦略を決める
- 全やり直しか部分修復かを判断する
- repair count を管理する

入力:
- `verify:last_report`
- `repair:count`
- prior plan

出力:
- `temp:repair_patch`
- revised planner input

---

### Memory Curator

責務:
- session から記憶候補を抽出する
- dedup / contradiction / trust scoring を行う
- memory を promote / merge / reject する

入力:
- session events
- final result
- verification report
- existing memory matches

出力:
- `memory:candidate_ids`
- promoted memory records

権限:
- candidate read
- memory promote / merge / deprecate

禁止:
- shell
- browser
- arbitrary execution

---

## Control Loop Lifecycle

基本フローは以下。

```text
INTAKE
  ↓
PLANNER
  ↓
POLICY REVIEW
  ├─ deny → REVISE PLAN
  └─ approve → EXECUTE
                 ↓
               VERIFY
            ├─ pass → CURATE MEMORY → DONE
            ├─ partial → REPAIR → EXECUTE
            └─ fail → STOP / ESCALATE
```

### Detailed Flow

1. Interface Layer がユーザー要求を受け取る
2. Session を取得または生成する
3. Retrieval Planner が task に応じて relevant memory を集める
4. Planner が draft plan を生成する
5. Policy Judge が plan を審査する
6. 承認済み plan を Executor が実行する
7. Verifier が結果を success criteria に照らして評価する
8. 不十分なら Repair Controller が再実行方針を決める
9. 十分なら Memory Curator が記憶候補を抽出する
10. 審査済み memory を promoted memory として保存する
11. 最終結果を返却する

---

## State Model

ADK state は短期制御専用として使う。

### Persistent State Keys

| キー                          | 内容                       |
|------------------------------|---------------------------|
| `task:goal`                  | ユーザーの目標             |
| `task:constraints`           | 制約条件                   |
| `task:success_criteria`      | 成功判定基準               |
| `plan:current`               | 最新の計画草案             |
| `plan:approved`              | 承認済み計画               |
| `plan:risk_level`            | リスクレベル               |
| `approval:status`            | 承認状態                   |
| `verify:last_report`         | 最後の検証レポート         |
| `repair:count`               | リペアループ回数           |
| `memory:last_candidate_ids`  | 最後に抽出した候補ID群     |
| `memory:last_promoted_ids`   | 最後に昇格したメモリID群   |

### Temporary State Keys

| キー                          | 内容                       |
|------------------------------|---------------------------|
| `temp:retrieval_bundle`      | 検索結果束                 |
| `temp:planner_draft`         | Planner の草案             |
| `temp:executor_outputs`      | Executor の出力            |
| `temp:artifact_refs`         | 成果物参照                 |
| `temp:verification_inputs`   | Verifier への入力          |
| `temp:repair_patch`          | リペア提案                 |

### State Usage Principles

- `temp:` は invocation 中だけ使う
- 大きな payload は state に直接置かず artifact store に保存する
- state は traceable な構造化データだけ持つ
- 長期保存したい知識は state ではなく memory lifecycle に流す

---

## Plan Schema

Planner は構造化 plan を生成する。

```json
{
  "plan_id": "plan_001",
  "goal": "README を agent runtime 中心に再設計する",
  "constraints": [
    "既存機能は事実ベースで扱う",
    "OpenClaw 劣化コピーに見せない",
    "セキュリティと制御面を前に出す"
  ],
  "subgoals": [
    "既存 README の訴求軸を抽出する",
    "差別化可能な設計要素を特定する",
    "新しい見出し構成を作成する",
    "全文リライト案を生成する"
  ],
  "success_criteria": [
    {
      "name": "design_goals_explained_first",
      "criterion_type": "evidence",
      "description": "設計目標が先に説明される"
    }
  ],
  "required_capabilities": [
    { "name": "file.read", "mode": "read" },
    { "name": "memory.read", "mode": "read" }
  ],
  "risk_level": "low",
  "approval_status": "pending"
}
```

---

## Verification Model

Verifier は 0/1 判定ではなく criteria-based report を返す。

```json
{
  "plan_id": "plan_001",
  "status": "partial_pass",
  "overall_score": 0.81,
  "confidence": 0.87,
  "criterion_results": [
    { "name": "design_goals_explained_first", "passed": true, "score": 1.0 },
    { "name": "positioning_is_distinct", "passed": true, "score": 0.9 },
    { "name": "openclaw_dependency_language_reduced", "passed": false, "score": 0.4 }
  ],
  "failure_type": "insufficient_evidence",
  "repair_actions": [
    {
      "action_type": "regenerate_format",
      "description": "reduce repeated OpenClaw framing"
    }
  ]
}
```

### Failure Types

| タイプ                  | 説明                           |
|------------------------|-------------------------------|
| `tool_failure`         | ツール実行エラー               |
| `plan_failure`         | 計画自体の問題                 |
| `format_failure`       | 出力フォーマット不一致         |
| `insufficient_evidence`| 根拠不足                       |
| `policy_denied`        | ポリシー違反                   |
| `memory_conflict`      | メモリ矛盾検出                 |

---

## Repair Loop

repair は全面再実行ではなく、局所修復を基本とする。

### Repair Strategy

| 失敗タイプ              | 戦略                                       |
|------------------------|-------------------------------------------|
| `tool_failure`         | 同一 step の再試行 / fallback tool         |
| `plan_failure`         | Planner に部分再計画を依頼                 |
| `format_failure`       | 出力整形のみ再生成                         |
| `insufficient_evidence`| 追加取得・追加検証を実施                   |
| `policy_denied`        | capability を絞って plan を再提出          |
| `memory_conflict`      | Curator に conflict review を依頼          |

### Repair Limits

- `repair:max_attempts` - 上限回数
- `repair:count` - 現在の回数
- `repair:last_reason` - 最後の失敗理由

一定回数を超えたら停止または human escalation に移行する。

---

## Memory Architecture

Memory は会話履歴の dump ではなく、知識管理層として扱う。

### Memory Classes

#### 1. Episodic Memory

何が起きたかを記録する。

例:
- 2026-03-10 に README 方針を runtime 中心へ変更した
- GitHub repo review を行った

特徴:
- 時系列が重要
- 鮮度が重要
- TTL を持ちやすい

#### 2. Semantic Memory

比較的安定した事実を記録する。

例:
- boiled-claw は FastAPI ベースの gateway を持つ
- dynamic agent + MCP をサポートする

特徴:
- 抽象化された事実
- provenance が重要
- contradiction 管理が必要

#### 3. Procedural Memory

やり方や方針を記録する。

例:
- README は設計目標を先に出す
- 高リスク操作は approval を挟む

特徴:
- 行動パターンに効く
- task planning に有効

#### 4. Policy Memory

制約や承認条件を記録する。

例:
- public posting は human approval 必須
- shell mutation は restricted mode のみ許可

特徴:
- 常時適用
- 通常 memory より優先される

---

## Memory Lifecycle

### 1. Candidate Extraction

session 完了後、または重要イベント発生後に候補を抽出する。

抽出元:
- user direct statements
- verified results
- repeated patterns
- policy decisions
- successful procedures

### 2. Normalization

候補を canonical form に寄せる。
- 主語を明示
- 時制を揃える
- ノイズを落とす
- memory type を付与する

### 3. Dedup / Conflict Check

既存 memory と比較する。
- 同義重複
- 属性衝突
- stale 候補
- sensitivity mismatch

### 4. Trust Scoring

以下を元に trust を算出する。
- direct user statement か
- verified artifact 由来か
- repo / file evidence があるか
- model inference か
- recency
- contradiction history

### 5. Review

Curator が次を判断する。
- promote
- merge
- reject
- deprecate existing
- hold for review

### 6. Promotion

promote された記憶だけを long-term memory に保存する。

---

## Memory Candidate Schema

```json
{
  "candidate_id": "cand_001",
  "memory_type": "procedural",
  "content": "README は feature list より design goals を先に説明する",
  "source_session_id": "sess_123",
  "source_event_ids": ["evt_14", "evt_15"],
  "provenance": {
    "originator_type": "user",
    "capture_method": "direct_statement",
    "captured_at": "2026-03-10T10:20:00Z"
  },
  "confidence": 0.88,
  "trust_score": 0.91,
  "sensitivity": "internal",
  "ttl_seconds": null,
  "review_status": "candidate",
  "dedup_key": "readme_design_goals_first",
  "contradiction_refs": []
}
```

## Promoted Memory Schema

```json
{
  "memory_id": "mem_001",
  "memory_type": "semantic",
  "content": "boiled-claw uses FastAPI for its gateway server",
  "subject": "boiled-claw",
  "provenance": {
    "originator_type": "repo",
    "capture_method": "verified_file_read",
    "source_ref": "README.md",
    "captured_at": "2026-03-10T10:20:00Z"
  },
  "confidence": 0.95,
  "trust_score": 0.94,
  "sensitivity": "internal",
  "review_status": "promoted",
  "supersedes": [],
  "contradicts": []
}
```

---

## Retrieval Architecture

単純な類似検索ではなく task-conditioned retrieval を行う。

### Retrieval Inputs

- current goal
- task type
- actor role
- allowed sensitivity
- required confidence
- time horizon

### Retrieval Flow

1. Task classifier が task type を推定
2. Retrieval planner が必要 memory class を選択
3. 各 class ごとに検索を実行
4. re-ranker が relevance / freshness / trust で並べ替え
5. token budget に収まるよう pack する

### Example

README 改善タスクでは、以下を別々に引く。
- semantic: 現在のアーキテクチャ事実
- procedural: 過去の positioning 方針
- episodic: 直近のリポジトリ変更
- policy: 公開文面で避けるべき表現

---

## Policy Model

policy は memory の一種ではあるが、通常記憶とは別優先で扱う。

### Policy Types

- capability policy
- approval policy
- path policy
- network policy
- publication policy
- memory sensitivity policy

### Example Rules

- file write は workspace/* のみ許可
- public post は verify pass かつ human approval 必須
- browser action は allowlist domain のみ
- memory promote は curator のみ実行可

---

## ADK Integration Strategy

### Session / State

ADK Session を使って以下を保持する。
- plan 状態
- approval 状態
- verification 状態
- repair 状態
- temporary control data

### Event History

ADK event history を trace として利用する。

trace に残すもの:
- plan draft
- approval decision
- tool call summary
- verification report
- repair transition
- memory promotion result

### MemoryService

ADK MemoryService は **promoted memory の保存先** として使う。

注意:
- raw transcript はそのまま memory に入れない
- candidate review を通った知識だけ追加する
- memory class / trust / sensitivity は独自メタデータで補う

---

## Storage Responsibilities

| ストア                  | 保存対象                          | 用途                          |
|------------------------|----------------------------------|------------------------------|
| Session Store          | active sessions, state, events   | 短期実行継続性                |
| Candidate Memory Store | candidates, review status, conflicts | メモリライフサイクル処理     |
| Promoted Memory Store  | curated memory, provenance, trust | 検索・計画支援・パーソナライズ|
| Artifact Store         | tool outputs, screenshots, files | state 肥大化防止・監査       |

---

## Module Layout

```
src/
├── control_loop/
│   ├── planner.py
│   ├── policy_judge.py
│   ├── executor.py
│   ├── verifier.py
│   ├── repair.py
│   └── orchestration.py
├── memory_lifecycle/
│   ├── memory_schema.py
│   ├── candidate_store.py
│   ├── curator.py
│   ├── conflict_detector.py
│   └── retrieval_planner.py
└── runtime/
    ├── plan_schema.py
    ├── verification_schema.py
    ├── policy_schema.py
    ├── trace_schema.py
    └── state_keys.py
```

---

## Implementation Phases

### Phase 1: Control Loop MVP

- Planner / Executor / Verifier 分離
- plan JSON schema 導入
- success criteria based verification
- one-shot repair loop
- `temp:` state の活用

### Phase 2: Memory Lifecycle MVP

- candidate memory store 導入
- candidate → promote フロー追加
- promoted memory のみ long-term 化
- basic dedup

### Phase 3: Policy and Capability Layer

- Policy Judge 導入
- capability-aware tool execution
- risk scoring
- human approval routing

### Phase 4: Semantic Memory System

- episodic / semantic / procedural / policy の分離
- contradiction detection
- stale detection
- task-conditioned retrieval
- trust-aware re-ranking

---

## Success Criteria

### Control Loop

- plan が構造化されている
- approval 前に危険な execution が起きない
- result verification が必須になっている
- partial failure 時に局所修復できる

### Memory

- raw transcript と long-term memory が分離されている
- candidate review を通った記憶だけが昇格する
- provenance / trust / sensitivity が保持される
- retrieval が task-conditioned に行われる

### System

- state が肥大化しない
- trace が監査可能
- memory が矛盾で自己崩壊しない
- agent role ごとの権限が分かれている

---

## Positioning

boiled-claw v2 は、単なる multi-agent assistant ではない。

それは、
- plan-driven
- policy-aware
- verification-first
- repair-capable
- memory-curated

な **closed-loop agent runtime** である。

そして memory は単なる会話補助ではなく、
episodic / semantic / procedural / policy を備えた意味的な知識管理層として扱う。

この2つを組み合わせることで、boiled-claw は
「複数の agent がいる system」から
「経験を整理しながら改善する agent operating layer」へ進化する。
