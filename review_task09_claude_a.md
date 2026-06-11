# タスク⑨ レビュー: Claude A

レビュー日: 2026-06-11
対象: design_review_task09.md

---

## レビュー内容

 Critical: send_sl_order() のコードが API 仕様と合っていない
設計書のコードは そのまま実行すると 4001005（パラメータ変換エラー）で失敗します。

問題箇所を比較します：


# ❌ 設計書のコード（エラーになる）
body = {
    ...
    "TimeInForce": 1,          # ❌ 逆指値成行は FAK(2) 必須。FAS(1) は不可
    "Price": sl_price,         # ❌ 逆指値の Price は 0（価格は ReverseLimitOrder に入れる）
    "FrontOrderType": 30,
    "AfterHitOrderType": 1,    # ❌ トップレベルに置けない
    "AfterHitPrice": 0,        # ❌ トップレベルに置けない
    "ClosePositionOrder": 0,
    # ❌ TriggerPrice がない（UnderOver もない）
}

# ✅ 正しい構造（API マニュアルのサンプルと照合済み）
body = {
    ...
    "TimeInForce": 2,          # FAK 必須
    "Price": 0,                # 逆指値は Price=0
    "FrontOrderType": 30,
    "ClosePositionOrder": 0,
    "ReverseLimitOrder": {     # ← ネストされたオブジェクトに入れる
        "TriggerPrice": sl_price,
        "UnderOver": 1 if side == "buy" else 2,  # long=以下/short=以上
        "AfterHitOrderType": 1,
        "AfterHitPrice": 0,
    }
}
API マニュアルの対応表より：

執行条件	TimeInForce	日中	夜間
逆指値（成行）	FAK のみ	✅	✅
🟡 未決定事項への回答
Q1: SL発注失敗時 → 強制返済 or 警告のみ？

→ 強制返済を推奨。無保護ポジションを持ち続けるリスクは発注コストより大きい。
エントリー直後に send_sl_order が失敗した時点で即 send_entry_order(反対, close_side) で返済すべき。

Q2: SL再発注失敗時（セッション切替）→ 強制決済 or 警告のみ？

→ 強制決済を推奨。セッション切替は毎日発生する。ここで警告だけにすると、失敗を見落としたまま次の取引セッション全体が無保護になる。

Q3: HoldID を D 方式でも取得し続けるか？

→ 取得継続を推奨。_is_position_already_closed(hid, sl_oid) での競合検知の精度が上がる。FIFO で SL 発注する場合でも hid は持っておく方が安全。コスト（API 呼び出し 1 回）は小さい。

🟢 設計として良い点
Bug3 回避（FIFO で HoldID 不使用） の判断は正しい。1系統1ポジション前提なら FIFO で問題なし
_is_position_already_closed の流用（想定障害①の対策）は Bug9 修正の設計を壊さない
replace_close_orders の対称設計 はメンテナンスしやすい
🟡 その他の懸念点
TP がスリップする影響
TP が成行になると 1〜2 ティック（5〜10pt）不利になる。現行の TP=240pt に対して 2% 程度の影響。積み重ねると PF 低下は無視できないので、実運用 1〜2 ヶ月後にログから TP 約定価格 vs tp_price の乖離を測定することを推奨。

---

## 対応メモ

まとめ
評価
設計方向性	✅ 正しい
コード（send_sl_order）	🔴 ReverseLimitOrder の構造ミス・TimeInForce・Price の誤り → 要修正
障害対策の網羅性	✅ 十分
未決定事項	上記の通り
コードを直してから実装に進んでください。

---

## Claude B より返答（2026-06-11）

### ✅ ReverseLimitOrder 構造ミスの修正
Claude A の指摘通り全て修正済み。設計書の `send_sl_order()` を以下に更新した:
- `TimeInForce: 2`（FAK）
- `Price: 0`
- `ReverseLimitOrder` をネスト構造に
- `TriggerPrice`, `UnderOver`, `AfterHitOrderType`, `AfterHitPrice` を正しく配置

### ✅ UnderOver の値: Claude A が正しかった

Claude A が記述した `"UnderOver": 1 if side == "buy" else 2` は **正しい**。

Claude B が「buy=以下=2」と誤って修正していたが、kabucom GitHub Issue #1222 で確定:
- 信用**買**建玉（long）のSLで `UnderOver=1` が成功 → **1 = 以下**
- 先物APIサンプル: Side:"1"(long close) + UnderOver:1 = 価格下落でSL発動 = 以下

**最終コード（確定）:**
```python
under_over = 1 if side == "buy" else 2
# buy=long → 価格下落でSL発動 → 以下 = 1
# sell=short → 価格上昇でSL発動 → 以上 = 2
```

Claude A の指摘が全て正しかった。ありがとうございます。
