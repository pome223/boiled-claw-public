# Host and Desktop Bridge Architecture

## Overview

boiled-claw の次段階では、Docker 内の gateway に host OS や GUI 操作まで背負わせない。
代わりに、**control plane** と **host capability surface** を分離した 3 層構成へ進める。

この設計は、OpenClaw の

- Gateway / node host / browser proxy
- macOS Companion app / PeekabooBridge

のような分離思想に影響を受けている。
ただし、ここで定義する API と責務分割は boiled-claw 向けに再構成した独自設計である。

---

## Design Goals

### 1. Keep the gateway unprivileged

Gateway は control plane に徹し、host OS の直接操作を行わない。

### 2. Move host execution out of Docker

shell、browser、file 操作は host 上の bridge に逃がす。
Docker 内 runtime は orchestration と approval に集中する。

### 3. Separate desktop control from shell access

GUI automation は shell の延長ではない。
Accessibility、screen capture、click、type は別 capability として扱う。

### 4. Make approval and audit first-class

高リスク capability は明示的に approval を要求し、すべての bridge 呼び出しを audit する。

### 5. Allow phased adoption

最初は Host Bridge に shell / browser / file だけを載せ、Desktop Bridge は後から追加できるようにする。

---

## Three-Layer Model

### 1. Gateway

実行場所:

- Docker container
- Web / HTTP / WebSocket endpoint

責務:

- session 管理
- transcript 永続化
- routing
- approvals
- audit event emission
- cron
- UI event stream
- control loop orchestration

保持するもの:

- routing_agent
- root_agent
- control_loop
- transcript store
- approval queue
- policy engine

やらないこと:

- host shell 実行
- host filesystem 直接操作
- host GUI 直接操作
- host browser 直接起動

位置づけ:

Gateway は **trusted control plane** であり、capability の直接実行者ではなく、
「誰が、何を、どこまで、どの権限で実行してよいか」を判断し記録する層である。

---

### 2. Host Bridge

実行場所:

- host OS 上の常駐プロセス
- Docker 外

責務:

- host shell 実行
- host filesystem 操作
- host browser automation
- stdio MCP の host-side 実行
- host process / network / git / toolchain へのアクセス

代表 capability:

- `host.shell.run`
- `host.file.read`
- `host.file.write`
- `host.file.list`
- `host.browser.navigate`
- `host.browser.extract_text`
- `host.browser.screenshot`
- `host.mcp.stdio.spawn`

やらないこと:

- routing
- conversation management
- approval policy の最終判断
- GUI の自由操作

位置づけ:

Host Bridge は **host execution plane** である。
Gateway から許可された capability を受け取り、host OS 上で実際の処理を行う。

---

### 3. Desktop Bridge

実行場所:

- host OS 上の高権限プロセス
- できれば Host Bridge から論理分離

責務:

- GUI automation
- Accessibility tree 取得
- screen capture
- window / app control
- keyboard / pointer input

代表 capability:

- `desktop.view.screenshot`
- `desktop.view.windows`
- `desktop.view.frontmost_app`
- `desktop.ax.snapshot`
- `desktop.control.launch_app`
- `desktop.control.click`
- `desktop.control.type`
- `desktop.control.hotkey`
- `desktop.control.drag`

前提権限:

- macOS Accessibility
- Screen Recording
- Automation / Apple Events

位置づけ:

Desktop Bridge は **high-trust desktop capability plane** である。
shell よりも危険で、誤操作の影響も大きいため、
approval と emergency stop を強く要求する。

---

## Why File Belongs to Host Bridge

ファイル操作は GUI automation ではなく host capability である。
そのため `file` は Desktop Bridge ではなく Host Bridge に置く。

理由:

- shell / git / build / editor workflow と近い
- GUI 権限を必要としない
- coding agent 的な利用では file と shell を一緒に扱うことが多い

---

## Capability Boundaries

### Gateway-side capabilities

- route decision
- approval request / resolution
- control loop state transition
- transcript persistence
- audit aggregation

### Host-side capabilities

- shell
- file
- browser
- stdio MCP

### Desktop-side capabilities

- screenshot
- window list
- app focus
- click / type / drag
- accessibility snapshot

重要なのは、**desktop capability を shell capability に埋め込まないこと** である。
`osascript` で GUI が動かせるとしても、権限モデルと approval モデルは分けるべきである。

---

## Routing Model

### Normal chat

通常チャットは `routing_agent` を通る。
route 先は次のいずれか:

- `root_agent`
- `control_loop`
- specialist
- dynamic agent

### Host capability request

shell / file / browser の要求は、最終的に Host Bridge へ送られる。

例:

- 「この repo のテストを実行して」 → `host.shell.run`
- 「このファイルを開いて修正して」 → `host.file.read` / `host.file.write`
- 「このページを読んで要約して」 → `host.browser.navigate` / `extract_text`

### Desktop capability request

GUI 操作は Desktop Bridge へ送られる。

例:

- 「Safari を開いて」 → `desktop.control.launch_app`
- 「このボタンを押して」 → `desktop.control.click`
- 「画面の内容を見て」 → `desktop.view.screenshot`

高リスクな desktop task は原則 `control_loop` に上げる。

---

## Approval Model

### Low-risk host actions

例:

- 読み取り専用の file access
- 安全な web navigation
- 許可済みの shell read command

扱い:

- policy allow なら自動実行可能

### Medium-risk host actions

例:

- file write
- package install
- long-running shell
- git mutation

扱い:

- approval required

### High-risk desktop actions

例:

- click
- key input
- app switching
- drag and drop
- GUI フローの自動実行

扱い:

- approval required
- できれば step-by-step execution
- emergency stop 必須

---

## Audit Model

すべての bridge 呼び出しは Gateway 側に監査イベントとして返す。

最低限残すもの:

- capability name
- request id
- session id
- user id
- agent name
- target resource
- redacted args
- start time
- end time
- result
- approval id

UI には次のようなイベントを出す:

- `tool.start`
- `tool.result`
- `system.event`
- `approval.requested`
- `approval.resolved`

---

## Transport Options

Bridge との接続方式は次のいずれかを想定する。

### Option A: HTTP / SSE bridge

特徴:

- 実装しやすい
- Gateway と分離しやすい
- Docker 越しでも扱いやすい

向いている用途:

- Host Bridge v1

### Option B: MCP bridge

特徴:

- ADK / agent runtime と相性が良い
- capability surface を tool として自然に公開できる

向いている用途:

- Host Bridge の一部
- browser / desktop の専用 surface

### Option C: local companion app

特徴:

- macOS 権限を扱いやすい
- GUI automation と相性が良い

向いている用途:

- Desktop Bridge

推奨:

- Host Bridge は HTTP か MCP で開始
- Desktop Bridge は companion app 的構成を将来的に検討

---

## Phase Plan

### Phase 1: Host Bridge

実装対象:

- `host.shell.run`
- `host.file.read`
- `host.file.write`
- `host.file.list`
- `host.browser.navigate`
- `host.browser.extract_text`
- `host.browser.screenshot`

目的:

- Docker 内 gateway から host OS の作業を切り離す
- coding / research / browser タスクを実用化する

### Phase 2: Gateway integration

実装対象:

- Host Bridge client
- capability gating
- approval wiring
- audit wiring
- `tool.start` / `tool.result` event forwarding

目的:

- Gateway から bridge を第一級 capability として扱う

### Phase 3: Desktop Bridge

実装対象:

- screenshot
- window list
- frontmost app
- app launch
- click
- type
- hotkey

目的:

- GUI assistance を追加する
- screen-aware task を扱えるようにする

### Phase 4: Verified desktop automation

実装対象:

- desktop task を `control_loop` に統合
- verify step
- recovery / retry
- emergency stop

目的:

- GUI 自動操作を「なんとなく動かす」のではなく、制御可能な runtime に載せる

---

## Recommended Initial Implementation

最初に作るべき最小構成は次の通り。

1. Docker の Gateway はそのまま残す
2. host OS 上に Host Bridge を常駐させる
3. shell / file / browser を Host Bridge へ移す
4. Gateway からの bridge 呼び出しを approval と audit に接続する
5. Desktop Bridge は後から追加する

この順にすることで、最小の変更で実用性を大きく上げられる。

---

## Non-Goals

初期段階では次はやらない。

- full autonomous GUI driving
- desktop 操作の完全無承認実行
- Docker container から host GUI を直接触る設計
- shell の中に GUI automation を埋め込むこと

---

## Summary

boiled-claw の次のアーキテクチャは、次の 3 層で整理するのが自然である。

- **Gateway**: control plane
- **Host Bridge**: host shell / file / browser / stdio MCP
- **Desktop Bridge**: GUI / Accessibility / screen / input

この分離は複雑化ではなく、責務の明確化である。
最初は Host Bridge のみで十分に価値があり、Desktop Bridge はその後に安全に追加できる。
