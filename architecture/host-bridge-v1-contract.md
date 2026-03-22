# Host Bridge v1 Contract

## Overview

Host Bridge v1 は、Gateway から host OS の capability を安全に呼び出すための最小 surface を定義する。

この段階では次を対象にする。

- `ping`
- `capabilities.list`
- `host.shell.run`
- `host.file.read`
- `host.file.write`
- `host.file.list`

`browser` は v1 の後続 capability とし、同じ envelope に載せて追加する。

Transport は MCP over SSE を前提とする。
ただし、開発とテストのため stdio transport でも同じ tool surface を提供する。

---

## Design Goals

### 1. Define the contract before broad integration

Gateway と Host Bridge の偶発的な密結合を避けるため、
最初に request / response shape を固定する。

### 2. Keep v1 narrow

最初から file / browser / desktop まで広げず、
`host.shell.run` を縦に通すことを優先する。

### 3. Preserve audit and approval context

bridge 呼び出しは execution だけでなく、
だれが、どの session で、どの agent として呼んだかを保持する。

---

## Tool Surface

### `ping`

用途:

- Bridge の生存確認
- version / implementation 名の確認

入力:

- なし

出力:

- `ok`
- `service`
- `version`
- `transport`

---

### `capabilities.list`

用途:

- Bridge が現在提供している capability 一覧を返す

入力:

- なし

出力:

- `capabilities[]`
  - `name`
  - `risk`
  - `requires_approval`
  - `description`
  - `implemented`

---

### `host.shell.run`

用途:

- host OS 上で shell command を実行する

入力:

- `request_id`
- `session_id`
- `user_id`
- `agent_name`
- `approval_token`
- `command`
- `timeout_seconds`
- `cwd`

出力:

- `ok`
- `stdout`
- `stderr`
- `return_code`
- `timed_out`
- `error`

---

### `host.file.read`

用途:

- host OS 上の guarded file を読み込む

入力:

- `request_id`
- `session_id`
- `user_id`
- `agent_name`
- `approval_token`
- `path`

出力:

- `ok`
- `path`
- `content`
- `size`
- `error`

---

### `host.file.write`

用途:

- host OS 上の guarded file に書き込む

入力:

- `request_id`
- `session_id`
- `user_id`
- `agent_name`
- `approval_token`
- `path`
- `content`

出力:

- `ok`
- `path`
- `size`
- `success`
- `error`

---

### `host.file.list`

用途:

- host OS 上の guarded directory を列挙する

入力:

- `request_id`
- `session_id`
- `user_id`
- `agent_name`
- `approval_token`
- `path`

出力:

- `ok`
- `path`
- `entries`
- `error`

---

## Request Context

`host.shell.run` では、以下の request context を引き回す。

- `request_id`: 単一呼び出しの識別子
- `session_id`: Gateway session
- `user_id`: effective user
- `agent_name`: 呼び出し元 agent
- `approval_token`: Gateway が発行した approval 証跡

v1 では `approval_token` は optional とするが、
将来は medium / high risk capability で必須化する前提で残しておく。

---

## Security Model

Host Bridge v1 は、Gateway の approval の代替をしない。
最終的な allow / deny は Gateway 側が持つ。

ただし Bridge 側でも最低限の安全策は入れる。

- shell metacharacter を shell 解釈しない
- `subprocess_exec` 相当で実行する
- ブロック対象コマンドを拒否する
- timeout を強制する

つまり v1 は

- **Gateway**: policy and approval
- **Host Bridge**: guarded execution

の分担とする。

---

## Capability Metadata

`capabilities.list` は最低限次を返す。

- `name`
- `risk`
- `requires_approval`
- `description`
- `implemented`

`risk` は次の 3 値。

- `low`
- `medium`
- `high`

v1 では次の risk を使う。

- `host.shell.run` = `medium`
- `host.file.read` = `low`
- `host.file.write` = `medium`
- `host.file.list` = `low`

---

## Future Compatibility

v1 の envelope に、次の capability をそのまま追加できる形にしておく。

- `host.browser.navigate`
- `host.browser.extract_text`
- `host.browser.screenshot`

将来的に Desktop Bridge 側では別 namespace を使う。

- `desktop.view.*`
- `desktop.control.*`

---

## Test Targets

v1 で最低限通すべきテストは次の通り。

1. `ping` が成功する
2. `capabilities.list` に shell / file capability が含まれる
3. `host.shell.run` で安全な command が成功する
4. `host.shell.run` で blocked command pattern が拒否される
5. `host.file.read` が成功する
6. `host.file.write` が成功する
7. `host.file.list` が成功する
8. stdio transport でも `tools/list` と `tools/call` が通る

---

## Summary

Host Bridge v1 は、MCP over SSE/stdio を transport とする最小 contract であり、
まず `host.shell.run` と `host.file.*` を end-to-end で通すことを目的にする。
