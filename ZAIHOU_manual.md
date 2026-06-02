# ZAIHOU 運用マニュアル v1.0

対象: ZAIHOU.py（N225マイクロ先物 自動売買 系統①③④⑤）
作成: 2026-06-02

---

## 1. 起動時

### ① データ準備
- CPI イベントカレンダーを読み込む（シグナル判定で使用）
- `open_positions.json` から前回保有していたポジション情報を読み込む
- `micro_dry_log_all.csv` から当月の損益を集計し、月次 DD 停止状態を復元する
- `micro_5min.csv` から過去 300 本分の 5 分足データを読み込む（ウォームアップ）
  - データが 300 本未満の場合はエントリーを無効化して起動する

### ② API 接続
- kabuステーション API にパスワードでトークンを取得する
- 銘柄コードを取得する（NK225マイクロ先物のみ。取得失敗時は銘柄登録も失敗し起動を中止する）
- 翌限月の銘柄コードも取得し、バー収集対象として登録する
- 銘柄を API 登録銘柄リストに登録する（失敗時は起動を中止する）
- WebSocket を起動し、リアルタイム価格配信を受信開始する

### ③ ポジション整合チェック（本番モードのみ）
- ブローカーの `/positions` API を呼び出し、実際の保有建玉一覧を取得する
- `open_positions.json` に記録されている HoldID がブローカー側に存在しない場合、  
  「停止中に約定済み」と判断してそのポジションをリストから除外する
- 確認できないポジション（HoldID なし）は生存扱いで残す

### ④ SL/TP 再発注（本番モード・ポジションあり時のみ）
- 保有中のポジション全件に対し、現在のセッションに合わせた SL・TP 注文を再発注する
- これにより再起動前の SL/TP 注文が無効化されていても保護が回復する

---

## 2. メインループ（0.4 秒ごと）

起動完了後は以下を繰り返す。

- 板情報（現在値・Bid・Ask）を取得する
- ポジションがあれば → ポジション監視を実行する（→ 3章）
- エントリー条件を判定する（→ 4章）
- 5 分ごとに 5 分足 CSV を保存する
- 60 秒ごとにブローカー整合チェックを実行する（本番のみ）
- 16:45・8:00 にセッション切替の SL/TP 再発注を行う
- 毎時 0 分と 8:30・16:45 に Discord へポジション状況を報告する
- 毎時 0 分にハートビート通知を Discord へ送る

---

## 3. ポジション保有中の監視

ポジション保有中は 0.4 秒ごと（WebSocket tick 受信時も）に以下を確認する。

板情報から現在値を取得し、各ポジションについて順番に判定する。

### TP 到達
- long ポジションで `現在値 >= TP価格 + 5円`、または short で `現在値 <= TP価格 - 5円`
- 本番モード: `/positions` でポジションが消滅していれば TP 約定済みと判断し、  
  SL 注文をキャンセルして損益を記録する
- ポジションがまだ残っていれば次の tick で再確認する

### SL 到達
- long ポジションで `現在値 <= SL価格`、または short で `現在値 >= SL価格`
- 本番モード:
  - SL 注文 ID があれば約定確認をポーリング（最大 5 秒）
  - 約定確認できた場合 → SL 価格で損益記録、TP 注文をキャンセル
  - タイムアウト時 → `/positions` でポジション確認
    - ポジションが消滅していれば「SL 約定済み（価格不明）」として板価格で記録し Discord に警告
    - ポジションが残っていれば「SL 未約定」として TP をキャンセルし成行で緊急返済する

---

## 4. エントリー判定

毎ポーリングで確認バー（最新の確定 5 分足）が更新されていれば以下を実行する。

### スキップ条件（いずれかに該当する場合はエントリーしない）
- 月次 DD 停止中
- 同一バー時刻でシグナルチェック済み（重複防止）
- バーが前のセッションのもの（セッション跨ぎ）
- バーが現在時刻より 10 分以上古い（陳腐化）
- 金曜 5:00〜6:00 のエントリー禁止時間帯
- ウォームアップ残本数が残っている（最大 26 本）

### シグナル判定
- 系統①③: `check_s1_s3()` でシグナルを確認する（①=long、③=short）
- 系統④: `check_s4()` でシグナルを確認する（long のみ）
- 系統⑤: `check_s5()` でシグナルを確認する（short のみ）

### エントリー実行（シグナルあり）
1. 同じ系統に逆方向ポジションがあれば成行で決済する（逆ポジ決済）
2. エントリーを実行する（→ 5章）

---

## 5. エントリー実行

### DRY_RUN モード
実際の発注は行わず、現在の板価格でポジションをリストに追加して終了する。

### 本番モード
1. エントリー前に `/positions` を確認して既存建玉の ID 一覧を取得する  
   取得失敗時はエントリーをスキップする（機会損失を許容）
2. 成行発注する
3. 約定確認をポーリングする（最大 10 秒）  
   タイムアウト時は注文をキャンセルしてスキップする
4. 実約定価格をもとに SL 価格・TP 価格を再計算する
5. `/positions` を再取得して新規建玉の HoldID（ExecutionID）を特定する
6. SL 逆指値注文を発注する（HoldID 指定、取得失敗時は FIFO で代替）
7. TP 指値注文を発注する
8. SL 注文の受付状態を確認する
   - SL 非アクティブ確定 → TP キャンセル・成行緊急返済してポジション未登録で終了
   - SL 状態不明 → 警告 Discord 通知のみで継続
9. ポジションをリストに追加して `open_positions.json` に保存する

---

## 6. TIME 決済（MAX_HOLD）

エントリーから経過した 5 分足の本数が系統ごとの上限を超えた場合に決済する。

- 本番モード: SL・TP 注文を両方キャンセルしてから成行返済する
  - キャンセルが一方でも失敗した場合は緊急フラット（→ 8章）を実行する
  - キャンセル後にポジションが消滅していれば「SL/TP が先に約定済み」と判断しスキップする
  - 成行返済の約定が確認できなかった場合は Discord に緊急警告を出す

---

## 7. 強制決済

以下の条件のいずれかを満たす場合に強制決済を行う（TIME 決済と同じ手順）。

| 条件 | 対象 |
|---|---|
| 現在時刻が 15:40〜17:00 または 5:55〜8:45 | 全系統（セッション終了） |
| 土曜 6:00 以降 | 全系統（金曜夜間終了） |
| 23:50 以降 | 系統①のみ（夜間終了） |

セッション終了強制決済は、トリガーされた時刻を記録して 1 回のみ実行する。

---

## 8. 緊急フラット

SL・TP のキャンセルに失敗した場合に実行する最終手段。

1. `/positions` でブローカー側のネットポジション（買い枚数－売り枚数）を確認する
2. ネットポジションが 0 でなければ逆方向に成行発注してフラット化する
3. 発注失敗時は「手動決済要」として Discord に最緊急アラートを送る

---

## 9. SL/TP 再発注（セッション切替）

先物は日中・夜間でセッション（Exchange コード）が異なり、注文はセッション内でのみ有効。

- **16:45**（夜間セッション前気配）: 全ポジションの SL・TP を Exchange=24 で再発注
- **8:00**（日中セッション前気配）: 全ポジションの SL・TP を Exchange=23 で再発注

手順: 旧注文キャンセル → 新注文発注 → 受付確認（失敗時は Discord 警告）

---

## 10. DD 停止

月次損益（円）が DD 上限（デフォルト -300,000 円）を下回った場合。

- `monthly_stopped = True` に設定し、以降のエントリーを全系統でブロックする
- ポジション決済は引き続き実行する（保有中ポジションは監視・決済を継続する）
- 翌月 1 日以降の最初のバー確認時に月次リセットして自動再開する

---

## 11. ブローカー整合チェック（reconcile）

本番モードでポジションがある場合、60 秒ごとに内部状態とブローカーを突合する。

| 不一致パターン | 対応 |
|---|---|
| ブローカー側にポジションなし | SL/TP キャンセル → 内部ポジションをクリア |
| 内部と逆方向のポジション | 緊急成行フラット |
| 枚数が合わない | Discord 警告のみ |

---

## 12. 異常時の動作

### 認証エラー（401）
- 自動でトークンを再取得し、銘柄を再登録して直前のリクエストをリトライする
- 再取得のクールダウン: 10 秒
- 認証エラーが **5 回連続** した場合は安全終了する

### 通信エラー（タイムアウト・接続失敗）
- リクエストは None を返す
- 各処理がそれぞれの判断で継続・スキップ・再試行を選択する

### HoldID 取得失敗
- SL/TP 注文を `ClosePositionOrder=0`（FIFO）で代替発注する
- 複数ポジション保有時に意図しない建玉が返済されるリスクがある

---

## 13. 終了時

### 正常終了（週末）
- 土曜 6:00 以降にポジションが残っていれば強制決済を実行する
- 5 分足データを CSV に保存する
- Discord に「週末終了」と本日の損益を通知する

### 手動停止（Ctrl+C）
- 5 分足データを CSV に保存する
- Discord に「手動停止」を通知する
- ポジションは `open_positions.json` に保存済みのまま残る（次回起動時に復元）

### 例外終了
- スタックトレースをログと Discord に送信する
- ポジションは保存済みのまま残る

---

## 14. 既知の問題点

| Bug | 発生場面 | 症状 |
|---|---|---|
| Bug3 | 再起動時 SL/TP 再発注 | HoldID が無効で SL/TP 全滅 |
| Bug4 | 再起動後 | state.json のゴーストポジションを復元 |
| Bug5 | 認証エラー連続 | 5 回で安全終了（指数バックオフなし） |

---

## 付録A: ファイル構成（リファクタリング進行中）

最終更新: Phase 8 完了（2026-06-02）

### Phase 進捗

| Phase | ファイル | 内容 | 状態 |
|---|---|---|---|
| 1 | zh_config.py | 定数・設定値のみ | ✅ 完了 |
| 2 | zh_utils.py | 純粋関数（log / send_discord 等） | ✅ 完了 |
| 3 | zh_api.py | 認証・API通信・銘柄管理 | ✅ 完了 |
| 4 | zh_bar.py | バーデータ・WebSocket・ウォームアップ | ✅ 完了 |
| 5 | zh_order.py | 発注・約定確認・SL/TP発注 | ✅ 完了 |
| 6 | zh_monitor.py | 監視・決済・reconcile・replace | ✅ 完了 |
| 7 | zh_entry.py | シグナル判定・エントリー実行 | ✅ 完了 |
| 8 | ZAIHOU.py | main() のみ（起動・ループ制御） | ✅ 完了 |

### 状態変数の所有モジュール（現在）

| 変数 | 現在の所有モジュール | 最終的な所有モジュール |
|---|---|---|
| `token` `SYMBOL` `EXCHANGE` | zh_api.py | zh_api.py |
| `_collect_symbols` `_bar_state` | zh_api.py（一時避難） | zh_bar.py（Phase 4 後に本来はここ） |
| `consecutive_auth_errors` `last_reauth_time` | zh_api.py | zh_api.py |
| `completed_bars` `current_bar` `last_cum_vol` | zh_bar.py | zh_bar.py |
| `warmup_remaining` `can_trade` | zh_bar.py | zh_bar.py |
| `positions` `day_pnl` `trade_log` | zh_monitor.py | zh_monitor.py |
| `monthly_pnl_yen` `monthly_stopped` `monthly_ym` | zh_monitor.py | zh_monitor.py |
| `_positions_lock` | zh_monitor.py | zh_monitor.py |
| `last_signal_bar_time` `s4_last_bar` `s5_last_bar` `cpi_df` | zh_entry.py | zh_entry.py |

### import の方向（循環 import 禁止）

```
zh_config
    ↑
zh_utils
    ↑
zh_api ←── zh_bar（_bar_state / SYMBOL の参照）
    ↑           ↑
    └───────────┘
         ↑
     ZAIHOU.py（全モジュールを orchestrate）
```

### Phase 8 完了後の ZAIHOU.py

ZAIHOU.py は `main()` のみ（起動・ループ制御）。リファクタリング完了。

### 設計判断メモ（次回セッションで参照）

- **_collect_symbols / _bar_state が zh_api.py にある理由**: `register_symbol()` と `_init_collect_symbols()` が API 通信と bar 状態を両方触るため、循環 import を避けるため zh_api.py が一時所有。
- **_bar_state_lock を新設した理由**: WebSocket スレッドとメインループ間で `_bar_state` を保護するため。`_positions_lock`（positions 保護用）とは別ロックにすることでロック競合を避ける。
- **global 宣言に注意**: Python の `global` は dotted name（`zh_bar.foo` 等）を受け付けない。replace_all で変数名を置換する際は事前に `global` 宣言行を確認して手動修正する。
- **zh_monitor.py が ZAIHOU_signals に依存する理由**: `_SL_MAP` / `_TP_MAP` / `_MH_MAP` の参照のため。Phase 7 以降で zh_entry.py が持つべき情報だが、現時点では zh_monitor.py が保有（設計懸念 D1）。

---

## 付録B: レビュー記録（Phase別）

### Phase 5 レビュー（zh_order.py / 2026-06-02）

#### 要カブコム確認事項（保留中・本番移行前に要確認）

| No | 確認内容 | 影響 | 状態 |
|---|---|---|---|
| C1 | `/orders` Details.RecType の定義値（約定明細の値は 8 か？） | wait_for_fill() が常にタイムアウトするリスク | ⏸ 保留（API仕様書に定義なし・実機確認要） |
| C2 | ReverseLimitOrder.AfterHitOrderType の定義値（1=成行か？） | SL 逆指値発注の動作が意図通りか | ✅ サンプル確認（AfterHitOrderType:1 + AfterHitPrice:0 = 成行） |
| C3 | ExecutionID の採番規則（文字列辞書順で最新が最大になるか？） | get_hold_id() の判定精度 | ⏸ 保留（形式は "E日付xxx" と判明、順序保証は未確認） |
| C4 | /positions の HoldQty の意味（他注文に拘束中の数量か？） | 将来の get_hold_id() 簡素化に影響 | ✅ サンプル確認（HoldQty=0 実例あり、拘束数量を意味する） |

#### API 仕様追加確認事項（2026-06-02）

| フィールド | 確認内容 |
|---|---|
| FrontOrderType=120 | 成行（マーケットオーダー）確定 |
| FrontOrderType=30 | 逆指値確定 |
| FrontOrderType=20 | 指値確定 |
| TimeInForce=2（FAK）+ 成行 | Exchange=23/24 のみ有効。**日通し(2)は不可**。_sess_exchange は 23/24 のみ返すため問題なし |
| ClosePositions と ClosePositionOrder | 排他的（両方指定するとエラー）。コードは正しく either/or で分岐済み |
| ExecutionID | "E"で始まる建玉ID。ClosePositions[].HoldID に渡す値と一致 |

#### C1 実機確認手順

1. `zh_order.py` の `wait_for_fill` 内、`for d in details:` の先頭に下記を一時追加する
   ```python
   log(f"[DEBUG] RecType: {d.get('RecType')}, Price: {d.get('Price')}")
   ```
2. `DRY_RUN = False` で起動し、シグナル発生 → 約定を待つ
3. `CumQty >= 1` になると `Details` 配列がループされ `[DEBUG]` ログが出力される
4. RecType の実際の値を確認したらデバッグ行を削除し C1 を解決済みに更新する

※ API 仕様書には `Details` 内フィールドの定義なし。実機確認が唯一の方法。

#### 今は修正しない潜在リスク

| No | 箇所 | 内容 |
|---|---|---|
| B3 | get_hold_id() | ExecutionID を文字列比較で最新判定。採番規則依存 |
| B4 | send_sl_order() / send_tp_order() | HoldID なし時は ClosePositionOrder=0（FIFO）フォールバック。複数ポジション時に意図しない建玉返済リスク |

---

### Phase 6 レビュー（zh_monitor.py / 2026-06-02）

#### 検証結果

- ZAIHOU2026.5.27.py との比較でロジック差分なし（`_TICK=5` → `TICK_UNIT` のみ、等価）
- 全関数呼び出しの prefix（zh_api. / zh_order.）正しく変換済み

#### 今は修正しない潜在リスク

| No | 箇所 | 内容 |
|---|---|---|
| P1 | _monitor_inner: SL到達パス | sl_oid=None 時、TP のみキャンセルし実決済注文なしでポジションを記録から削除する。本番で裸ポジションが残る可能性 |
| P2 | _monitor_inner: MAX_HOLD 判定 | `elapsed >= max_hold` は「以上」。マニュアルの「超えた場合」との境界の解釈要確認（1本ずれの可能性） |

#### 設計懸念（ロジック変更が必要なため保留）

| No | 内容 |
|---|---|
| D1 | zh_monitor.py が ZAIHOU_signals に依存（SL/TP/MAX_HOLD パラメータ参照）。責務上は zh_entry.py が持つべき |
| D2 | _close_opposite（zh_entry.py）が zh_monitor の状態変数を直接書き換え。月次 DD 判定が 2 箇所に分散 |
| D3 | reconcile_positions のロック間 race condition。2 回の _positions_lock 取得の間に monitor_positions が割り込める |

#### 将来の簡素化候補（提案のみ・未実装）

- **get_existing_execution_ids + get_hold_id の統合**: 現在 /positions を 2 回呼ぶ。約定後 1 回の呼び出しで `LeavesQty > 0 かつ HoldQty == 0` の建玉を新規と判定できる可能性あり（HoldQty 仕様の確認要）

---

### Phase 7 レビュー（zh_entry.py / 2026-06-02）

#### 検証結果

- ZAIHOU2026.5.27.py との比較でロジック差分なし（関数呼び出しのプレフィックス変換のみ）
- `global` 宣言の変化（`warmup_remaining` → `zh_bar.warmup_remaining -= 1` 等）は Python のモジュール属性代入で等価動作を確認

#### マニュアル 4章との差分

| 項目 | マニュアル記載 | コードの実際の動作 |
|---|---|---|
| DD停止チェック位置 | スキップ条件の最初に列挙 | シグナル判定ループ内でチェック（バー消費・last_signal_bar_time 更新は DD 停止中でも実行される） |
| `can_trade` ガード | 記載なし | `if not zh_bar.can_trade: return`（データ不足時の最初のガード）がある |

#### 今は修正しない潜在リスク

| No | 箇所 | 内容 |
|---|---|---|
| PB1 | check_entry: `_close_opposite` の呼び出し | `_close_opposite` が逆ポジ決済の約定未確認で `return` しても、その後の `_enter_position` 呼び出しはスキップされない（コメントの「新規エントリースキップ」は実態と一致しない） |
| PB2 | `_close_opposite`: `to_close` リスト構築 | `_positions_lock` 非保持で `zh_monitor.positions` を読む。WebSocket スレッドとの競合リスクあり（D3 と同根） |

---

## 付録C: 次期設計変更の方向性

### 2大優先判断（未決定・要合意）

次の設計変更を行うかどうかが、削除できるコードの量と複雑さに大きく影響する。

| No | 判断 | Yes の場合に削除できるもの |
|---|---|---|
| **J1** | TP/SL をブローカー注文なしにする（ソフトウェア価格監視のみ） | send_sl_order / send_tp_order / cancel_order / check_order_active / replace_close_orders の大半 |
| **J2** | open_positions.json を廃止しブローカー API を正とする | reconcile_positions / save_positions / load_positions / Bug4（ゴーストポジション）が根本解決 |

**J1 → Yes にすると J2 も自然に Yes になる**（SL/TP 注文がないため HoldID 管理も不要になり、状態の二重管理が解消される）。

---

### 設計変更の方向性（原則ごと）

| 原則 | 現状の問題 | 方向性 |
|---|---|---|
| ① Single Source of Truth | `open_positions.json` と ブローカー `/positions` の二重管理 → Bug4（ゴースト） | 起動時にブローカー API から取得。`open_positions.json` は廃止か補助のみ |
| ② 暗黙の前提条件を排除 | `wait_for_fill → get_hold_id → send_sl_order` の順序依存 → Bug3（HoldID 無効で SL/TP 全滅） | 連鎖ごと削除。TP/SL はソフトウェア価格監視のみにする |
| ③ ピュアな関数 | `check_entry()` がシグナル判定・状態変更・発注を一気に行う | `get_signal(bars) → Signal \| None`（純粋）と `execute_entry(signal)`（副作用）に分離 |
| ④ 検証は1箇所で | 認証エラー処理が `request_with_reauth` / `_monitor_inner` 等に散在 | `request_with_reauth()` 内に集約。5回終了 → 指数バックオフで継続 |
| ⑤ 早期 return | `_monitor_inner()` がネスト 5 階層以上 | 条件ごとに早期 return して平坦化 |
| ⑥ 念のためのコードを書かない | `reconcile_positions()` が存在するのは `open_positions.json` が信頼できないから | ① を解決すれば reconcile 自体が不要になる |
| ⑦ DRY / 単純化 | `_collect_symbols` / `_bar_state` による複数シンボル管理フレームが複雑 | 日経 225 マイクロ 1 本に絞り削除（翌限月バー収集の廃止） |
| ⑧ シークレット管理 | `API_PASSWORD = "sakimono35oku"` がコードにハードコード | 環境変数または設定ファイル（`secrets.json` 等）に移動 |

---

### 変更しない場合のリスク

- **J1 を No にする場合**: Bug3（HoldID 無効 SL/TP 全滅）が本番で発生するリスクが残る。再起動ごとに SL/TP 再発注が必要で、その都度失敗する可能性がある。
- **J2 を No にする場合**: Bug4（ゴーストポジション）が再発する可能性がある。`reconcile_positions` による検知に依存し続けることになる。
