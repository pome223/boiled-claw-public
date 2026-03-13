# Desktop Bridge v1 Contract

## Overview

Desktop Bridge v1 は、GUI automation を Host Bridge から論理分離するための最小 contract を定義する。

この段階では、まず **tool surface と request / response shape** を固定する。
実装は段階導入とし、view 系 capability を先に追加し、
macOS Accessibility / pointer control の本格実装は後続フェーズに回す。

Transport は Host Bridge と同じく MCP over SSE を前提にし、stdio でも同じ surface を提供する。

---

## Design Goals

### 1. Keep desktop access separate from shell access

GUI automation は shell の延長ではない。
approval、audit、権限モデルが別なので、Host Bridge に混ぜない。

### 2. Split view and control

screen / window / accessibility tree の取得と、
click / type / drag のような操作は別 capability として扱う。

### 3. Ship the contract before privileged implementation

先に route / approval / audit の名前と shape を固定し、
後から macOS 実装を安全に差し込めるようにする。

---

## Tool Surface

### Core

- `ping`
- `capabilities.list`

### Desktop view

- `desktop.view.screenshot`
- `desktop.view.windows`
- `desktop.view.frontmost_app`
- `desktop.ax.snapshot`

### Desktop control

- `desktop.control.launch_app`
- `desktop.control.focus_window`
- `desktop.control.click`
- `desktop.control.type`
- `desktop.control.hotkey`
- `desktop.control.drag`

---

## Request Context

すべての desktop tool は次の context を受け取る。

- `request_id`
- `session_id`
- `user_id`
- `agent_name`
- `approval_token`

Desktop Bridge 側は approval の最終判断をしない。
ただし将来的には `approval_token` の存在検証や replay 防止を追加できる shape にしておく。

---

## Result Model

v1 では、desktop tool は以下のどちらかを返す。

- 実装済み capability: `ok=true`
- 未実装 capability: `ok=false`, `error="not implemented"`

`desktop.control.click` と `desktop.control.type` は、
単純な座標入力に加えて Accessibility selector による targeting を許可する。
selector は次の scoping / matching field を持てる。

- `app_name`
- `window_id`
- `role`
- `title`
- `identifier`
- `value_contains`
- `index`

これにより、Gateway や routing 側は capability を先に認識できる一方、
実装済みと誤解して危険な操作を進めることを防ぐ。

---

## Capability Policy

推奨リスク分類:

- `desktop.view.windows`: low
- `desktop.view.frontmost_app`: low
- `desktop.view.screenshot`: medium
- `desktop.ax.snapshot`: medium
- `desktop.control.launch_app`: high
- `desktop.control.focus_window`: high
- `desktop.control.*`: high

推奨 approval:

- view 系は基本 approve
- control 系は常に approve

---

## Non-Goals for v1

- 高精度な ScreenCaptureKit 実装
- window move / resize 実装
- image understanding / OCR
- Gateway との実運用接続

v1 では low-level desktop primitives を先に実装し、
より高次の semantic targeting / OCR / vision-guided automation は後続フェーズに回す。
