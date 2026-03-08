# boiled-claw — Claude Code 設定

## ブランチ戦略

```
main
 └─ develop          # 統合ブランチ
      └─ feature/*   # 機能開発ブランチ
```

### ルール
- **feature → develop** : ローカルマージ
- **develop → main**    : 必ず PR で実施（直接マージ禁止）
- ブランチ名: `feature/<kebab-case>`

### 新機能の手順

```bash
# 1. develop を最新化
git checkout main && git pull
git checkout develop && git merge main

# 2. feature ブランチを develop から作成
git checkout -b feature/<name>

# 3. 実装・コミット
# ...

# 4. develop にマージ
git checkout develop
git merge feature/<name>

# 5. develop → main は PR
gh pr create --base main --head develop
```

## Skills → Agent スポーン方針

スキルが実行される際、root agent は `skill_spawn` を使って
そのスキルに最適化された動的 Agent を生成して実行する。

```
ユーザーリクエスト
  → skill_list でスキル確認
  → skill_spawn(name, task)
      → SKILL.md の内容を instruction に動的 Agent 生成
      → sessions_spawn_dynamic に委譲
  → subagents_list / subagents_steer / subagents_kill で管理
```

### 使い分け
| ツール | 用途 |
|---|---|
| `skill_spawn` | タスク実行（Agent 生成して委譲） |
| `skill_execute` | スキル内容確認・メタ情報取得のみ |
| `skill_list` | 利用可能スキル一覧確認 |
