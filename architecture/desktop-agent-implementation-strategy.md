# Desktop Agent Implementation Strategy

## Position

boiled-claw で GUI を自由に扱う desktop agent を本気で作るなら、
**MCP server を中核にするのではなく、native desktop companion を中核にする**のが最適である。

MCP は transport adapter としては有用だが、

- OS 権限の取得
- Accessibility API への接続
- screen capture
- pointer / keyboard injection
- window observation
- emergency stop

の中心には向いていない。

特に macOS では、GUI automation の品質は
**AppleScript をどれだけ叩けるか**ではなく、
**Accessibility / ScreenCaptureKit / CGEvent をどれだけ安定して扱えるか**
で決まる。

そのため、Desktop agent の中核は次であるべきだ。

- macOS native companion
- local privileged runtime
- typed desktop primitives
- Gateway からは thin client で呼ぶ

---

## Recommendation

### Best architecture

最適構成は 4 層で考える。

1. `Gateway`
2. `Host Bridge`
3. `Desktop Companion`
4. `Desktop Control Loop`

### 1. Gateway

責務:

- routing
- session / transcript
- approval
- audit aggregation
- control loop orchestration

やらないこと:

- host shell 実行
- GUI 直接操作

### 2. Host Bridge

責務:

- shell
- file
- browser
- Desktop Companion への proxy

位置づけ:

Host Bridge は host capability surface を持つが、
GUI automation の本体は持たない。
Desktop 操作は `desktop client adapter` を通して companion へ渡す。

### 3. Desktop Companion

責務:

- macOS Accessibility API
- ScreenCaptureKit screenshot
- NSWorkspace / window metadata
- CGEvent / keyboard / pointer injection
- foreground app observation
- emergency stop

位置づけ:

Desktop Companion は GUI automation の **実行本体** である。
boiled-claw の desktop capability はここに実装する。

### 4. Desktop Control Loop

責務:

- multi-step GUI task の plan / act / verify
- screenshot / AX tree / window state による検証
- risky action 前の approval

位置づけ:

Desktop Companion は primitive executor に徹し、
「Safari を開いてログインしてこの画面まで進む」のような手順管理は
Gateway 側の control loop が持つ。

---

## Why MCP Should Not Be The Core

MCP を中核にしない理由は 5 つある。

### 1. GUI automation は transport より runtime が本質

必要なのは RPC 名ではなく、

- permission lifecycle
- app lifecycle
- state observation
- failure recovery

である。

MCP は呼び出し面には便利だが、
これらの OS 固有課題の解決には直接効かない。

### 2. macOS permissions は native app のほうが扱いやすい

Accessibility / Screen Recording / Automation は
署名済みの app / helper / launch agent に寄せたほうが安定する。

Python process を ad-hoc に立てて権限を取るより、
native companion のほうが TCC 周りの挙動が読みやすい。

### 3. AX tree と UI element targeting は native 実装のほうが自然

AXUIElement を辿る実装は Swift / Objective-C 側のほうが素直で、
イベント監視や observer 登録もやりやすい。

### 4. Pointer / keyboard injection は厳格に分けたい

`desktop.control.click` や `desktop.control.type` は、
shell や browser automation より明らかに危険である。
Host Bridge と分けた companion に閉じ込めたほうが境界が明確になる。

### 5. MCP はあとから被せられる

中核を native desktop runtime にしておけば、
必要に応じて

- local HTTP
- Unix domain socket
- XPC
- MCP adapter

のどれでも被せられる。
逆に MCP server を中核にすると、runtime 設計が transport に引っ張られやすい。

---

## Recommended Core Design

### Native companion first

まずは **macOS first** で割り切る。
cross-platform abstraction を最初から作らない。

### Language strategy

言語戦略は段階的に取る。

- Phase 0-1: `pyobjc` を使った Python companion / client で高速に進める
- Phase 2+: input injection や常駐安定性の要求が強くなったら Swift companion を検討する

最初から Swift を前提にしない理由は、

- Python 側の contract test を流用しやすい
- Host Bridge / Gateway との統合速度が高い
- view-only capability を早く通せる

からである。

最初の実装対象:

- `desktop.view.windows`
- `desktop.view.frontmost_app`
- `desktop.view.screenshot`
- `desktop.control.launch_app`
- `desktop.control.focus_window`
- `desktop.control.click`
- `desktop.control.type`
- `desktop.control.hotkey`

後回し:

- drag
- scroll
- OCR
- image matching
- vision-model-guided clicking

### Typed primitives, not free-form shell

Desktop 操作はすべて typed primitive にする。

悪い例:

- `osascript` を自由入力で叩く
- shell から GUI を直接動かす

良い例:

- `launch_app(bundle_id)`
- `list_windows()`
- `focus_window(window_id)`
- `click(x, y, button)`
- `type_text(text)`
- `press_hotkey(keys)`

### Semantic target before raw coordinate

coordinate click は最後の手段にする。

優先順位:

1. accessibility element target
2. window target + relative point
3. absolute coordinate

これにより GUI 操作の壊れやすさを下げる。

---

## Proposed Process Boundary

### Preferred process model

#### A. Gateway

Docker 内。control plane。

#### B. Host Bridge

host OS 上の Python process。
Gateway からの shell / file / browser / desktop proxy を受ける。

#### C. Desktop Companion

host OS 上の native app or helper。
Accessibility / screen / input を担当する。

Host Bridge は Desktop Companion と通信するだけに留める。

### Preferred local transport

Desktop Companion への接続は、
最初は **localhost HTTP or Unix domain socket** がよい。

理由:

- 実装が単純
- ログが取りやすい
- テストしやすい
- Python client から扱いやすい

最終的に macOS app 化を進めるなら XPC も候補になるが、
Phase 1 では不要。

### Where MCP fits

MCP は 2 通りの使い方に限定する。

1. Gateway / Host Bridge から Desktop Companion を呼ぶ adapter
2. 将来の外部連携向け surface

つまり、**MCP は core runtime ではなく compatibility layer** とする。

---

## Safety Model

### 1. Split view and control

別 namespace と approval policy を維持する。

- `desktop.view.*`
- `desktop.control.*`

### 2. Approval is action-scoped

approval は task 全体ではなく、
危険な action 単位で評価する。

例:

- screenshot: medium
- click/type/hotkey: high
- app launch: medium or high

### 3. Verify target before action

control 系 action の直前に少なくとも 1 つ確認する。

- frontmost app
- window id
- AX element id
- screenshot hash / small region match

### 4. Emergency stop

Desktop Companion には必ず emergency stop を持たせる。

必要なもの:

- global abort flag
- in-flight action cancellation
- pointer / key injection の停止

### 5. Strong audit trail

最低限残すもの:

- request_id
- user_id
- session_id
- action name
- target app/window
- approval token presence
- result
- optional screenshot artifact ref

---

## Recommended Implementation Order

### Phase 0: Companion skeleton

先に作るもの:

- `desktop client` interface
- `Desktop Companion` skeleton
- localhost transport
- `ping`
- `capabilities.list`

この段階では MCP 不要。

### Phase 1: View-only desktop runtime

実装:

- `desktop.view.windows`
- `desktop.view.frontmost_app`
- `desktop.view.screenshot`

目的:

- permission / app lifecycle / screenshot artifact 管理を先に固める

### Phase 2: Safe control primitives

実装:

- `desktop.control.launch_app`
- `desktop.control.focus_window`
- `desktop.control.hotkey`
- `desktop.control.click`
- `desktop.control.type`

条件:

- approval
- emergency stop
- audit
- frontmost verification

### Phase 3: AX snapshot and semantic targeting

実装:

- `desktop.ax.snapshot`
- element targeting
- element_id based click / type

この段階で GUI automation の実用性が大きく上がる。

### Phase 4: Control loop integration

実装:

- desktop task を `control_loop` に route
- step verify
- retry / repair

ここで初めて「自由に GUI を触る agent」に近づく。

---

## What We Should Not Do

やってはいけない方向は明確である。

### 1. Put GUI control into shell

`osascript` や shell command を desktop capability の本体にしない。
それは fallback にはなっても、設計の中心にはならない。

### 2. Make Gateway privileged

Gateway に host GUI 権限を持たせない。

### 3. Start with computer vision only

pixel-only agent を最初に作らない。
まずは window / AX / screenshot の structure を使う。

### 4. Over-abstract for non-macOS too early

最初から Windows / Linux まで一般化しない。
macOS first で成功させてから広げる。

### 5. Treat MCP as the architecture

MCP は接続方式であって、desktop runtime そのものではない。

---

## Proposed Repository Shape

### New desktop runtime area

候補:

- `src/desktop/client.py`
- `src/desktop/policy.py`
- `src/desktop/runtime.py`
- `src/desktop/models.py`
- `src/desktop/artifacts.py`

### Native companion

将来的には repo 内に別ディレクトリを切る。

候補:

- `desktop-companion-macos/`

中身の想定:

- Swift package or Xcode project
- AX access
- ScreenCaptureKit
- input injection
- local HTTP / socket server

### Existing bridge role after adoption

- `Host Bridge`: shell / file / browser + desktop proxy
- `Desktop Bridge`: optional MCP adapter or compatibility shim

---

## Immediate Next Step

次にやるべきことは Desktop Bridge の MCP server を厚くすることではない。

最初の実装単位はこれである。

1. `DesktopClient` Python interface を作る
2. `FakeDesktopClient` で contract test を作る
3. `pyobjc` 前提の localhost `Desktop Companion` skeleton を作る
4. view-only capability を先に通す

この順なら、MCP を使うかどうかは後から選べる。

---

## Final Recommendation

boiled-claw の desktop agent の最適解は、

**Gateway + Host Bridge + native Desktop Companion + control loop**

である。

MCP server は必要なら adapter として乗せる。
しかし GUI automation の中核は、
**native desktop runtime と typed desktop primitives** に置くべきである。
