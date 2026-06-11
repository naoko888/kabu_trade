# タスク⑨ 設計レビュー: SL/TP 入れ替え

作成: 2026-06-11  
レビュアー: Claude A / ChatGPT / 将来の自分

---

## このシステムについて（レビュアー向け背景説明）

### システム概要
- **ZAIHOU**: 日経225マイクロ先物（N225マイクロ）の自動売買ボット（Python）
- **取引所API**: kabuステーション API（カブコム証券）REST API
- **運用環境**: さくらVPS（Windows Server 2025）で24時間稼働
- **取引セッション**: 夜間（17:00〜翌5:55）/ 日中（8:45〜15:40）の2セッション制

### ファイル構成（今回関係するもの）

| ファイル | 役割 |
|---|---|
| `zh_entry.py` | シグナル検知 → エントリー発注 → SL/TP注文発注 |
| `zh_monitor.py` | ポジション価格監視 → SL/TP到達時の決済処理 |
| `zh_order.py` | カブコムAPIへの注文送信（成行・指値・逆指値・キャンセル） |
| `zh_config.py` | 設定値（DRY_RUN, LOT, SL/TPパラメータ等） |

### 現在の SL/TP 管理方式（B方式）
```
エントリー約定
  ↓
zh_entry.py: TP指値注文をカブコムに発注（SL注文はなし）
  ↓
zh_monitor.py: ポーリングで価格監視
  ├── SL到達 → TP注文キャンセル → 成行決済発注
  └── TP到達 → /positions でTP約定を確認 → 記録
```

### 変更したい理由
- 現状: **SLはPythonが価格監視**。Pythonがクラッシュ・停止するとSLが機能しない
- VPS 24h稼働開始後、**Pythonが止まっている間もSLでロスカットしたい**
- カブコムAPI側で逆指値成行注文（SL）を持たせれば、Python停止中でも保護される

### 将来の設計目標
- **監視ロジックの統一**: 現在 zh_monitor.py の `_monitor_inner` が「SL価格監視」と「TP約定確認」の2種類の監視を混在処理している。将来的にこれを「ブローカー注文状態チェック」と「価格監視」に分離・統一しやすい構造にしたい
- **カスタマイズしやすさ**: SL/TP方式を変更する際に影響範囲が小さく、バグが入りにくい設計

### 重要な制約・既知のバグ
- **Bug3（既発生・修正済み）**: HoldID指定のブローカー注文は再起動時にHoldIDが無効になりSL/TPが全滅した。→ 今回の設計では HoldID を使わず FIFO（ClosePositionOrder=0）で対応
- **Bug9（既発生・修正済み）**: PCスリープ中にTPが約定 → Pythonが知らずにSL処理を実行 → 二重決済ループ。→ `_is_position_already_closed()` で事前確認する関数を追加済み
- **1系統1ポジション**: 同一系統での複数ポジション保有はしない設計。FIFOでも意図しない建玉決済は起きない

---

## 前提条件

### 1. 設計目標
- **将来の監視統一**: 現在 zh_monitor.py が SL/TP を混在管理しているが、将来的に監視ロジックを一元化しやすい構造にする
- **カスタマイズしやすさ**: B方式（現状）→ D方式への変更が最小差分で実現できること。将来さらに変更する場合も追いやすい
- **バグを増やさない**: 既存の Bug9 修正（`_is_position_already_closed`）や `restore_from_broker` の設計を壊さないこと

### 2. カブコムAPI確認済み事項（マニュアル照合済み）

| パラメータ | 値 | 確認状態 |
|---|---|---|
| `FrontOrderType` | 30 = 逆指値、Price は **指定なし**（0） | ✅ マニュアル確認済み |
| `ReverseLimitOrder` | ネストされたオブジェクト。`TriggerPrice`, `UnderOver`, `AfterHitOrderType`, `AfterHitPrice` を含む | ✅ 先物APIサンプル確認済み |
| `TriggerSec` | **先物には不要**（現物サンプルにはあるが先物サンプルにはない） | ✅ マニュアルサンプル比較で確認 |
| `AfterHitOrderType` | 1 = 成行 | ✅ C2・マニュアルサンプル確認済み |
| `AfterHitPrice` | 0（成行のため価格不要） | ✅ マニュアルサンプル確認済み |
| `TimeInForce` | 逆指値（成行）= **FAK(2) のみ有効**。FAS(1) は不可 | ✅ マニュアル対応表確認済み |
| `UnderOver` | **1 = 以下**（価格がトリガー以下で発動）、**2 = 以上**（価格がトリガー以上で発動） | ✅ **C5: 確定**（GitHub Issue #1222・先物APIサンプルで確認） |
| `ClosePositions` と `ClosePositionOrder` | **排他的**（両方指定不可、片方のみ） | ✅ マニュアル確認済み |
| `ClosePositionOrder: 0` | FIFO（日付古い順、損益高い順） | ✅ 現行コードで使用済み |

#### ✅ C5: UnderOver の値（確定済み）

**kabucom GitHub Issue #1222** と **先物APIサンプル** から確定:

| UnderOver | 意味 | 発動条件 |
|---|---|---|
| **1** | 以下 | 価格がトリガー価格**以下**になったら発動 |
| **2** | 以上 | 価格がトリガー価格**以上**になったら発動 |

根拠:
- Issue #1222: 信用**買**建玉（long）のSLで `UnderOver=1` が成功 → long SL = 以下 = 1
- 先物APIサンプル: Side:"1"(sell返済=longクローズ) + `UnderOver:1` = 価格下落でSL発動 = 以下

⚠️ **コード修正が必要**: 設計書の仮実装は誤りだった。Claude A の値が正しい。

```python
# ❌ 誤（設計書初版の仮実装）
under_over = 2 if side == "buy" else 1

# ✅ 正（Claude A の指摘通り）
under_over = 1 if side == "buy" else 2
# buy=long → SLは価格が下がったら発動 → 以下 = 1
# sell=short → SLは価格が上がったら発動 → 以上 = 2
```

### 3. 設計原則（カスタマイズしやすくするために）
- **関数単位で差し替え可能にする**: `send_tp_order` / `send_sl_order` は対称的なインターフェイスにする
- **pos辞書のキーを統一する**: `sl_order_id` / `tp_order_id` を常に持つ（使わない方は `None`）
- **replace_close_orders は「使用中の注文種別を再発注する」だけ**: B方式なら TP、D方式なら SL を再発注する。将来モード切替しやすいよう分岐を明確に
- **監視統一への道（A案・今回）**: 今回は `_monitor_inner` 内の「SL到達パス」と「TP到達パス」を明確に分けて書く。関数分離は行わない。将来タスク⑩として単独で設計・実施する（D方式安定後）

---

## 現在構成（B方式）

| | 方式 | ファイル/関数 |
|---|---|---|
| **SL** | Python価格監視 → 成行発注 | `zh_monitor._monitor_inner` |
| **TP** | カブコム指値注文（エントリー時発注） | `zh_order.send_tp_order()` |

セッション切替時: `replace_close_orders()` が TP を再発注（SLは不要）

---

## 変更案（D方式）

| | 方式 | 変更内容 |
|---|---|---|
| **SL** | カブコム逆指値成行注文（エントリー時発注） | `zh_order.send_sl_order()` を新規追加 |
| **TP** | Python価格監視 → 成行発注 | `_monitor_inner` の TP到達パスを変更 |

セッション切替時: `replace_close_orders()` が SL を再発注（TPは不要）

---

## メリット

- VPS停止中・Python クラッシュ中もSLがブローカー側で有効
- セッション間（15:40〜17:00 / 5:55〜8:45）もSL保護あり（現状は無保護）
- SL到達後の HoldQty解放待ち（`_wait_for_hold_release`）が不要になる
- SL処理のコードが大幅に簡素化される（現在の SL到達パスは約60行）

## デメリット

- TP が成行になるため約定価格が不利になる可能性（1〜2ティック = 5〜10円）
- PF低下リスクあり（要バックテスト比較は困難、実運用で確認）
- 再起動時に SL注文の復元が必要（後述: 想定障害②）

---

## 変更が必要なファイルと箇所

### zh_order.py: `send_sl_order()` を追加

> ⚠️ **Claude Aレビュー指摘**: 初版コードは API 構造が誤り（TimeInForce・Price・ReverseLimitOrder ネスト構造）。以下が修正版。

```python
def send_sl_order(side: str, sl_price: float, session_exchange: int) -> str | None:
    """SL逆指値成行発注。side=エントリー方向("buy"|"sell")"""
    if DRY_RUN:
        return None
    exit_side = "2" if side == "sell" else "1"
    # buy=long: SLは価格が下がったら発動 → 以下 = UnderOver=1
    # sell=short: SLは価格が上がったら発動 → 以上 = UnderOver=2
    under_over = 1 if side == "buy" else 2
    body = {
        "Password": API_PASSWORD, "Symbol": zh_api.SYMBOL,
        "Exchange": session_exchange,
        "TradeType": 2,
        "TimeInForce": 2,            # FAK必須（逆指値成行はFAS不可）
        "Side": exit_side,
        "Qty": LOT,
        "Price": 0,                  # 逆指値はPrice=0（価格はReverseLimitOrderに入れる）
        "ExpireDay": 0,
        "FrontOrderType": 30,        # 逆指値確定
        "ClosePositionOrder": 0,     # FIFO（HoldID指定しない → Bug3回避）
        "ReverseLimitOrder": {       # 逆指値パラメータはネスト構造
            "TriggerPrice": sl_price,
            "UnderOver": under_over,   # 1=以下(long SL), 2=以上(short SL)
            "AfterHitOrderType": 1,  # 成行
            "AfterHitPrice": 0,
        }
    }
    res = zh_api.request_with_reauth("POST", "/sendorder/future", json_body=body)
    if res:
        oid = safe_json(res).get("OrderId", "")
        log(f"[OK] SL逆指値発注 Price:{sl_price:.0f} OrderId:{oid}")
        return oid
    log(f"[ERR] SL発注失敗 Price:{sl_price:.0f}")
    return None
```

**注意点**: `ClosePositionOrder=0`（FIFO）を使い HoldID指定しない。
理由: HoldID指定は再起動時に無効化されBug3の根本原因になる。1系統1ポジション運用なのでFIFOで問題なし。

⚠️ **FIFO前提**: 現在は1系統1ポジション前提のため問題なし。将来、複数系統が同一商品で同時にポジションを持つ場合は FIFO が意図しない建玉を決済する可能性があり、HoldID 指定への変更が必要になる。

### zh_entry.py: `_enter_position()` の変更

変更前:
```python
# SLはソフトウェア価格監視のみ（ブローカーSL注文なし）
tp_oid = zh_order.send_tp_order(order_side, pos["tp_price"], session_exchange, hold_id)
pos["tp_order_id"] = tp_oid
```

変更後:
```python
# SLをブローカー逆指値成行で発注（D方式）
sl_oid = zh_order.send_sl_order(order_side, pos["sl_price"], session_exchange)
pos["sl_order_id"] = sl_oid
# SL発注失敗 or 非アクティブ → 強制返済（無保護ポジションを持たない）
if not sl_oid or zh_order.check_order_active(sl_oid) is False:
    log(f"[ERR] 系統{system} SL発注失敗 → 強制返済")
    send_discord(f"🚨 系統{system} SL発注失敗 → 強制返済実行")
    close_oid = zh_order.send_entry_order(close_side, session_exchange, trade_type=2)
    zh_order.wait_for_fill(close_oid)
    return  # エントリー処理を中断
```

### zh_monitor.py: `_monitor_inner()` の変更

**TP到達パス**（現在のSL到達パスとほぼ同じ構造）:
1. `_is_position_already_closed(hid, sl_oid)` で SL約定済み確認（ポジション消滅 → SL約定として記録して終了）
2. SL未約定なら `cancel_order(sl_oid)`
3. **0.2〜0.5秒待機**（キャンセル受付から状態反映ラグを吸収）
4. `check_order_active(sl_oid)` でキャンセル確認 → まだアクティブなら次ティックで再試行
5. キャンセル確認済みなら成行決済 `send_entry_order(close_side, _sess, trade_type=2)`
6. 約定確認 `wait_for_fill(close_oid)`

**SL到達パス**（現在のTP到達パスと同じ構造）:
1. `/positions` でポジション消滅確認（ブローカーSLが先に約定済みか）
2. 消滅確認できれば `/orders?id=sl_oid` から**実約定価格**を取得して `cp` に記録（B案。取得失敗時は `sl_price` でフォールバック）
3. 消滅未確認なら「次ティック再確認」

### zh_monitor.py: `restore_sl_orders()` を追加（再起動専用）

再起動後 `restore_from_broker()` の直後に呼ぶ。セッション切替とは別関数。

```python
def restore_sl_orders(session_exchange: int) -> None:
    """再起動後のSL注文復元。有効なSLが残っていれば再利用、なければ新規発注。"""
    for pos in positions:
        sl_oid = pos.get("sl_order_id")
        if sl_oid and zh_order.check_order_active(sl_oid):
            log(f"[OK] SL注文有効のため再利用 OrderId:{sl_oid}")
            continue  # 既存SLが生きている → そのまま使う
        order_side = "sell" if pos["side"] == "short" else "buy"
        new_sl = zh_order.send_sl_order(order_side, pos["sl_price"], session_exchange)
        pos["sl_order_id"] = new_sl
        if not new_sl:
            log(f"[ERR] restore SL発注失敗 → 強制決済")
            send_discord(f"🚨 restore SL発注失敗 → 強制決済実行")
            close_side = "buy" if pos["side"] == "short" else "sell"
            close_oid = zh_order.send_entry_order(close_side, session_exchange, trade_type=2)
            zh_order.wait_for_fill(close_oid)
```

### zh_monitor.py: `replace_close_orders()` の変更

変更前: TP再発注（SLは不要）
変更後: SL再発注（TPは不要）

```python
def replace_close_orders(session_exchange: int) -> None:
    """セッション切替時にSL注文を新セッションで再発注（TPはソフトウェア監視のため不要）"""
    ...
    for pos in positions:
        order_side = "sell" if pos["side"] == "short" else "buy"
        # SL 再発注
        sl_oid = pos.get("sl_order_id")
        if sl_oid:
            zh_order.cancel_order(sl_oid)
            time.sleep(0.2)
        new_sl = zh_order.send_sl_order(order_side, pos["sl_price"], session_exchange)
        pos["sl_order_id"] = new_sl
        ...
```

---

## 想定障害① SL約定とTP検知が同時

**シナリオ**: Python が TP到達を検知してSLキャンセルしようとした瞬間、
ブローカーSLが先に約定済みだった場合。

**現状（B方式の対称問題）**: SL到達時に TP が先に約定していた場合は
`_is_position_already_closed()` で検知済み（Bug9修正）。

**D方式での対策**:
TP到達パスの先頭で `_is_position_already_closed(hid, sl_oid)` を呼ぶ。
- ポジション消滅 → SL約定済み → `/orders?id=sl_oid` から**実約定価格**を取得して記録（B案。取得失敗時のみ `sl_price` でフォールバック）
- ポジション残存 → SLキャンセル → 成行決済へ（想定障害⑤のキャンセル確認フロー適用）

**懸念**: `hid`（HoldID）を D方式でも保持するか？
- `get_hold_id()` は引き続きエントリー後に呼ぶ
- `_is_position_already_closed(hid, sl_oid)` の `hid` チェックに使う
- ただし SL発注には使わない（FIFO のため）

---

## 想定障害② 再起動時の SL注文復元

**シナリオ**: VPS 再起動後、`restore_from_broker()` でポジション復元。
この時 SL注文は消えている（セッション切替で失効）。

**現状（B方式）**: 再起動後 `replace_close_orders()` が TP を再発注。

**D方式での対策**:
`restore_from_broker()` の後に `replace_close_orders()` を呼ぶ流れは同じ。
ただし SL再発注なので順序が重要:
1. `restore_from_broker()` でポジション復元
2. `replace_close_orders(current_session_exchange)` で SL再発注
3. SL発注失敗なら Discord 緊急アラート

**残課題**: 再起動〜SL再発注の間（数秒）は無保護。
→ ポジションがある状態で再起動すること自体を避けるのがベスト。

---

## 想定障害③ セッション切替時の SL再発注失敗

**シナリオ**: 15:40 頃にセッション切替で SL を再発注しようとしたが失敗した。

**現状（B方式）**: TP再発注失敗時は Discord 警告を出して続行。

**D方式での対策**: Discord 警告 + **強制決済**。

→ ✅ **強制決済**（Claude A・ChatGPT 両者一致）
SL再発注失敗 = 無保護ポジション。Discord通知のみで放置するリスクが高いため、即時成行返済する。

---

## 想定障害④ SL発注自体の失敗（エントリー直後）

**シナリオ**: エントリー約定後、`send_sl_order()` が失敗した。

**対策**:
- Discord 緊急アラートを出す（現在の TP非アクティブ確定と同様）
- ただし TP と違い SL失敗は即時リスクなので、エントリー後にポジション追加しないフラグも検討

→ ✅ **強制返済**（Claude A・ChatGPT 両者一致）
SL発注失敗 = 無保護ポジション。エントリー直後に `send_sl_order()` が失敗した時点で即成行返済する。

---

## 想定障害⑤ TP到達時の SL キャンセル失敗（GPT 新規指摘）

**シナリオ**: TP到達を検知して `cancel_order(sl_oid)` を呼んだが、キャンセルAPIが失敗した。
その状態で成行TP決済を発注すると、SLが生きたまま両方の注文が出ることになる。
数秒後にSLも発動 → **二重決済**。

**現設計の問題**: `cancel_order()` の戻り値チェックが未定義。キャンセル失敗をスルーして決済へ進む。

**対策**:
1. `cancel_order(sl_oid)` 呼び出し
2. **0.2〜0.5秒待機**（キャンセル受付から状態反映までのラグを吸収）
3. `check_order_active(sl_oid)` でキャンセル確認
4. まだアクティブ → **TP決済しない**（次ティックで再試行）
5. キャンセル済み確認 → 成行TP決済へ進む

⚠️ 手順2の待機が必要な理由: `cancel_order()` 成功（受付）≠ 注文状態がキャンセル済みに変わった（反映）。即座に `check_order_active` を呼ぶとまだ "Active" が返ることがある。

---

## 検証方法（実装後）

| 確認事項 | 確認方法 |
|---|---|
| SL逆指値注文がカブコムに届くか | DRY_RUN=False + 少額テスト / kabuステーション注文一覧 |
| TP成行決済が正しく約定するか | `[EXIT]` ログで reason="TP到達" を確認 |
| SL約定後にPythonが正しく検知するか | `[SL到達]` ログでポジション消滅確認 |
| 再起動後のSL再発注 | 手動で ZAIHOU 再起動 → `[REPLACE]` ログ確認 |
| SL/TP競合 | DRY_RUN=True でシミュレーション |
| `/orders` レスポンス検証 | `product=3` が先物か・`RecType==8` が約定レコードか・約定価格フィールド名（`Price` / `CumPrice`）を kabuステーションで実注文後に目視確認 |

---

## 未決定事項（レビュアーへの質問）

1. ~~SL発注失敗時~~ → ✅ **強制返済**（Claude A・ChatGPT 両者一致）
2. ~~SL再発注失敗時（セッション切替）~~ → ✅ **強制決済**（Claude A・ChatGPT 両者一致）
3. ~~HoldID~~ → ✅ **継続取得**（Claude A・ChatGPT 両者一致: `_is_position_already_closed` 精度のため）
4. ~~AfterHitOrderType の確認~~ → ✅ 解決済み（C2確認: AfterHitOrderType:1 + AfterHitPrice:0 = 成行）

---

## Claude B（現セッション）コメント

### ✅ C5解決: UnderOver の値（確定済み）

**kabucom GitHub Issue #1222 で確定:**
- `UnderOver: 1 = 以下`（価格がトリガー以下で発動）
- `UnderOver: 2 = 以上`（価格がトリガー以上で発動）

設計書の初版コードは **Claude B（私）が誤って修正**していた（`2 if side == "buy"` は誤り）。  
**Claude A の `1 if side == "buy" else 2` が正しかった。**  
コードは修正済み。

---

### 🟡 新規懸念: SL約定価格の記録ズレ（ChatGPT 指摘①を受けて）

現案では SL発動後に `cp = pos["sl_price"]` で損益記録しているが、  
逆指値成行はスリッページが発生するため **実約定価格 ≠ sl_price** になる。

対策案: SL発動確認後に `/orders?id=sl_oid` から実約定価格を取得して記録。  
ただし `sl_oid` は FIFO発注のため注文照会で正しく取れるか未確認。

→ **✅ B案採用（GPT・Claude 両者一致）**:  
　SL発動確認後に `/orders?id=sl_oid` から実約定価格を取得して記録する。  
　取得失敗時は `sl_price` でフォールバック（harmless）。  
　理由: BT vs 実運用の差異分析を重視するため、近似記録の積み重なりは許容できない。

---

### 🟡 restore_from_broker 時のSL確認（ChatGPT 指摘②を受けて）

ChatGPT の指摘「既存SL注文が有効な場合はキャンセルしないほうが安全」について:

**✅ セッション切替と再起動を分けて対応する（GPT 指摘を一部採用）**:

| ケース | SLの状態 | 対応 |
|---|---|---|
| セッション切替（日中↔夜間） | 旧SLは FAK のためセッション終了時に**自動失効済み** | 無条件でキャンセル試行 → 新SL発注 |
| 再起動（`restore_from_broker`） | SLが**まだ有効な可能性あり** | `check_order_active(sl_oid)` で確認 → 有効なら残す / 無効・なければ新規発注 |

セッション切替時は「キャンセル成功 → 新SL失敗 → 無保護」リスクは存在するが、  
旧SLは既に失効済みのため起点は常に「無保護」。失敗時は強制決済（既に決定済み）で対応。

再起動時は既存SLが有効な可能性があるため、不要なキャンセル→再発注を避ける。

---

### ✅ 両レビュアー共通で評価された点

- Bug3 回避（FIFO + HoldID不使用）の判断: 正しい
- `_is_position_already_closed` の流用（Bug9修正を壊さない）: 正しい
- 将来の監視統一（`check_price_events` / `check_order_events` 分離）との相性: 良好

---

---

## レビュー手順

### ステップ1: このファイルを全文コピー

### ステップ2: 各レビュアーに送る

**Claude A（新しいチャット）**
1. claude.ai を新しいウィンドウで開く
2. このファイルの全文を貼り付ける
3. 末尾に「このシステムの設計変更案をレビューしてください。問題点・懸念点・改善案を指摘してください」と追加して送信
4. 回答を `review_task09_claude_a.md` に貼り付ける

**ChatGPT**
1. chatgpt.com を開く
2. 同じ手順で送信
3. 回答を `review_task09_chatgpt.md` に貼り付ける

**将来の自分**
- 実装後・運用後に気づいたことを `review_task09_self.md` に随時メモ

### レビューファイル一覧

| ファイル | 担当 | 状態 |
|---|---|---|
| `review_task09_claude_a.md` | Claude A（新規チャット） | 未実施 |
| `review_task09_chatgpt.md` | ChatGPT | 未実施 |
| `review_task09_self.md` | 将来の自分 | 随時更新 |
