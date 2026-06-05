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

### ③ ポジション復元（本番モードのみ・ブローカーAPIを正とする）
- ブローカーの `/positions` API を取得し、実際の建玉一覧を正とする
- コンテキストファイル（`open_positions.json`）を補助情報として照合：
  - HoldID が一致 → 復元（エントリー価格はブローカー値を使用）
  - コンテキストにあってブローカーにない → 「停止中に約定済み」として破棄・Discord 通知
  - ブローカーにあってコンテキストにない → 「不明ポジション」として監視不可・Discord 警告
  - HoldID なし（DRY_RUN で作られたポジション）→ 復元しない（Bug4 解決）
- DRY_RUN モードでは `open_positions.json` からそのまま読み込む（従来通り）

### ④ TP 再発注（本番モード・ポジションあり時のみ）
- 保有中のポジション全件に対し、現在のセッションに合わせた **TP 指値注文** を再発注する
- SL はソフトウェア監視のため再発注不要

---

## 2. メインループ（0.4 秒ごと）

起動完了後は以下を繰り返す。

- 板情報（現在値・Bid・Ask）を取得する
- ポジションがあれば → ポジション監視を実行する（→ 3章）
- エントリー条件を判定する（→ 4章）
- 5 分ごとに 5 分足 CSV を保存する
- 16:45・8:00 にセッション切替の TP 再発注を行う（SL はソフトウェア監視のため不要）
- 毎時 0 分と 8:30・16:45 に Discord へポジション状況を報告する
- 毎時 0 分にハートビート通知を Discord へ送る

---

## 3. ポジション保有中の監視

ポジション保有中は 0.4 秒ごと（WebSocket tick 受信時も）に以下を確認する。

板情報から現在値を取得し、各ポジションについて順番に判定する。

### TP 到達
- long ポジションで `現在値 >= TP価格 + 5円`、または short で `現在値 <= TP価格 - 5円`
- 本番モード: `/positions` でポジションが消滅していれば TP 約定済みと判断し損益を記録する
- ポジションがまだ残っていれば次の tick で再確認する

### SL 到達（ソフトウェア監視）
- long ポジションで `現在値 <= SL価格`、または short で `現在値 >= SL価格`
- **SL はブローカー注文なし。ソフトウェアが価格を監視し、到達時に成行決済を発注する**
- 本番モード:
  - HoldID が取得済みの場合: `ClosePositions` 指定の成行発注（FrontOrderType=120, TradeType=2）
  - HoldID なしの場合: FIFO 成行発注
  - 約定確認後 → 実約定価格で損益記録、TP 注文をキャンセル
  - 約定未確認・発注失敗 → Discord 最緊急アラート（手動決済要）
- スリッページ: ポーリング間隔（最大 0.4 秒）分の遅延。N225マイクロ先物では概ね 1〜2 ティック（5〜10 円）程度

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
2. 成行発注する（FrontOrderType=120, TimeInForce=FAK）
3. 約定確認をポーリングする（最大 10 秒）  
   タイムアウト時は注文をキャンセルしてスキップする
4. 実約定価格をもとに SL 価格・TP 価格を再計算する
5. `/positions` を再取得して新規建玉の HoldID（ExecutionID）を特定する
6. **SL はソフトウェア監視のみ（ブローカー注文なし）**
7. TP 指値注文を発注する（FrontOrderType=20, TimeInForce=FAS, HoldID 指定）
8. TP 注文の受付状態を確認する（非アクティブ確定時は Discord 警告）
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
| 現在時刻が **15:30**〜17:00 または **5:40**〜8:45 | 全系統（セッション終了） |
| 土曜 6:00 以降 | 全系統（金曜夜間終了） |
| 23:50 以降 | 系統①のみ（夜間終了） |

セッション終了強制決済は、トリガーされた時刻を記録して 1 回のみ実行する。

> ⚠️ **暫定措置中（Bug8 検証期間）**: 通常は 15:40 / 5:55 だが、2026-06-05 より 15:30 / 5:40 に前倒し中。解除方法は「15章 暫定措置」を参照。

---

## 8. 緊急フラット

SL・TP のキャンセルに失敗した場合に実行する最終手段。

1. `/positions` でブローカー側のネットポジション（買い枚数－売り枚数）を確認する
2. ネットポジションが 0 でなければ逆方向に成行発注してフラット化する
3. 発注失敗時は「手動決済要」として Discord に最緊急アラートを送る

---

## 9. TP 再発注（セッション切替）

先物は日中・夜間でセッション（Exchange コード）が異なり、注文はセッション内でのみ有効。  
**SL はソフトウェア監視のため再発注不要。TP 注文のみ再発注する。**

- **16:45**（夜間セッション前気配）: 全ポジションの TP を Exchange=24 で再発注
- **8:00**（日中セッション前気配）: 全ポジションの TP を Exchange=23 で再発注

手順: 旧 TP 注文キャンセル → 新 TP 注文発注 → 受付確認（失敗時は Discord 警告）

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

| Bug | 発生場面 | 症状 | 状態 |
|---|---|---|---|
| Bug3 | 再起動時 SL 再発注 | HoldID が無効で SL 全滅 | ✅ 解決（SL をソフトウェア監視に変更） |
| Bug4 | 再起動後 | state.json のゴーストポジションを復元 | ✅ 解決（restore_from_broker() でブローカーを正とする） |
| Bug5 | 認証エラー連続 | 5 回で安全終了（指数バックオフなし） | ⏸ 未着手 |
| Bug6 | SL 到達時 | TP 注文がアクティブなまま同一 HoldID に ClosePositions を送ると Code:8 で失敗し無限リトライ | ✅ 解決（`_wait_for_hold_release()` で HoldQty==0 確認後に発注。2026-06-04） |
| Bug7 | 強制決済（TIME/セッション終了） | FIFO（ClosePositionOrder=0）使用のため複数ポジション保有時に意図しない建玉を閉じる可能性がある。①④が冬時間23時に同時 long エントリーするケースが実在（月火水） | ⏸ 未着手（HoldID 指定決済に変更が必要） |
| Bug8 | 強制決済の HoldQty 解放 | SL パスと異なり強制決済パスは `_wait_for_hold_release()` 未適用。TP キャンセル後 0.5 秒固定待機のため HoldQty 未解放で FIFO が失敗する可能性あり | ⏸ 検証中（暫定措置で影響を抑制。15章参照） |

---

## 15. 暫定措置（Bug8 検証期間中）

> **適用期間**: 2026-06-05〜（Bug7・Bug8 が解決したら解除）

### 背景

強制決済パス（セッション終了・時間切れ等）に、SL と同じ「決済前にTP予約解放を確認する処理」が未実装（Bug8）。  
FIFO（名指しなし）決済で予約が残ったまま失敗すると無限リトライになる可能性がある。  
解決まで安全のため、強制決済時刻とエントリー禁止時刻を前倒しして余裕を持たせている。

### 変更内容

| 項目 | 通常 | 暫定措置中 |
|---|---|---|
| 日中強制決済 | 15:40 | **15:30** |
| 夜間強制決済 | 5:55 | **5:40** |
| 日中エントリー禁止 | なし | **15:30 以降** |
| 夜間エントリー禁止 | 金曜 5:00〜6:00 のみ | **毎日 5:40 以降** |

### 解除方法

以下の 2 ファイルを元に戻して git commit する。

**zh_monitor.py**（2 箇所）:
- `1530 → 1540`、`540 → 555`

**zh_entry.py**（2 行を削除）:
- `if 1530 <= hhmm_now < 1700:` の行
- `if 540 <= hhmm_now < 845:` の行

---

## 付録A: ファイル構成（リファクタリング進行中）

最終更新: J2実装・ブローカーAPIを正とするポジション復元（2026-06-03）

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
- **`_wait_for_hold_release(hid, max_retries=10, interval=0.3)` の役割**: SL 到達時に TP キャンセル後の HoldID 解放を確認するヘルパー関数（zh_monitor.py）。`/positions` の `HoldQty==0` を最大 10 回ポーリング（最大 3 秒）。解放確認後に ClosePositions を送ることで Bug6（Code:8 無限リトライ）を根本解消。「HoldQty==0 なら ClosePositions が必ず成功する」は実機検証中（`[SL_POLL]` ログで確認予定）。J3-A 実装時はこの関数ごと移植する。
- **`_adjust_trading_day` による曜日判定の動作**: バーの datetime は `_adjust_trading_day` で補正済み（17:00以降 → 翌取引日付）。シグナル判定の曜日・時刻フィルターはこの補正後 datetime を使用するため、**夜間バーの weekday は物理日ではなく翌取引日の weekday** になる。例: 2026-06-02（火）17:00 のバーは 2026-06-03（水）17:00 として扱われ、③ の S3_WEEKDAYS 判定が水曜（wd=2）で行われる。これは設計通りの動作（trading day convention）。BT 側のパフォーマンス比較スクリプトがこの変換を考慮していなかったため、2026-06-03 に `micro_performance_summary.py` の照合キーを trade_date ベースに修正した。

---

## 付録B: レビュー記録（Phase別）

### Phase 5 レビュー（zh_order.py / 2026-06-02）

#### 要カブコム確認事項（保留中・本番移行前に要確認）

| No | 確認内容 | 影響 | 状態 |
|---|---|---|---|
| C1 | `/orders` Details.RecType の定義値（約定明細の値は 8 か？） | wait_for_fill() が常にタイムアウトするリスク | ✅ 実機確認（RecType:1=受注 / 4=訂正 / 8=約定。2026-06-03） |
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

### 実弾テスト記録（DRY_RUN=False / 2026-06-03）

#### 確認済み

| 項目 | 結果 |
|---|---|
| エントリーフロー | ✅ 成行発注 → 約定確認(1回) → 価格取得 正常動作（系統⑤ short @ 68725） |
| HoldID 取得 | ✅ ExecutionID 正常取得（E2026060305PI1） |
| TP 指値発注 | ✅ Exchange=23 で正常発注 |
| J2（0件起動） | ✅ ブローカーから 0 件復元 正常動作 |
| C1（RecType=8） | ✅ 付録B Phase5 参照 |

#### 未確認（本番モードで状況発生待ち）

| 項目 | 理由 |
|---|---|
| J2（ポジションあり再起動） | DRY_RUN=True で誤再起動したため未検証 |
| TP 到達 | `/positions` 消滅確認ロジック未実施 |
| SL 到達 | ✅ 実機確認（2026-06-04）。Bug6 発覚→根本対処済み。次回実弾で `[SL_POLL]` ログにより HoldQty==0 後の ClosePositions 成否を検証予定 |

---

## 付録C: 次期設計変更の方向性

### 2大優先判断（未決定・要合意）

次の設計変更を行うかどうかが、削除できるコードの量と複雑さに大きく影響する。

| No | 判断 | Yes の場合に削除できるもの |
|---|---|---|
| **J1** | TP/SL をブローカー注文なしにする（ソフトウェア価格監視のみ） | send_sl_order / send_tp_order / cancel_order / check_order_active / replace_close_orders の大半 |
| **J2** | open_positions.json を廃止しブローカー API を正とする | reconcile_positions / Bug4（ゴーストポジション）が根本解決 | ✅ 実装済み |

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

---

### J1 設計オプション詳細（TP/SL の管理方式）

#### 背景・課題

- **TP（利確）**: ブローカー指値注文が約定価格通りに刺さる。成行に変えると約定価格が不利になりPF低下のリスクがある。
- **SL（損切）**: ブローカー逆指値注文は HoldID 指定が必要 → Bug3（再起動時に HoldID が無効になりSL全滅）の根本原因。

#### 3つの選択肢

| 方式 | SL | TP | PFへの影響 | 複雑度 | Bug3 |
|---|---|---|---|---|---|
| A: 完全ソフトウェア | 価格監視＋成行 | 価格監視＋成行 | TP が成行になりPF低下リスク | 最小 | 解決 |
| B: **ハイブリッド（推奨）** | **価格監視＋成行** | **ブローカー指値注文** | **TPは指値のままPF維持** | **中** | **解決** |
| C: 現状維持 | ブローカー逆指値 | ブローカー指値 | 影響なし | 最大 | 残存 |

#### B: ハイブリッド方式の詳細

```
SL: _monitor_inner が価格監視 → SL価格に達したら成行で即時決済
TP: ブローカー指値注文のまま → 約定価格通りに刺さる
    → _monitor_inner が /positions でTP約定を確認して記録
```

**削除できるもの:**
- `send_sl_order()` — SL 逆指値注文が不要になる
- `check_order_active()` — SL 受付確認が不要になる
- SL の `replace_close_orders()` — セッション切替時のSL再発注が不要（TP のみ残る）
- `_monitor_inner` の SL 到達パス（約40行 → 約10行）: `wait_for_fill(sl_oid)` / `/positions` タイムアウト確認 / `_tp_already_cancelled` フラグが全部不要になる

**残るもの:**
- `send_tp_order()` — TP 指値注文は継続
- `cancel_order()` — TP キャンセル（SL到達時・TIME決済時）は必要
- `replace_close_orders()` — TP のみの再発注として残る（簡素化される）
- HoldID — TP の `ClosePositions` 指定には引き続き必要

**SLスリッページについて:**
ソフトウェア監視から成行発注まで最大 0.4秒（ポーリング間隔）の遅延がある。N225マイクロ先物は流動性が高く、現実的なスリッページは 1〜2 ティック（5〜10円）程度。SL幅（60〜80pt）に対して軽微。Bug3 解決の恩恵の方が大きい。

**セッション間のSL保護:**
ブローカーSL注文がなくなるため、セッション間（15:40〜17:00 / 5:55〜8:45）は価格配信が止まる。この間は `_monitor_inner` が動かないため、SL保護が機能しない。ただし現状もセッション終了強制決済でポジションをクローズしているため、実質的な差はない。

#### 決定ステータス

| 判断 | 決定 |
|---|---|
| J2: state.json 廃止 | ✅ 実装済み |
| J1: TP/SL 方式 | ✅ 実装済み（B: ハイブリッド） |

---

### 次期設計方向（J3: 責任分界の整理）

#### 現状の問題

J1・J2 実装後も以下の設計上の曖昧さが残っている。

**状態の正（Source of Truth）が分散：**

| 状態 | 現在の所在 | 問題 |
|---|---|---|
| ポジション実在 | ブローカー /positions | ✅ J2で整理済み |
| system/sl_price/tp_price | open_positions.json（補助） | 起動間のみ必要 |
| TP注文ID | open_positions.json | 再起動後に古い値が残ることがある |
| 月次損益 | zh_monitor + CSV | どちらが正か不明確 |

**責任分界が曖昧：**
- `zh_entry._close_opposite()` が zh_monitor の内部状態（pnl/trade_log/positions）を直接書き換えている
- 同じ決済後処理ロジックが `_monitor_inner` と `_close_opposite` の2箇所に分散

#### 設計案（カブコム API 照合済み・実装前）

**J3-A: zh_monitor を「ポジション管理の唯一の窓口」にする**

```
zh_monitor が公開する API:

  startup_restore()
    = restore_from_broker() + replace_close_orders() を統合
    = 起動時に1回呼ぶだけで「ポジション復元 + TP再発注」が完結
    → カブコム API: /positions(GET) + /sendorder/future(POST, FrontOrderType=20)

  add_position(pos)
    = positions.append + save_positions のラッパー
    → API呼び出しなし

  close_position(pos, exit_price, reason, now)
    = PnL計算・trade_log・CSV・DD判定・positions削除・save を一括
    = _monitor_inner と _close_opposite に分散しているロジックを統合
    → API呼び出しなし（決済注文は呼び出し元が担当）
```

**ZAIHOU.py 起動部分の変化（イメージ）:**

```python
# 現在（起動後に3回呼ぶ）:
zh_monitor.restore_from_broker()
zh_monitor.replace_close_orders(_se)

# J3-A後（1回で済む）:
zh_monitor.startup_restore()
```

**変更範囲（API違反なし・確認済み）:**

| ファイル | 変更量 | 内容 |
|---|---|---|
| zh_monitor.py | 中 | startup_restore/add_position/close_position を追加 |
| zh_entry.py | 中 | _close_opposite が大幅に短くなる（約60行→15行） |
| ZAIHOU.py | 小 | 起動シーケンス数行の変更 |
| zh_order/bar/api/utils/config | **変更なし** | |

**実装の優先度:** J3-A → J3-B（⑦翌限月削除）→ J3-C（⑧シークレット管理）の順を推奨

---

---

# 【重要】バーデータのソート順について（2026-06-03 修正済み）

> **初見の方へ:** このセクションはソート順のバグ修正の記録です。  
> 他のセクションとは独立した内容です。

---

## 背景：先物の「取引日」とは

N225マイクロ先物の取引日は **カレンダー日付とズレる** ことがある。

```
例：5/27（月）のナイトセッション
  物理時刻: 2026-05-27 17:00 〜 2026-05-27 23:55
  取引日  : 2026-05-27（月）の取引として扱われる

  ただしナイトセッションの続き（深夜〜翌朝）
  物理時刻: 2026-05-28 00:00 〜 2026-05-28 05:55
  取引日  : 2026-05-27（月）の取引として扱われる（翌日ではない）
```

**正しい取引日順のバー並び順：**
```
5/27 17:00 → 5/27 23:55 → 5/28 00:00 → 5/28 05:55 → 5/28 08:45 → 5/28 15:15
  ↑ナイト開始          ↑深夜              ↑翌朝日中
  ←─────── 5/27の取引日 ──────────────────────────→
```

---

## 修正前の問題

バックテストエンジン `backtest_system45_combined.py`（系統④⑤）が  
**物理時間順（カレンダー順）** でバーをソートしていた。

```
修正前（物理時間順）:
  5/27 00:00 → 5/27 08:45 → 5/27 15:15 → 5/27 17:00 → 5/27 23:55 → ...
  ← 日中 ─────────────────────→ ← ナイト →

修正後（取引日順）:
  5/27 17:00 → 5/27 23:55 → 5/28 00:00 → 5/28 08:45 → 5/28 15:15 → 5/28 17:00
  ← ナイト ─────────────────────────────────── ← 翌日中 →
```

これにより、系統④⑤の **移動平均・MACD などの指標値がライブシステムと乖離** していた。

---

## 各モジュールの対応

| モジュール | `_trading_day_sort_key` の挙動 | 結果 | 状態 |
|---|---|---|---|
| `bt13`（系統①③） | `hour < 17 → dt + 1日` | 取引日順 ✓ | 元々正しい |
| `ZAIHOU_signals`（実運用） | `hour < 17 → dt + 1日` | 取引日順 ✓ | 元々正しい |
| `bt45`（系統④⑤）| `hour < 17 → (dt-1日, dt)` タプル | **物理時間順 ✗** | **2026-06-03 修正** |
| `micro_performance_summary.py` | `_tday_sort` → `_bt13_sort` に変更 | 取引日順 ✓ | **2026-06-03 修正** |

---

## 修正箇所（`backtest_system45_combined.py`）

```python
# 修正前
def _trading_day_sort_key(dt):
    if dt.hour < 17:
        trading_date = (dt - pd.Timedelta(days=1)).date()
    else:
        trading_date = dt.date()
    return (pd.Timestamp(trading_date), dt)  # ← タプルで物理時間順になっていた

# 修正後（bt13/ZAIHOU_signals と同一）
def _trading_day_sort_key(dt):
    if dt.hour < 17:
        return dt + pd.Timedelta(days=1)  # ← 1日加算で取引日順
    return dt
```

---

## 修正による BT 成績変化（`backtest_combined_all.py` DD制限なし）

| 系統 | 修正前 件数 | 修正前 損益 | 修正前 PF | 修正後 件数 | 修正後 損益 | 修正後 PF |
|---|---|---|---|---|---|---|
| ① | 5,623 | +724,494円 | 1.341 | 5,623 | +724,494円 | 1.341（**変化なし**）|
| ③ | 9,917 | +915,126円 | 1.246 | 9,917 | +915,126円 | 1.246（**変化なし**）|
| ④ | **4,980** | +419,540円 | 1.163 | **4,315** | **+1,002,720円** | **1.588** |
| ⑤ | **5,433** | +768,424円 | 1.393 | **5,433** | +514,574円 | 1.271 |
| 合算 | 25,953 | +2,827,584円 | 1.272 | 25,288 | **+3,156,914円** | **1.334** |

> ①③ は bt13 が元々正しかったため変化なし。  
> ④ は件数減・損益大幅改善（PF 1.163 → **1.588**）。  
> ⑤ は損益やや低下（PF 1.393 → 1.271）。  
> **合算損益は +329,330円 改善。**

---

## 今後の注意

- `bt45` の BT 結果を参照するときは **2026-06-03以降の数値を使うこと**（それ以前は物理時間順ソートの誤った数値）
- `ZAIHOU_bt.py` の `EXPECTED` 値も修正が必要（現在は旧値のまま）
