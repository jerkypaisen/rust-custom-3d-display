using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using Oxide.Core;
using UnityEngine;

namespace Oxide.Plugins
{
    [Info("MeshSurfaceDraw", "jerky+claude", "0.2.0")]
    [Description("Displays custom 3D models in-world by scanline-filling faces with ddraw lines")]
    public class MeshSurfaceDraw : RustPlugin
    {
        // ===== 設計 =====
        // 回転なしの静的表示に特化。プレイヤーごとに一度だけ高密度で描き、
        // 長いdurationで表示を維持する。継続的な再送信をしないため
        // 線の予算を桁違いに増やせて、サーバー/ネットワーク負荷は初回送信時のみ。

        // ===== 設定 =====
        // 線間隔は「観測者の画面上のピクセル間隔」基準(近いほど細かい)。Stepはその下限。
        // 1px未満を狙うと線が重なり合い、面がベタ塗りに近づいて奥が透けなくなる
        // 密度は「基準距離RefDistから見て画面PxSpacing間隔」になる固定の世界間隔で決める。
        // プレイヤーの距離や向きに依存しないので、再描画なしでどこから見ても同じ品質
        private float RefDist = 0.5f;          // 想定観賞距離(m)。/mdraw dist で変更。小=濃い
        private float PxSpacing = 0.9f;        // 基準距離での目標画面間隔(px)。小=濃い/線数増
        private float Step = 0.0005f;          // 世界間隔の下限(m)。密度は dist/px で制御する
        private int MaxLinesPerModel = 60000;  // 1モデル1人あたりの線数上限
        private float Duration = 1800f;        // 線の寿命(秒)。期限前に静かに積み直すのみ
        private float MinResend = 2.5f;        // 再送の最短間隔(秒)
        private float MaxDistance = 200f;      // この距離内のプレイヤーに描く
        private int LinesPerPump = 1500;       // 0.1秒ごとに送る線数(送信レート制御)
        // 重ね描きの層数。2層目以降は走査線の位相をずらし、塗り方向も変えて(クロスハッチ)
        // 前の層の隙間を補間する。層を重ねるほど面が濃くなる(2〜4が現実的)
        private int DensifyPasses = 3;

        // インナーコア(裏打ち): モデルを法線方向にわずかに縮めた内側のコピーを
        // 少し暗い同色で最下層に描く。線の隙間から見えるのが背景ではなく裏打ちになる。
        // 縮小が大きいと視差でズレて見えるため、ごく薄い貼り合わせにする
        private bool CoreFill = true;
        private const float CoreSpacingMul = 1.3f; // 裏打ちは少し粗く(線数節約)
        private const float CoreShade = 0.85f;     // 裏打ちの暗さ(表面よりわずかに暗く)
        private const float CoreShrink = 0.006f;   // 縮小量(モデル高さ比)
        private bool ZTest = true;             // 地形/建物に隠れる
        // どこから見ても面が存在するよう、既定で全方向の面を描く(ONにすると線数半減の省エネ)
        private bool BackfaceCull = false;
        private bool ShadeTextured = false;    // テクスチャ面に陰影を掛けるか(アニメ調はOFF)
        private bool TexOutline = false;       // テクスチャ面にも輪郭線を描くか(顔が汚れるので既定OFF)
        private int EdgeBoldPx = 2;            // 輪郭の太さ(重ね本数)
        private float CreaseDeg = 25f;         // 輪郭を描く折れ目のしきい値(度)
        private int TexSegMax = 16;            // 1本の線の最大分割数(テクスチャ採色)
        private const float RadPerPx = 0.0012f; // 1pxあたりの視角(輪郭の太らせ用)
        private static readonly Vector3 LightDir = new Vector3(0.4f, 0.8f, 0.3f).normalized;
        private static readonly Color TexEdgeColor = new Color(0.05f, 0.05f, 0.07f);

        private const string PermUse = "meshsurfacedraw.use";

        // ===== メッシュ =====
        private struct Edge
        {
            public int V0, V1;
            public int FA, FB;     // 隣接面。FB=-1は境界辺
            public float AngleDeg; // 隣接面のなす角。境界辺は360
        }

        private class MeshData
        {
            public Vector3[] Verts;
            public List<int[]> Faces;
            public List<Color> FaceColors; // 拡張OBJ c 行。無ければnull
            public Edge[] Edges;

            public Vector2[] UVs;          // v=0が下
            public List<int[]> FaceUVs;    // UVの無い面はnull
            public Color32[] Tex;          // 行0=画像上端
            public int TexW, TexH;
            public Vector3[] VNormals;     // 頂点法線(インナーコアの縮小方向)
            public float Height;           // モデルの高さ(縮小量の基準)

            public Color SampleTex(Vector2 uv)
            {
                int x = Mathf.Clamp(Mathf.RoundToInt(uv.x * (TexW - 1)), 0, TexW - 1);
                int y = Mathf.Clamp(Mathf.RoundToInt((1f - uv.y) * (TexH - 1)), 0, TexH - 1);
                return Tex[y * TexW + x];
            }

            public SkinData Skin; // .skinファイルがあれば読み込まれる
        }

        // ===== スキニング (.skin / .anim) =====
        private class SkinData
        {
            public int[] BoneParent;
            public Vector3[] BoneRest;    // X反転済み(プラグイン空間)
            public int[][] VertBones;
            public float[][] VertWeights;
        }

        private class AnimClip
        {
            public string Name;
            public float Fps = 30f;
            public int MaxFrame;
            public int[][] Frames;        // ボーンごとのキーフレーム(昇順)
            public Vector3[][] Pos;       // X反転済み
            public Quaternion[][] Rot;    // X鏡映の共役済み
        }

        private class Instance
        {
            public MeshData Mesh;
            public Vector3 Pos;
            public float Scale = 1f;
            public float Yaw;

            // アニメーション再生状態
            public AnimClip Clip;
            public float Frame;
            public bool[] DynFace;     // クリップ中に動く面(毎ティック再送)
            public int[] DynVerts;     // 動く面が参照する頂点
            public Vector3[] SkinBuf;  // スキニング結果(モデル空間)の作業バッファ
            public Quaternion[] GRot;  // ボーングローバル回転の作業バッファ
            public Vector3[] GPos;
        }

        // プレイヤーごとの描画状態。距離や向きが変わっても一切描き直さない。
        // 再送は「モデルが変わった」「寿命が近い」ときだけ(どちらも消さずに上書き)
        private struct DrawState
        {
            public float Time;
            public int Version;
            public int Pass;       // 完了済みの重ね描き層数(1=基本層のみ)
        }

        // ===== 送信キュー =====
        private struct QLine
        {
            public Vector3 A, B;
            public Color C;
            public bool Core; // 裏打ち層(常に最初=最下層に送る)
        }

        private class SendJob
        {
            public BasePlayer Player;
            public QLine[] Lines;
            public int Index;
        }

        private readonly List<Instance> instances = new List<Instance>();
        private readonly List<SendJob> jobs = new List<SendJob>();
        private readonly Dictionary<ulong, DrawState> playerDraw = new Dictionary<ulong, DrawState>();
        private int version; // モデルの追加/消去/refreshで増える。古い描画の検出用
        private readonly Dictionary<ulong, bool> adminRestore = new Dictionary<ulong, bool>();
        private readonly Dictionary<string, MeshData> meshCache = new Dictionary<string, MeshData>(StringComparer.OrdinalIgnoreCase);
        private Timer checkTimer;
        private Timer pumpTimer;

        private void Init()
        {
            permission.RegisterPermission(PermUse, this);
        }

        private void OnServerInitialized()
        {
            checkTimer = timer.Every(2f, CheckPlayers);
            pumpTimer = timer.Every(0.1f, Pump);
            animTimer = timer.Every(AnimTick, AnimUpdate);
        }

        private void Unload()
        {
            checkTimer?.Destroy();
            pumpTimer?.Destroy();
            animTimer?.Destroy();
            foreach (var kv in adminRestore)
            {
                var p = BasePlayer.FindByID(kv.Key);
                if (p != null && !kv.Value)
                {
                    p.SetPlayerFlag(BasePlayer.PlayerFlags.IsAdmin, false);
                    p.SendNetworkUpdateImmediate();
                }
            }
            adminRestore.Clear();
            jobs.Clear();
            instances.Clear();
        }

        private void OnPlayerDisconnected(BasePlayer player, string reason)
        {
            jobs.RemoveAll(j => j.Player == player);
            adminRestore.Remove(player.userID);
            playerDraw.Remove(player.userID); // 再接続時に描き直す
        }

        // ===== コマンド =====
        [ChatCommand("mdraw")]
        private void CmdDraw(BasePlayer player, string command, string[] args)
        {
            if (!permission.UserHasPermission(player.UserIDString, PermUse) && !player.IsAdmin)
            {
                SendReply(player, "権限がありません");
                return;
            }
            if (args.Length == 0)
            {
                SendReply(player, "/mdraw cube|<objName> [scale] | anim <名前|stop> | clear | refresh | dist <m> | px <画面間隔> | passes <層数> | core | step <m> | lines <上限> | edge <太さ> | crease <度> | cull | shade | texedge");
                return;
            }
            switch (args[0].ToLowerInvariant())
            {
                case "clear":
                    instances.Clear();
                    jobs.Clear();
                    playerDraw.Clear();
                    version++;
                    foreach (var p in BasePlayer.activePlayerList)
                    {
                        SendClear(p);
                    }
                    SendReply(player, "全モデルを消去しました");
                    return;
                case "refresh":
                    jobs.Clear();
                    playerDraw.Clear();
                    version++;
                    SendReply(player, "全モデルを再描画します");
                    return;
                case "anim":
                {
                    if (args.Length < 2)
                    {
                        SendReply(player, "/mdraw anim <モーション名|stop> (oxide/data/MeshDraw/<名前>.anim)");
                        return;
                    }
                    if (args[1].Equals("stop", StringComparison.OrdinalIgnoreCase))
                    {
                        foreach (var inst in instances)
                        {
                            inst.Clip = null;
                            inst.DynFace = null;
                        }
                        jobs.Clear();
                        playerDraw.Clear();
                        foreach (var p in BasePlayer.activePlayerList)
                        {
                            SendClear(p);
                        }
                        version++; // 全面を静的描画に戻す
                        SendReply(player, "アニメーションを停止しました");
                        return;
                    }
                    Instance target = null;
                    for (int i = instances.Count - 1; i >= 0; i--)
                    {
                        if (instances[i].Mesh.Skin != null)
                        {
                            target = instances[i];
                            break;
                        }
                    }
                    if (target == null)
                    {
                        SendReply(player, "スキン付きモデルがありません (.skinファイルをOBJと同じ場所に置いてください)");
                        return;
                    }
                    var clip = LoadAnim(args[1], target.Mesh.Skin.BoneParent.Length);
                    if (clip == null)
                    {
                        SendReply(player, $"モーション '{args[1]}' を読み込めません (oxide/data/MeshDraw/{args[1]}.anim)");
                        return;
                    }
                    target.Clip = clip;
                    target.Frame = 0f;
                    InitAnimBuffers(target);
                    // 静的な高密度描画を一旦消す(全身フレーム方式に切替)。
                    // 旧静的を残すと「後勝ち」で最前面に貼り付き動きが見えなくなる
                    jobs.Clear();
                    playerDraw.Clear();
                    foreach (var p in BasePlayer.activePlayerList)
                    {
                        SendClear(p);
                    }
                    SendReply(player, $"'{args[1]}' を再生します ({clip.MaxFrame / clip.Fps:F1}秒ループ, " +
                        $"全身を{AnimTick}秒ごとに再描画)。アニメ中は密度が落ちます");
                    return;
                }
                case "step":
                    if (args.Length > 1 && float.TryParse(args[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var s))
                    {
                        Step = Mathf.Clamp(s, 0.0002f, 0.1f);
                        SendReply(player, $"線間隔の下限 = {Step}m (/mdraw refresh で反映)");
                    }
                    return;
                case "px":
                    if (args.Length > 1 && float.TryParse(args[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var px))
                    {
                        PxSpacing = Mathf.Clamp(px, 0.3f, 5f);
                        SendReply(player, $"線の画面間隔 = {PxSpacing}px (小=濃い。/mdraw refresh で反映)");
                    }
                    return;
                case "animlines":
                    if (args.Length > 1 && int.TryParse(args[1], out var al))
                    {
                        AnimMaxLines = Mathf.Clamp(al, 2000, 60000);
                        SendReply(player, $"アニメ1フレームの線数上限 = {AnimMaxLines} (大=綺麗/重い)");
                    }
                    return;
                case "animtick":
                    if (args.Length > 1 && float.TryParse(args[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var at))
                    {
                        AnimTick = Mathf.Clamp(at, 0.1f, 1f);
                        animTimer?.Destroy();
                        animTimer = timer.Every(AnimTick, AnimUpdate);
                        SendReply(player, $"アニメ更新間隔 = {AnimTick}秒 (小=滑らか/重い)");
                    }
                    return;
                case "dist":
                    if (args.Length > 1 && float.TryParse(args[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var rd))
                    {
                        RefDist = Mathf.Clamp(rd, 0.1f, 50f);
                        SendReply(player, $"想定観賞距離 = {RefDist}m (この距離で隙間が見えない密度にする。小=濃い。/mdraw refresh で反映)");
                    }
                    return;
                case "passes":
                    if (args.Length > 1 && int.TryParse(args[1], out var ps))
                    {
                        DensifyPasses = Mathf.Clamp(ps, 1, 6);
                        SendReply(player, $"重ね描き層数 = {DensifyPasses} (層ごとに位相をずらして隙間を補間)");
                    }
                    return;
                case "lines":
                    if (args.Length > 1 && int.TryParse(args[1], out var ml))
                    {
                        MaxLinesPerModel = Mathf.Clamp(ml, 1000, 200000);
                        SendReply(player, $"線数上限 = {MaxLinesPerModel}/モデル");
                    }
                    return;
                case "edge":
                    if (args.Length > 1 && int.TryParse(args[1], out var eb))
                    {
                        EdgeBoldPx = Mathf.Clamp(eb, 0, 6);
                        SendReply(player, $"輪郭の太さ = {EdgeBoldPx}本重ね" + (EdgeBoldPx == 0 ? " (輪郭OFF)" : ""));
                    }
                    return;
                case "crease":
                    if (args.Length > 1 && float.TryParse(args[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var cr))
                    {
                        CreaseDeg = Mathf.Clamp(cr, 1f, 180f);
                        SendReply(player, $"折れ目しきい値 = {CreaseDeg}度");
                    }
                    return;
                case "cull":
                    BackfaceCull = !BackfaceCull;
                    SendReply(player, $"裏面カリング = {(BackfaceCull ? "ON" : "OFF")} (/mdraw refresh で反映)");
                    return;
                case "shade":
                    ShadeTextured = !ShadeTextured;
                    SendReply(player, $"テクスチャ面の陰影 = {(ShadeTextured ? "ON" : "OFF (原色=アニメ調向き)")} (/mdraw refresh で反映)");
                    return;
                case "texedge":
                    TexOutline = !TexOutline;
                    SendReply(player, $"テクスチャ面の輪郭線 = {(TexOutline ? "ON" : "OFF")} (/mdraw refresh で反映)");
                    return;
                case "core":
                    CoreFill = !CoreFill;
                    SendReply(player, $"インナーコア(裏打ち) = {(CoreFill ? "ON" : "OFF")} (隙間から背景でなく裏打ちが見える。/mdraw refresh で反映)");
                    return;
            }

            string name = args[0];
            MeshData mesh = name.Equals("cube", StringComparison.OrdinalIgnoreCase) ? BuiltinCube() : LoadObj(name);
            if (mesh == null)
            {
                SendReply(player, $"モデル '{name}' を読み込めません (oxide/data/MeshDraw/{name}.obj)");
                return;
            }
            float scale = args.Length > 1 && float.TryParse(args[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var sc) ? sc : 1f;

            Vector3 fwd = player.eyes.HeadForward();
            fwd.y = 0f;
            fwd.Normalize();
            Vector3 pos = player.transform.position + fwd * 4f;
            // モデルの正面(+Z)をプレイヤーに向ける
            float yaw = Mathf.Atan2(-fwd.x, -fwd.z) * Mathf.Rad2Deg;

            instances.Add(new Instance { Mesh = mesh, Pos = pos, Scale = scale, Yaw = yaw });
            version++;
            SendReply(player, $"'{name}' を設置しました (面数 {mesh.Faces.Count}, scale {scale})。数秒かけて描画されます");
            CheckPlayers();
        }

        // ===== アニメーション =====
        private float AnimTick = 0.3f;       // 再送間隔(秒)。小=滑らか/重い
        private int AnimMaxLines = 15000;    // 1フレーム(全身)の線数上限。1回でまとめ送信するので上げすぎるとカクつく
        private const float AnimSpacingMul = 1.0f; // 動的部位の線間隔(静止部と同等)
        private const float DynThreshold = 0.02f;  // この距離(モデル単位)以上動く頂点を動的とみなす
        // 動的線の寿命=更新間隔×この倍率。1.0に近いほど残像が消える(ただし小さすぎると
        // サーバーが遅延したとき腕が一瞬消える)。1.5だと常に1.5ポーズ重なり残像になる
        private const float AnimDurMul = 1.15f;
        private readonly Dictionary<string, AnimClip> clipCache = new Dictionary<string, AnimClip>(StringComparer.OrdinalIgnoreCase);
        private Timer animTimer;

        private SkinData LoadSkin(string name)
        {
            string path = Path.Combine(Interface.Oxide.DataDirectory, "MeshDraw", name + ".skin");
            if (!File.Exists(path))
            {
                return null;
            }
            var lines = File.ReadAllLines(path);
            int boneCount = 0, vertCount = 0, li = 0;
            var head = lines[li++].Split(' ');
            boneCount = int.Parse(head[1]);
            vertCount = int.Parse(head[2]);
            var skin = new SkinData
            {
                BoneParent = new int[boneCount],
                BoneRest = new Vector3[boneCount],
                VertBones = new int[vertCount][],
                VertWeights = new float[vertCount][],
            };
            for (int b = 0; b < boneCount; b++)
            {
                var t = lines[li++].Split(' ');
                skin.BoneParent[b] = int.Parse(t[1]);
                skin.BoneRest[b] = new Vector3(
                    -float.Parse(t[2], CultureInfo.InvariantCulture), // X反転(頂点と同じ)
                    float.Parse(t[3], CultureInfo.InvariantCulture),
                    float.Parse(t[4], CultureInfo.InvariantCulture));
            }
            for (int v = 0; v < vertCount; v++)
            {
                var t = lines[li++].Split(' ');
                int n = (t.Length - 1) / 2;
                var bs = new int[n];
                var ws = new float[n];
                for (int j = 0; j < n; j++)
                {
                    bs[j] = int.Parse(t[1 + j * 2]);
                    ws[j] = float.Parse(t[2 + j * 2], CultureInfo.InvariantCulture);
                }
                skin.VertBones[v] = bs;
                skin.VertWeights[v] = ws;
            }
            return skin;
        }

        private AnimClip LoadAnim(string name, int boneCount)
        {
            if (clipCache.TryGetValue(name, out var cached))
            {
                return cached;
            }
            string path = Path.Combine(Interface.Oxide.DataDirectory, "MeshDraw", name + ".anim");
            if (!File.Exists(path))
            {
                return null;
            }
            var frames = new List<int>[boneCount];
            var pos = new List<Vector3>[boneCount];
            var rot = new List<Quaternion>[boneCount];
            var clip = new AnimClip { Name = name };
            foreach (var raw in File.ReadAllLines(path))
            {
                var t = raw.Split(' ');
                if (t[0] == "anim")
                {
                    clip.MaxFrame = int.Parse(t[1]);
                    clip.Fps = float.Parse(t[2], CultureInfo.InvariantCulture);
                }
                else if (t[0] == "k")
                {
                    int b = int.Parse(t[1]);
                    if (b < 0 || b >= boneCount)
                    {
                        continue;
                    }
                    if (frames[b] == null)
                    {
                        frames[b] = new List<int>();
                        pos[b] = new List<Vector3>();
                        rot[b] = new List<Quaternion>();
                    }
                    frames[b].Add(int.Parse(t[2]));
                    pos[b].Add(new Vector3(
                        -float.Parse(t[3], CultureInfo.InvariantCulture), // X反転
                        float.Parse(t[4], CultureInfo.InvariantCulture),
                        float.Parse(t[5], CultureInfo.InvariantCulture)));
                    // X鏡映の共役: (qx,qy,qz,qw) → (qx,-qy,-qz,qw)
                    rot[b].Add(new Quaternion(
                        float.Parse(t[6], CultureInfo.InvariantCulture),
                        -float.Parse(t[7], CultureInfo.InvariantCulture),
                        -float.Parse(t[8], CultureInfo.InvariantCulture),
                        float.Parse(t[9], CultureInfo.InvariantCulture)));
                }
            }
            clip.Frames = new int[boneCount][];
            clip.Pos = new Vector3[boneCount][];
            clip.Rot = new Quaternion[boneCount][];
            for (int b = 0; b < boneCount; b++)
            {
                clip.Frames[b] = frames[b]?.ToArray();
                clip.Pos[b] = pos[b]?.ToArray();
                clip.Rot[b] = rot[b]?.ToArray();
            }
            clipCache[name] = clip;
            return clip;
        }

        // ボーンbのフレームframe時点のローカル姿勢(キー間は線形/球面補間)
        private static void SampleBone(AnimClip clip, int b, float frame, out Vector3 t, out Quaternion r)
        {
            var fs = clip.Frames[b];
            if (fs == null || fs.Length == 0)
            {
                t = Vector3.zero;
                r = Quaternion.identity;
                return;
            }
            if (frame <= fs[0])
            {
                t = clip.Pos[b][0];
                r = clip.Rot[b][0];
                return;
            }
            if (frame >= fs[fs.Length - 1])
            {
                t = clip.Pos[b][fs.Length - 1];
                r = clip.Rot[b][fs.Length - 1];
                return;
            }
            int lo = 0, hi = fs.Length - 1;
            while (hi - lo > 1)
            {
                int mid = (lo + hi) / 2;
                if (fs[mid] <= frame)
                {
                    lo = mid;
                }
                else
                {
                    hi = mid;
                }
            }
            float u = (frame - fs[lo]) / Mathf.Max(fs[hi] - fs[lo], 1);
            t = Vector3.Lerp(clip.Pos[b][lo], clip.Pos[b][hi], u);
            r = Quaternion.Slerp(clip.Rot[b][lo], clip.Rot[b][hi], u);
        }

        // 全ボーンのグローバル姿勢を解決 (.skinは親が先に並ぶよう出力済み)
        private static void EvaluatePose(SkinData skin, AnimClip clip, float frame,
            Quaternion[] gRot, Vector3[] gPos)
        {
            for (int i = 0; i < skin.BoneParent.Length; i++)
            {
                SampleBone(clip, i, frame, out var t, out var r);
                int p = skin.BoneParent[i];
                if (p < 0)
                {
                    gRot[i] = r;
                    gPos[i] = skin.BoneRest[i] + t;
                }
                else
                {
                    gRot[i] = gRot[p] * r;
                    gPos[i] = gPos[p] + gRot[p] * (skin.BoneRest[i] - skin.BoneRest[p] + t);
                }
            }
        }

        private static Vector3 SkinVert(SkinData s, Quaternion[] gR, Vector3[] gP, int vi, Vector3 v)
        {
            var bs = s.VertBones[vi];
            var ws = s.VertWeights[vi];
            Vector3 acc = Vector3.zero;
            for (int j = 0; j < bs.Length; j++)
            {
                acc += ws[j] * (gR[bs[j]] * (v - s.BoneRest[bs[j]]) + gP[bs[j]]);
            }
            return acc;
        }

        // アニメ用バッファを確保(スキニング結果とボーン姿勢の作業領域)
        private void InitAnimBuffers(Instance inst)
        {
            int bc = inst.Mesh.Skin.BoneParent.Length;
            inst.GRot = new Quaternion[bc];
            inst.GPos = new Vector3[bc];
            inst.SkinBuf = new Vector3[inst.Mesh.Verts.Length];
        }

        // 毎ティック: 全身をスキニングし、プレイヤー視点で遠→近に並べて全身を再描画する。
        // 線同士は深度処理されない(後勝ち)ため、毎フレーム全身を正しい順序で送り直すのが
        // 唯一の正しい方法。部分更新だと再送部位が常に最前面に来て破綻する
        private void AnimUpdate()
        {
            foreach (var inst in instances)
            {
                if (inst.Clip == null || inst.SkinBuf == null)
                {
                    continue;
                }
                inst.Frame = (inst.Frame + inst.Clip.Fps * AnimTick) % Mathf.Max(inst.Clip.MaxFrame, 1);
                var mesh = inst.Mesh;
                EvaluatePose(mesh.Skin, inst.Clip, inst.Frame, inst.GRot, inst.GPos);
                Quaternion rot = Quaternion.Euler(0f, inst.Yaw, 0f);
                var world = new Vector3[mesh.Verts.Length];
                for (int v = 0; v < world.Length; v++)
                {
                    world[v] = inst.Pos + rot * (SkinVert(mesh.Skin, inst.GRot, inst.GPos, v, mesh.Verts[v]) * inst.Scale);
                }

                foreach (var player in BasePlayer.activePlayerList)
                {
                    if (player == null || !player.IsConnected)
                    {
                        continue;
                    }
                    if (Vector3.Distance(player.transform.position, inst.Pos) > MaxDistance)
                    {
                        continue;
                    }
                    DrawFullFrame(player, inst, world);
                }
            }
        }

        private readonly List<QLine> animLines = new List<QLine>(16384);

        // 全身を1フレームぶん塗って、プレイヤー視点で遠→近に並べて一括送信する
        private void DrawFullFrame(BasePlayer player, Instance inst, Vector3[] world)
        {
            var mesh = inst.Mesh;
            Vector3 eye = player.eyes.position;
            var faceFront = new bool[mesh.Faces.Count];
            long wanted = 0;
            float spacing = FillSpacing() * AnimSpacingMul;
            for (int fi = 0; fi < mesh.Faces.Count; fi++)
            {
                var face = mesh.Faces[fi];
                Vector3 a = world[face[0]], b = world[face[1]], c = world[face[2]];
                Vector3 normal = Vector3.Cross(b - a, c - a);
                faceFront[fi] = Vector3.Dot(normal, (a + b + c) / 3f - eye) <= 0f;
                if (!faceFront[fi])
                {
                    continue; // プレイヤー視点で裏面カリング(線数ほぼ半減)
                }
                for (int t = 2; t < face.Length; t++)
                {
                    wanted += EstimateLines(world[face[0]], world[face[t - 1]], world[face[t]], spacing);
                }
            }
            if (wanted == 0)
            {
                return;
            }
            float factor = wanted > AnimMaxLines ? (float)AnimMaxLines / wanted : 1f;

            animLines.Clear();
            float acc = 0f;
            for (int fi = 0; fi < mesh.Faces.Count; fi++)
            {
                if (!faceFront[fi])
                {
                    continue;
                }
                var face = mesh.Faces[fi];
                Vector3 a = world[face[0]], b = world[face[1]], c = world[face[2]];
                Vector3 normal = Vector3.Cross(b - a, c - a).normalized;
                float brightness = 0.30f + 0.70f * Mathf.Abs(Vector3.Dot(normal, LightDir));
                int[] faceUV = mesh.Tex != null && mesh.FaceUVs != null ? mesh.FaceUVs[fi] : null;
                Color flat = Color.white;
                float shade = 1f;
                if (faceUV != null)
                {
                    shade = ShadeTextured ? brightness : 1f;
                }
                else
                {
                    flat = (mesh.FaceColors != null ? mesh.FaceColors[fi] : Color.white) * brightness;
                    flat.a = 1f;
                }
                for (int t = 2; t < face.Length; t++)
                {
                    FillTriangle(animLines, mesh, world[face[0]], world[face[t - 1]], world[face[t]],
                        faceUV, t, flat, shade, factor, ref acc, 0f, 0, spacing, false);
                }
            }

            // プレイヤーの今の視点で遠→近に並べる(1フレーム内で前後関係が正しくなる)
            var arr = animLines.ToArray();
            var keys = new float[arr.Length];
            for (int i = 0; i < arr.Length; i++)
            {
                keys[i] = -((arr[i].A + arr[i].B) * 0.5f - eye).sqrMagnitude;
            }
            Array.Sort(keys, arr);

            // アニメ中はadminを維持(毎ティックのフラグ切替を避ける)
            if (!adminRestore.ContainsKey(player.userID))
            {
                adminRestore[player.userID] = player.IsAdmin;
                if (!player.IsAdmin)
                {
                    player.SetPlayerFlag(BasePlayer.PlayerFlags.IsAdmin, true);
                    player.SendNetworkUpdateImmediate();
                }
            }
            // 寿命は更新間隔ぴったりより少し長く(間に合わないと一瞬消えるため)。
            // 旧フレームは自然消滅するので clear 不要・全身が常に正しい順序で更新される
            float dur = AnimTick * AnimDurMul;
            foreach (var l in arr)
            {
                UnityEngine.DDraw.Line(player, l.A, l.B, l.C, dur, false, ZTest);
            }
        }

        // ===== 描画スケジューリング =====
        // 全方向の面を高密度で描くため、視点の向きが変わっても再描画しない。
        // 再送するのは ①モデルが変わった ②寿命が近い ③大きく近づいた(密度の描き足し) のみ。
        // いずれも ddraw.clear はせず上から描き足すだけなので、表示が消える瞬間はない
        private void CheckPlayers()
        {
            if (instances.Count == 0)
            {
                return;
            }
            float now = Time.realtimeSinceStartup;
            foreach (var player in BasePlayer.activePlayerList)
            {
                if (player == null || !player.IsConnected)
                {
                    continue;
                }
                float nearest = float.MaxValue;
                foreach (var inst in instances)
                {
                    nearest = Mathf.Min(nearest, Vector3.Distance(player.transform.position, inst.Pos));
                }
                if (nearest > MaxDistance)
                {
                    continue;
                }
                bool pending = false;
                foreach (var j in jobs)
                {
                    if (j.Player == player)
                    {
                        pending = true; // 送信中は判断しない
                        break;
                    }
                }
                if (pending)
                {
                    continue;
                }

                int pass;
                if (!playerDraw.TryGetValue(player.userID, out var st) || st.Version != version)
                {
                    pass = 0; // 初回/モデル変更: 最初の層から積む
                }
                else if (st.Pass < DensifyPasses && now - st.Time > MinResend)
                {
                    pass = st.Pass; // 立ち上げ: 数秒間隔で位相をずらした層を積んで濃くする
                }
                else if (now - st.Time > Duration * 0.85f)
                {
                    pass = 0; // 寿命前の静かな積み直し(これ以外で再送はしない)
                }
                else
                {
                    continue;
                }

                float phase = (float)pass / DensifyPasses;
                foreach (var inst in instances)
                {
                    if (inst.Clip != null)
                    {
                        continue; // アニメ再生中のモデルは全身フレーム方式(AnimUpdate)が描く
                    }
                    if (Vector3.Distance(player.transform.position, inst.Pos) <= MaxDistance)
                    {
                        BuildJob(player, inst, phase);
                    }
                }
                playerDraw[player.userID] = new DrawState
                {
                    Time = now,
                    Version = version,
                    Pass = pass + 1,
                };
            }
        }

        // ===== 線の生成 (プレイヤー1人 x モデル1体ぶんを一括生成し、キューで順次送信) =====
        // phase: 0〜1。走査線の位相。2回目以降の重ね描きで前回の線の間を補間し、面を濃くする
        private void BuildJob(BasePlayer player, Instance inst, float phase = 0f)
        {
            // 検証済み: 線同士は深度処理されず「後から送った線が上」(描画順で前後が決まる)。
            // そのため遠→近の順で送り、基準は「モデル正面の固定視点」(正面観賞前提)
            var mesh = inst.Mesh;
            Quaternion rot = Quaternion.Euler(0f, inst.Yaw, 0f);
            float h = mesh.Height * inst.Scale;
            Vector3 eye = inst.Pos
                + rot * Vector3.forward * Mathf.Max(RefDist * 8f, h * 1.5f)
                + Vector3.up * (h * 0.6f);
            var world = new Vector3[mesh.Verts.Length];
            for (int i = 0; i < world.Length; i++)
            {
                world[i] = inst.Pos + rot * (mesh.Verts[i] * inst.Scale);
            }

            int faceCount = mesh.Faces.Count;
            // アニメ再生中は「動く面」を静的描画から外す(動的部位は毎ティック別送)
            bool[] skip = inst.Clip != null ? inst.DynFace : null;
            var faceFront = new bool[faceCount];
            long wanted = 0;
            for (int fi = 0; fi < faceCount; fi++)
            {
                if (skip != null && skip[fi])
                {
                    continue;
                }
                var face = mesh.Faces[fi];
                Vector3 a = world[face[0]], b = world[face[1]], c = world[face[2]];
                Vector3 normal = Vector3.Cross(b - a, c - a);
                Vector3 center = (a + b + c) / 3f;
                bool front = Vector3.Dot(normal, center - eye) <= 0f;
                faceFront[fi] = front;
                if (BackfaceCull && !front)
                {
                    continue;
                }
                for (int t = 2; t < face.Length; t++)
                {
                    wanted += EstimateLines(world[face[0]], world[face[t - 1]], world[face[t]], FillSpacing());
                }
            }
            bool drawCore = phase == 0f && CoreFill && mesh.VNormals != null;
            if (drawCore)
            {
                wanted = (long)(wanted * (1f + 1f / CoreSpacingMul)); // コア層ぶんの概算上乗せ
            }
            if (wanted == 0)
            {
                return;
            }
            float factor = wanted > MaxLinesPerModel ? (float)MaxLinesPerModel / wanted : 1f;

            var lines = new List<QLine>(Mathf.Min((int)(wanted * 1.3f), MaxLinesPerModel * 2));
            float acc = 0f;
            int variant = 0; // 塗り方向は全層で統一(方向を変えると織り目模様に見えるため)

            // ---- インナーコア(裏打ち層): 法線方向に縮めたコピーを暗い同色で最下層に ----
            if (drawCore)
            {
                float shrink = mesh.Height * inst.Scale * CoreShrink;
                var coreWorld = new Vector3[world.Length];
                for (int i = 0; i < world.Length; i++)
                {
                    coreWorld[i] = world[i] - rot * mesh.VNormals[i] * shrink;
                }
                EmitFillLayer(lines, mesh, coreWorld, faceFront, factor, ref acc,
                    0f, 0, FillSpacing() * CoreSpacingMul, CoreShade, true, skip);
            }

            // ---- 表面の塗り ----
            EmitFillLayer(lines, mesh, world, faceFront, factor, ref acc,
                phase, variant, FillSpacing(), 1f, false, skip);

            // 輪郭線 (テクスチャ面は既定OFF。重ね描きでは初回のみ)
            bool outline = phase == 0f && EdgeBoldPx > 0 && (mesh.Tex == null || TexOutline) && mesh.Edges != null;
            if (outline)
            {
                foreach (var e in mesh.Edges)
                {
                    if (skip != null && skip[e.FA] && (e.FB < 0 || skip[e.FB]))
                    {
                        continue; // 両隣とも動く面の辺は静的描画しない
                    }
                    bool fa = faceFront[e.FA];
                    bool fb = e.FB >= 0 && faceFront[e.FB];
                    if (BackfaceCull && !fa && !fb)
                    {
                        continue;
                    }
                    bool silhouette = e.FB >= 0 && fa != fb;
                    if (!silhouette && e.AngleDeg <= CreaseDeg)
                    {
                        continue;
                    }
                    Color edge;
                    if (mesh.Tex != null)
                    {
                        edge = TexEdgeColor;
                    }
                    else
                    {
                        int cf = fa ? e.FA : (e.FB >= 0 ? e.FB : e.FA);
                        edge = (mesh.FaceColors != null ? mesh.FaceColors[cf] : Color.white) * 0.2f;
                    }
                    edge.a = 1f;
                    Vector3 ea = world[e.V0], ebv = world[e.V1];
                    Vector3 mid = (ea + ebv) * 0.5f;
                    float d = Mathf.Max(Vector3.Distance(eye, mid), 0.5f);
                    Vector3 pull = (eye - mid).normalized * Mathf.Clamp(d * 0.01f, 0.01f, 0.5f);
                    Vector3 edgeDir = (ebv - ea).normalized;
                    Vector3 side = Vector3.Cross((eye - mid).normalized, edgeDir).normalized * (d * RadPerPx);
                    for (int o = 0; o < EdgeBoldPx; o++)
                    {
                        Vector3 off = pull + side * (o - (EdgeBoldPx - 1) * 0.5f);
                        lines.Add(new QLine { A = ea + off, B = ebv + off, C = edge });
                    }
                }
            }

            // 画家のアルゴリズム: 後から送った線が上に乗るため、視点(モデル正面の固定基準)から
            // 遠い線を先に、近い線を後に送る。コア(裏打ち)層はさらにその下に敷く
            var arr = lines.ToArray();
            var keys = new float[arr.Length];
            for (int i = 0; i < arr.Length; i++)
            {
                float d2 = ((arr[i].A + arr[i].B) * 0.5f - eye).sqrMagnitude;
                keys[i] = -(d2 + (arr[i].Core ? 1e9f : 0f)); // 遠いほど小さく=先頭。コアは常に先頭側
            }
            Array.Sort(keys, arr);

            jobs.Add(new SendJob { Player = player, Lines = arr, Index = 0 });
        }

        // 1層ぶんの塗り線を生成する(コア層と表面層で共用)
        private void EmitFillLayer(List<QLine> lines, MeshData mesh, Vector3[] world,
            bool[] faceFront, float factor, ref float acc,
            float phase, int variant, float spacing, float shadeMul, bool core, bool[] skip = null)
        {
            for (int fi = 0; fi < mesh.Faces.Count; fi++)
            {
                if (skip != null && skip[fi])
                {
                    continue;
                }
                if (BackfaceCull && !faceFront[fi])
                {
                    continue;
                }
                var face = mesh.Faces[fi];
                Vector3 a = world[face[0]], b = world[face[1]], c = world[face[2]];
                Vector3 normal = Vector3.Cross(b - a, c - a).normalized;
                float brightness = 0.30f + 0.70f * Mathf.Abs(Vector3.Dot(normal, LightDir));
                int[] faceUV = mesh.Tex != null && mesh.FaceUVs != null ? mesh.FaceUVs[fi] : null;

                Color flat = Color.white;
                float shade;
                if (faceUV != null)
                {
                    shade = (ShadeTextured ? brightness : 1f) * shadeMul;
                }
                else
                {
                    Color baseCol = mesh.FaceColors != null ? mesh.FaceColors[fi] : Color.white;
                    flat = baseCol * (brightness * shadeMul);
                    flat.a = 1f;
                    shade = shadeMul;
                }

                for (int t = 2; t < face.Length; t++)
                {
                    FillTriangle(lines, mesh, world[face[0]], world[face[t - 1]], world[face[t]],
                        faceUV, t, flat, shade, factor, ref acc, phase, variant, spacing, core);
                }
            }
        }

        // 基準距離RefDistから見て画面PxSpacing間隔になる固定の世界間隔。
        // プレイヤーの位置に依存しないので、描いた後に動かれても品質が変わらない
        private float FillSpacing()
        {
            return Mathf.Max(Step, RefDist * RadPerPx * PxSpacing);
        }

        private int EstimateLines(Vector3 a, Vector3 b, Vector3 c, float spacing)
        {
            float ab = (a - b).sqrMagnitude, bc = (b - c).sqrMagnitude, ca = (c - a).sqrMagnitude;
            Vector3 p, q, r;
            if (ab >= bc && ab >= ca) { p = a; q = b; r = c; }
            else if (bc >= ca) { p = b; q = c; r = a; }
            else { p = c; q = a; r = b; }
            Vector3 pq = (q - p).normalized;
            float height = (p + pq * Vector3.Dot(r - p, pq) - r).magnitude;
            return Mathf.Clamp(Mathf.CeilToInt(height / spacing), 1, 400);
        }

        // 三角形を平行線で塗る。variantで塗り方向(基準辺)を変え、層を重ねたとき
        // クロスハッチ状に隙間が埋まる。テクスチャ面は線を色の変わり目で分割
        private void FillTriangle(List<QLine> lines, MeshData mesh,
            Vector3 a, Vector3 b, Vector3 c, int[] faceUV, int fan,
            Color flat, float shade, float factor, ref float acc,
            float phase, int variant, float spacing, bool core)
        {
            float ab = (a - b).sqrMagnitude, bc = (b - c).sqrMagnitude, ca = (c - a).sqrMagnitude;
            int k0 = (ab >= bc && ab >= ca) ? 0 : (bc >= ca ? 1 : 2);
            int k = (k0 + variant) % 3;
            Vector3 p, q, r;
            int ip, iq, ir;
            if (k == 0) { p = a; q = b; r = c; ip = 0; iq = 1; ir = 2; }
            else if (k == 1) { p = b; q = c; r = a; ip = 1; iq = 2; ir = 0; }
            else { p = c; q = a; r = b; ip = 2; iq = 0; ir = 1; }

            Vector3 pq = (q - p).normalized;
            float height = (p + pq * Vector3.Dot(r - p, pq) - r).magnitude;
            int n = Mathf.Clamp(Mathf.CeilToInt(height / spacing), 1, 400);

            float want = n * factor;
            int nn = Mathf.FloorToInt(want);
            acc += want - nn;
            if (acc >= 1f)
            {
                nn++;
                acc -= 1f;
            }
            if (nn <= 0)
            {
                return;
            }

            Vector2 uvP = Vector2.zero, uvQ = Vector2.zero, uvR = Vector2.zero;
            bool hasUV = faceUV != null && mesh.UVs != null;
            if (hasUV)
            {
                // 扇形分割(0, fan-1, fan)のコーナーUVを最長辺の並べ替えに合わせる
                var corner = new[] { mesh.UVs[faceUV[0]], mesh.UVs[faceUV[fan - 1]], mesh.UVs[faceUV[fan]] };
                uvP = corner[ip];
                uvQ = corner[iq];
                uvR = corner[ir];
            }

            for (int i = 1; i <= nn; i++)
            {
                // phaseで走査位置をずらす: 重ね描き時に前回の線の隙間を埋める(補間)
                float t = Mathf.Clamp((i - 0.5f + phase) / nn, 0.01f, 0.99f);
                Vector3 s = Vector3.Lerp(r, p, t);
                Vector3 e = Vector3.Lerp(r, q, t);
                if (!hasUV)
                {
                    lines.Add(new QLine { A = s, B = e, C = flat, Core = core });
                    continue;
                }
                Vector2 uvS = Vector2.Lerp(uvR, uvP, t);
                Vector2 uvE = Vector2.Lerp(uvR, uvQ, t);
                EmitTexturedLine(lines, mesh, s, e, uvS, uvE, shade, spacing, core);
            }
        }

        private void EmitTexturedLine(List<QLine> lines, MeshData mesh,
            Vector3 a, Vector3 b, Vector2 uvA, Vector2 uvB, float shade, float spacing, bool core)
        {
            // 線間隔と同じ間隔で採色(縦横で均等な解像度になる)
            int samples = Mathf.Clamp(Mathf.CeilToInt(Vector3.Distance(a, b) / spacing), 1, 48);
            float runT0 = 0f;
            Color runCol = mesh.SampleTex(Vector2.Lerp(uvA, uvB, 0.5f / samples));
            int runKey = QuantKey(runCol);
            int segs = 1;
            for (int s2 = 1; s2 < samples; s2++)
            {
                Color c = mesh.SampleTex(Vector2.Lerp(uvA, uvB, (s2 + 0.5f) / samples));
                int key = QuantKey(c);
                if (key != runKey && segs < TexSegMax)
                {
                    float tSplit = (float)s2 / samples;
                    AddSeg(lines, a, b, runT0, tSplit, runCol, shade, core);
                    runT0 = tSplit;
                    runCol = c;
                    runKey = key;
                    segs++;
                }
            }
            AddSeg(lines, a, b, runT0, 1f, runCol, shade, core);
        }

        private static void AddSeg(List<QLine> lines, Vector3 a, Vector3 b, float t0, float t1, Color c, float shade, bool core)
        {
            Color col = c * shade;
            col.a = 1f;
            lines.Add(new QLine { A = Vector3.Lerp(a, b, t0), B = Vector3.Lerp(a, b, t1), C = col, Core = core });
        }

        private static int QuantKey(Color c)
        {
            return (Mathf.RoundToInt(Mathf.Clamp01(c.r) * 15f) << 8)
                 | (Mathf.RoundToInt(Mathf.Clamp01(c.g) * 15f) << 4)
                 | Mathf.RoundToInt(Mathf.Clamp01(c.b) * 15f);
        }

        // ===== 送信ポンプ (0.1秒ごとに少しずつ送る。終わったらadminフラグを戻す) =====
        private void Pump()
        {
            if (jobs.Count == 0)
            {
                return;
            }
            for (int ji = jobs.Count - 1; ji >= 0; ji--)
            {
                var job = jobs[ji];
                var player = job.Player;
                if (player == null || !player.IsConnected)
                {
                    jobs.RemoveAt(ji);
                    RestoreAdminIfDone(player);
                    continue;
                }
                if (!adminRestore.ContainsKey(player.userID))
                {
                    adminRestore[player.userID] = player.IsAdmin;
                    if (!player.IsAdmin)
                    {
                        player.SetPlayerFlag(BasePlayer.PlayerFlags.IsAdmin, true);
                        player.SendNetworkUpdateImmediate();
                    }
                }
                int end = Mathf.Min(job.Index + LinesPerPump, job.Lines.Length);
                for (; job.Index < end; job.Index++)
                {
                    var l = job.Lines[job.Index];
                    UnityEngine.DDraw.Line(player, l.A, l.B, l.C, Duration, false, ZTest);
                }
                if (job.Index >= job.Lines.Length)
                {
                    jobs.RemoveAt(ji);
                    RestoreAdminIfDone(player);
                }
            }
        }

        private void RestoreAdminIfDone(BasePlayer player)
        {
            if (player == null)
            {
                return;
            }
            foreach (var j in jobs)
            {
                if (j.Player == player)
                {
                    return; // まだ送信中のジョブがある
                }
            }
            if (adminRestore.TryGetValue(player.userID, out bool was))
            {
                adminRestore.Remove(player.userID);
                if (!was && player.IsConnected)
                {
                    player.SetPlayerFlag(BasePlayer.PlayerFlags.IsAdmin, false);
                    player.SendNetworkUpdateImmediate();
                }
            }
        }

        private void SendClear(BasePlayer player)
        {
            if (player == null || !player.IsConnected)
            {
                return;
            }
            // adminフラグ(エンティティ同期)と ddraw.clear(コンソールコマンド)は別経路。
            // 有効化直後に無効化すると無効化が先に届き、clearが非admin扱いで無視される。
            // → 有効化してフラグが確実に伝わってから clear を送り、無効化はさらに遅延する
            bool was = player.IsAdmin;
            if (was)
            {
                UnityEngine.DDraw.Clear(player);
                return;
            }
            player.SetPlayerFlag(BasePlayer.PlayerFlags.IsAdmin, true);
            player.SendNetworkUpdateImmediate();
            timer.Once(0.2f, () =>
            {
                if (player == null || !player.IsConnected)
                {
                    return;
                }
                UnityEngine.DDraw.Clear(player);
                timer.Once(0.4f, () =>
                {
                    // 描画系が別途adminを要求中(adminRestoreに記録あり)なら戻さない
                    if (player != null && player.IsConnected && !adminRestore.ContainsKey(player.userID))
                    {
                        player.SetPlayerFlag(BasePlayer.PlayerFlags.IsAdmin, false);
                        player.SendNetworkUpdateImmediate();
                    }
                });
            });
        }

        // ===== メッシュ読み込み =====
        private static void BuildEdges(MeshData m)
        {
            // UV継ぎ目で頂点が複製されていても隣接判定できるよう、位置で同一視する
            var canon = new int[m.Verts.Length];
            var posMap = new Dictionary<Vector3, int>(m.Verts.Length);
            for (int i = 0; i < m.Verts.Length; i++)
            {
                var v = m.Verts[i];
                var key = new Vector3(Mathf.Round(v.x * 8192f), Mathf.Round(v.y * 8192f), Mathf.Round(v.z * 8192f));
                if (!posMap.TryGetValue(key, out int ci))
                {
                    ci = i;
                    posMap[key] = ci;
                }
                canon[i] = ci;
            }

            var normals = new Vector3[m.Faces.Count];
            for (int fi = 0; fi < m.Faces.Count; fi++)
            {
                var f = m.Faces[fi];
                normals[fi] = Vector3.Cross(m.Verts[f[1]] - m.Verts[f[0]], m.Verts[f[2]] - m.Verts[f[0]]).normalized;
            }
            var map = new Dictionary<long, int>();
            var list = new List<Edge>(m.Faces.Count * 2);
            for (int fi = 0; fi < m.Faces.Count; fi++)
            {
                var f = m.Faces[fi];
                for (int i = 0; i < f.Length; i++)
                {
                    int a = canon[f[i]], b = canon[f[(i + 1) % f.Length]];
                    if (a == b)
                    {
                        continue;
                    }
                    long key = a < b ? ((long)a << 32) | (uint)b : ((long)b << 32) | (uint)a;
                    if (map.TryGetValue(key, out int ei))
                    {
                        var e = list[ei];
                        if (e.FB < 0)
                        {
                            e.FB = fi;
                            e.AngleDeg = Vector3.Angle(normals[e.FA], normals[fi]);
                            list[ei] = e;
                        }
                    }
                    else
                    {
                        map[key] = list.Count;
                        list.Add(new Edge { V0 = a, V1 = b, FA = fi, FB = -1, AngleDeg = 360f });
                    }
                }
            }
            m.Edges = list.ToArray();

            // 頂点法線(UV継ぎ目の複製頂点は位置で統合して滑らかに)とモデル高さ
            var vsum = new Vector3[m.Verts.Length];
            for (int fi = 0; fi < m.Faces.Count; fi++)
            {
                foreach (var vi in m.Faces[fi])
                {
                    vsum[canon[vi]] += normals[fi];
                }
            }
            m.VNormals = new Vector3[m.Verts.Length];
            float minY = float.MaxValue, maxY = float.MinValue;
            for (int i = 0; i < m.Verts.Length; i++)
            {
                Vector3 n = vsum[canon[i]];
                m.VNormals[i] = n.sqrMagnitude > 1e-12f ? n.normalized : Vector3.up;
                minY = Mathf.Min(minY, m.Verts[i].y);
                maxY = Mathf.Max(maxY, m.Verts[i].y);
            }
            m.Height = Mathf.Max(maxY - minY, 0.01f);
        }

        private static int HexVal(char c)
        {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return 0;
        }

        private MeshData LoadObj(string name)
        {
            if (meshCache.TryGetValue(name, out var cached))
            {
                return cached;
            }
            string path = Path.Combine(Interface.Oxide.DataDirectory, "MeshDraw", name + ".obj");
            if (!File.Exists(path))
            {
                return null;
            }
            var verts = new List<Vector3>();
            var uvs = new List<Vector2>();
            var faces = new List<int[]>();
            var faceUVs = new List<int[]>();
            bool anyUV = false;
            List<Color> faceColors = null;
            Color currentColor = Color.white;
            Color32[] tex = null;
            int texW = 0, texH = 0, texRow = 0;
            foreach (var raw in File.ReadAllLines(path))
            {
                var line = raw.Trim();
                if (line.StartsWith("tex "))
                {
                    var tt = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    if (tt.Length >= 3 && int.TryParse(tt[1], out texW) && int.TryParse(tt[2], out texH))
                    {
                        tex = new Color32[texW * texH];
                    }
                }
                else if (line.StartsWith("tx ") && tex != null && texRow < texH)
                {
                    string hex = line.Substring(3).Trim();
                    int count = Mathf.Min(texW, hex.Length / 6);
                    int baseIdx = texRow * texW;
                    for (int x = 0; x < count; x++)
                    {
                        int o = x * 6;
                        tex[baseIdx + x] = new Color32(
                            (byte)((HexVal(hex[o]) << 4) | HexVal(hex[o + 1])),
                            (byte)((HexVal(hex[o + 2]) << 4) | HexVal(hex[o + 3])),
                            (byte)((HexVal(hex[o + 4]) << 4) | HexVal(hex[o + 5])),
                            255);
                    }
                    texRow++;
                }
                else if (line.StartsWith("vt "))
                {
                    var tt = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    if (tt.Length >= 3)
                    {
                        uvs.Add(new Vector2(
                            float.Parse(tt[1], CultureInfo.InvariantCulture),
                            float.Parse(tt[2], CultureInfo.InvariantCulture)));
                    }
                }
                else if (line.StartsWith("c "))
                {
                    // 拡張: 面色指定 "c R G B" (0-1)。以降の f 行に適用
                    var ct = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    if (ct.Length >= 4)
                    {
                        if (faceColors == null)
                        {
                            faceColors = new List<Color>();
                            for (int i = 0; i < faces.Count; i++)
                            {
                                faceColors.Add(Color.white);
                            }
                        }
                        currentColor = new Color(
                            float.Parse(ct[1], CultureInfo.InvariantCulture),
                            float.Parse(ct[2], CultureInfo.InvariantCulture),
                            float.Parse(ct[3], CultureInfo.InvariantCulture));
                    }
                }
                else if (line.StartsWith("v "))
                {
                    var tok = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    if (tok.Length >= 4)
                    {
                        // OBJは右手系、Unityは左手系なのでXを反転
                        verts.Add(new Vector3(
                            -float.Parse(tok[1], CultureInfo.InvariantCulture),
                            float.Parse(tok[2], CultureInfo.InvariantCulture),
                            float.Parse(tok[3], CultureInfo.InvariantCulture)));
                    }
                }
                else if (line.StartsWith("f "))
                {
                    var tok = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    var idx = new List<int>();
                    var uvIdx = new List<int>();
                    bool hasUV = true;
                    for (int i = 1; i < tok.Length; i++)
                    {
                        var parts = tok[i].Split('/');
                        if (int.TryParse(parts[0], out var vi))
                        {
                            idx.Add(vi > 0 ? vi - 1 : verts.Count + vi);
                        }
                        if (parts.Length > 1 && int.TryParse(parts[1], out var ti))
                        {
                            uvIdx.Add(ti > 0 ? ti - 1 : uvs.Count + ti);
                        }
                        else
                        {
                            hasUV = false;
                        }
                    }
                    if (idx.Count >= 3)
                    {
                        idx.Reverse(); // X反転に合わせて巻き順も反転
                        faces.Add(idx.ToArray());
                        faceColors?.Add(currentColor);
                        if (hasUV && uvIdx.Count == idx.Count)
                        {
                            uvIdx.Reverse();
                            faceUVs.Add(uvIdx.ToArray());
                            anyUV = true;
                        }
                        else
                        {
                            faceUVs.Add(null);
                        }
                    }
                }
            }
            if (verts.Count == 0 || faces.Count == 0)
            {
                return null;
            }
            var mesh = new MeshData
            {
                Verts = verts.ToArray(),
                Faces = faces,
                FaceColors = faceColors,
                UVs = anyUV ? uvs.ToArray() : null,
                FaceUVs = anyUV ? faceUVs : null,
                Tex = (tex != null && anyUV && texRow == texH) ? tex : null,
                TexW = texW,
                TexH = texH,
            };
            BuildEdges(mesh);
            try
            {
                mesh.Skin = LoadSkin(name);
                if (mesh.Skin != null && mesh.Skin.VertBones.Length != mesh.Verts.Length)
                {
                    PrintWarning($"{name}.skin の頂点数がOBJと不一致のため無効化 ({mesh.Skin.VertBones.Length} vs {mesh.Verts.Length})");
                    mesh.Skin = null;
                }
            }
            catch (Exception e)
            {
                PrintError($"{name}.skin の読み込みに失敗: {e.Message}");
            }
            meshCache[name] = mesh;
            return mesh;
        }

        private static MeshData BuiltinCube()
        {
            var v = new[]
            {
                new Vector3(-0.5f, 0f, -0.5f), new Vector3(0.5f, 0f, -0.5f),
                new Vector3(0.5f, 0f, 0.5f),  new Vector3(-0.5f, 0f, 0.5f),
                new Vector3(-0.5f, 1f, -0.5f), new Vector3(0.5f, 1f, -0.5f),
                new Vector3(0.5f, 1f, 0.5f),  new Vector3(-0.5f, 1f, 0.5f),
            };
            // 巻き順は「外から見て Cross(b-a, c-a) が外向き法線」になる順
            var f = new List<int[]>
            {
                new[] { 0, 1, 2, 3 }, // 底 (法線 -Y)
                new[] { 7, 6, 5, 4 }, // 天 (法線 +Y)
                new[] { 4, 5, 1, 0 }, // 前 (法線 -Z)
                new[] { 5, 6, 2, 1 }, // 右 (法線 +X)
                new[] { 6, 7, 3, 2 }, // 奥 (法線 +Z)
                new[] { 0, 3, 7, 4 }, // 左 (法線 -X)
            };
            var mesh = new MeshData { Verts = v, Faces = f };
            BuildEdges(mesh);
            return mesh;
        }
    }
}
