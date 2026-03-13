# Desktop Bridge v1 Contract

## Overview

Desktop Bridge v1 は、GUI automation を Host Bridge から論理分離するための最小 contract を定義する。

この段階では、**tool surface と request / response shape の固定**だけを先に行い、
実際の macOS Accessibility / Screen Recording / pointer control までは実装しない。

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

v1 skeleton では、desktop tool は以下のどちらかを返す。

- 実装済み capability: `ok=true`
- skeleton / 未実装: `ok=false`, `error="not implemented"`

これにより、Gateway や routing 側は capability を先に認識できる一方、
実装済みと誤解して危険な操作を進めることを防ぐ。

---

## Capability Policy

推奨リスク分類:

- `desktop.view.windows`: low
- `desktop.view.frontmost_app`: low
- `desktop.view.screenshot`: medium
- `desktop.ax.snapshot`: medium
- `desktop.control.*`: high

推奨 approval:

- view 系は基本 approve
- control 系は常に approve

---

## Non-Goals for v1

- macOS Accessibility API 実装
- Screen Recording 実装
- pointer / keyboard injection 実装
- window move / resize 実装
- image understanding / OCR
- Gateway との実運用接続

v1 は **skeleton only** とする。
