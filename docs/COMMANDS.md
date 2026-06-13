# /mdraw コマンド一覧

権限 `meshsurfacedraw.use` または admin が必要。

## 表示・再生

| コマンド | 説明 |
|---|---|
| `/mdraw cube [scale]` | 組み込みキューブを表示（動作確認用） |
| `/mdraw <objName> [scale]` | `oxide/data/MeshDraw/<objName>.obj` を表示 |
| `/mdraw anim <animName>` | `<animName>.anim` を再生（全身フレーム方式・密度低下） |
| `/mdraw anim stop` | アニメ停止、静止高密度に戻す |
| `/mdraw clear` | 全モデル消去 |
| `/mdraw refresh` | 全モデルを再描画（設定変更の反映） |

設置時、その瞬間にプレイヤーがいた方向が「正面」になる（前後関係の基準）。

## 静止表示の画質調整

| コマンド | 説明 | 既定 |
|---|---|---|
| `/mdraw dist <m>` | 想定観賞距離。小さいほど高密度 | 0.5 |
| `/mdraw px <px>` | 線の目標画面間隔(px)。小さいほど濃い | 0.9 |
| `/mdraw passes <n>` | 重ね描きの層数（位相ずらしで隙間を補間） | 3 |
| `/mdraw lines <n>` | 1モデルあたりの線数上限 | 60000 |
| `/mdraw step <m>` | 線間隔の世界座標下限 | 0.0005 |
| `/mdraw core` | インナーコア(裏打ち層)のON/OFF | ON |
| `/mdraw edge <n>` | 輪郭線の太さ（重ね本数、0で無効） | 2 |
| `/mdraw crease <度>` | 輪郭を描く折れ目の角度しきい値 | 25 |
| `/mdraw cull` | 裏面カリングのON/OFF | OFF |
| `/mdraw shade` | テクスチャ面の陰影ON/OFF（OFF=原色＝アニメ調向き） | OFF |
| `/mdraw texedge` | テクスチャ面に輪郭線を描くか | OFF |

## アニメーション調整

| コマンド | 説明 | 既定 |
|---|---|---|
| `/mdraw animlines <n>` | アニメ1フレームの線数上限（大=綺麗だが点滅リスク） | 15000 |
| `/mdraw animtick <秒>` | アニメ更新間隔（小=滑らかだが重い） | 0.3 |
