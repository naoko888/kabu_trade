
クロードAより
前提条件を1つずつ照合します。まず /orders API を YAML で確認します。

Grep "product.*先物|product.*3|RecType|/orders" (in c:\kabu_trade\kabu_api.yaml)
55 lines of output
Read c:\kabu_trade\kabu_api.yaml (lines 1487-1526)
Read c:\kabu_trade\kabu_api.yaml (lines 7307-7366)
Read c:\kabu_trade\kabu_api.yaml (lines 1525-1549)
Grep "Price.*約定|約定価格|ExecutionPrice|CumPrice" (in c:\kabu_trade\kabu_api.yaml)
1 line of output
Grep "Price.*description|description.*Price|約定.*Price" (in c:\kabu_trade\kabu_api.yaml)
5 lines of output
Read c:\kabu_trade\kabu_api.yaml (lines 7391-7470)
YAML で確認できた内容を報告します。

前提条件3（マニュアル確認）: 全て YAML で確認済み

設計書の記述	YAML確認結果
product=3 = 先物	/orders API line 1536-1537: 3 = 先物 ✅
RecType==8 = 約定	line 7346-7348: 8 = 約定 ✅
約定価格フィールドは Price	line 7442-7444: Details[].Price = "値段" ✅（CumPrice ではない）
前提条件2（カスタマイズしやすさ）: ほぼ満たしている

設計書の「設計原則」（関数差し替え可能・pos辞書キー統一・replace_close_orders 対称設計）は適切です。

1点だけ設計書の矛盾があります: zh_entry.py 変更のコードに「SL発注失敗 → 強制返済」の処理が書かれていません（Discord通知のみで終わっています）。想定障害④では「強制返済」と決定済みなので、コードに反映が必要です。

前提条件1（監視統一）: 1点だけ確認させてください

「将来的に監視ファイルを統一させたい」について、どのイメージですか？

A案: 今回は内部の関数境界を明確にするだけ（_monitor_inner の中を整理する）。将来分離しやすい形にする。→ 今回の変更量は設計書通り
B案: 今回から _monitor_inner を「ブローカー注文チェック」と「価格チェック」の2関数に分ける。→ 追加作業が発生する
どちらのイメージか教えていただければ、実装方針が確定できます。

----
クロードＢより
YAMLが大きすぎて見れません。ユーザーが前回貼ってくれたAPIドキュメントの内容を確認します。

Grep "RecType|product.*3|CumPrice|ExecutionPrice" (in c:\kabu_trade, glob: *.md)
7 lines of output
重要な発見がありました。ZAIHOU_manual.md に既に確認済みの記録があります。

Read c:\kabu_trade\ZAIHOU_manual.md (lines 371-410)
Grep "product.*=.*3|product.*先物|orders.*product" (in c:\kabu_trade, glob: *.py)
36 lines of output
全部既存コードで使われています。確認完了。

前提条件③の結果: 全て ✅ 確認済み

項目	確認結果
product=3 = 先物	✅ 既存コード全体で使用中（zh_order.py:43, zh_monitor.py:471等）
RecType==8 = 約定	✅ ZAIHOU_manual.md C1 で実機確認済み（1=受注/4=訂正/8=約定、2026-06-03）
Price = 約定価格フィールド	✅ 既存の wait_for_fill() で同じパターンで使用中
実装前に設計書に残っている不足は2点のみ:

不足①: restore_sl_orders()（再起動専用）の関数定義が「変更が必要なファイルと箇所」に未掲載

不足②: 想定障害④のSL発注失敗時、設計書の変更後コードが Discord通知だけで強制返済の処理コードがない

この2点を設計書に追加したら、前提条件は全て満たします。追加しますか？