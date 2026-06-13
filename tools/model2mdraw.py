#!/usr/bin/env python3
"""
3Dモデル → MeshSurfaceDraw プラグイン用 拡張OBJ 変換ツール

対応入力:
  PMX (MMDモデル。テクスチャ込みで直接変換)
  OBJ / GLB / GLTF / STL / PLY / 3MF (trimeshが読める形式)
  (FBXは非対応。Blender等で一度GLBに書き出してください)

テクスチャがあるモデルは「テクスチャモード」で変換され、
縮小したテクスチャ画像そのものを拡張OBJに埋め込みます。
プラグインは塗り線をテクスチャの色の変わり目で分割して描くため、
顔の目・口のような描き込みも(線の密度なりに)再現されます。

テクスチャがないモデルは従来どおり面ごとの代表色(c R G B 行)で出力します。

使い方:
  python model2mdraw.py miku.pmx -H 1.6          # 等身大ミク(テクスチャ込み)
  python model2mdraw.py miku.pmx -f 6000 -H 1.6  # 6000面に削減(UVは保持)
  python model2mdraw.py input.glb                # GLBも同様
  python model2mdraw.py input.stl --zup          # 色なしSTL(従来モード)

出力OBJを oxide/data/MeshDraw/<名前>.obj に置き、ゲーム内で /mdraw <名前>
"""
import argparse
import os
import struct
import sys

import numpy as np
import trimesh
from PIL import Image


# ============================================================
# PMX 読み込み (形状+UV+テクスチャのみ。ボーン/モーフは読み飛ばす)
# ============================================================
class _Reader:
    def __init__(self, data):
        self.d = data
        self.o = 0

    def read(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.o)
        self.o += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def take(self, n):
        b = self.d[self.o:self.o + n]
        self.o += n
        return b


def _pmx_text(r, enc):
    n = r.read("i")
    return r.take(n).decode("utf-16-le" if enc == 0 else "utf-8", errors="replace")


_IDX_FMT = {1: "b", 2: "h", 4: "i"}          # 符号つき(テクスチャ等。-1=なし)
_VIDX_DTYPE = {1: np.uint8, 2: np.uint16, 4: np.int32}  # 頂点indexは符号なし


def load_pmx(path):
    """PMXを読み、マテリアルごとのサブメッシュのリストと
    スキニング情報(ボーン+頂点ウェイト)を返す"""
    data = open(path, "rb").read()
    r = _Reader(data)
    if r.take(4) != b"PMX ":
        raise ValueError("PMXファイルではありません")
    r.read("f")  # version
    gcount = r.read("B")
    g = list(r.take(gcount))
    enc, adduv, vsize, tsize, bsize = g[0], g[1], g[2], g[3], g[5]
    for _ in range(4):
        _pmx_text(r, enc)  # モデル名/コメント(日英)

    # ---- 頂点 (ウェイトも保持) ----
    vn = r.read("i")
    verts = np.empty((vn, 3), dtype=np.float64)
    uvs = np.empty((vn, 2), dtype=np.float64)
    wbone = np.zeros((vn, 4), dtype=np.int64)
    wval = np.zeros((vn, 4), dtype=np.float64)
    bfmt = _IDX_FMT[bsize]
    for i in range(vn):
        x, y, z, _nx, _ny, _nz, u, v = r.read("8f")
        verts[i] = (x, y, z)
        uvs[i] = (u, v)
        if adduv:
            r.take(16 * adduv)
        wt = r.read("B")
        if wt == 0:
            wbone[i, 0] = r.read(bfmt)
            wval[i, 0] = 1.0
        elif wt == 1:
            wbone[i, 0] = r.read(bfmt)
            wbone[i, 1] = r.read(bfmt)
            w0 = r.read("f")
            wval[i, 0] = w0
            wval[i, 1] = 1.0 - w0
        elif wt in (2, 4):
            for j in range(4):
                wbone[i, j] = r.read(bfmt)
            ws = r.read("4f")
            for j in range(4):
                wval[i, j] = ws[j]
        elif wt == 3:  # SDEF → BDEF2近似
            wbone[i, 0] = r.read(bfmt)
            wbone[i, 1] = r.read(bfmt)
            w0 = r.read("f")
            wval[i, 0] = w0
            wval[i, 1] = 1.0 - w0
            r.read("9f")
        r.read("f")  # エッジ倍率

    # ---- 面 ----
    icount = r.read("i")
    idx = np.frombuffer(data, dtype=_VIDX_DTYPE[vsize], count=icount, offset=r.o).astype(np.int64)
    r.o += icount * vsize
    faces_all = idx.reshape(-1, 3)

    # ---- テクスチャパス ----
    tn = r.read("i")
    texpaths = [_pmx_text(r, enc) for _ in range(tn)]

    # ---- マテリアル ----
    mn = r.read("i")
    tfmt = _IDX_FMT[tsize]
    mats = []
    for _ in range(mn):
        _pmx_text(r, enc); _pmx_text(r, enc)  # 名前(日英)
        dr, dg, db, da = r.read("4f")
        r.read("3f"); r.read("f"); r.read("3f")  # specular/power/ambient
        r.read("B")                              # 描画フラグ
        r.read("4f"); r.read("f")                # エッジ色/サイズ
        tex = r.read(tfmt)
        r.read(tfmt); r.read("B")                # スフィア
        if r.read("B"):                          # 共有toon
            r.read("B")
        else:
            r.read(tfmt)
        _pmx_text(r, enc)                        # メモ
        fcount = r.read("i") // 3
        mats.append({"diffuse": (dr, dg, db), "alpha": da, "tex": tex, "fcount": fcount})

    # ---- ボーン ----
    bn = r.read("i")
    bones = []
    for _ in range(bn):
        bname = _pmx_text(r, enc)
        _pmx_text(r, enc)  # 英名
        bx, by, bz = r.read("3f")
        parent = r.read(bfmt)
        r.read("i")  # 変形階層
        flags = r.read("H")
        if flags & 0x0001:
            r.read(bfmt)        # 接続先ボーン
        else:
            r.read("3f")        # 接続先オフセット
        if flags & (0x0100 | 0x0200):
            r.read(bfmt); r.read("f")  # 付与親
        if flags & 0x0400:
            r.read("3f")        # 軸固定
        if flags & 0x0800:
            r.read("6f")        # ローカル軸
        if flags & 0x2000:
            r.read("i")         # 外部親
        if flags & 0x0020:      # IK
            r.read(bfmt); r.read("i"); r.read("f")
            for _ in range(r.read("i")):
                r.read(bfmt)
                if r.read("B"):
                    r.read("6f")
        bones.append({"name": bname, "parent": parent, "pos": np.array([bx, by, -bz])})

    # ---- 座標変換: PMXは左手系(z奥)・UVはv=0が上 → 右手系y-up・v=0が下 ----
    # z反転は鏡映で面の向きが裏返るため、巻き順も反転して外向きを保つ
    verts[:, 2] *= -1.0
    uvs[:, 1] = 1.0 - uvs[:, 1]
    faces_all = faces_all[:, ::-1]

    base = os.path.dirname(os.path.abspath(path))
    subs = []
    fo = 0
    for m in mats:
        faces = faces_all[fo:fo + m["fcount"]]
        fo += m["fcount"]
        if m["fcount"] == 0 or m["alpha"] < 0.1:
            continue  # 透明マテリアル(エフェクト用など)はスキップ
        img = None
        if m["tex"] >= 0 and m["tex"] < len(texpaths):
            p = _find_tex(base, texpaths[m["tex"]])
            if p:
                img = Image.open(p)
        subs.append({"verts": verts, "faces": faces, "uv": uvs,
                     "image": img, "color": np.array(m["diffuse"])})
    skin = {"bones": bones, "wbone": wbone, "wval": wval}
    return subs, skin


# ============================================================
# VMD モーション読み込み (ボーンキーフレームのみ)
# ============================================================
def load_vmd(path, bone_name_to_index):
    """VMDを読み、ボーンindexごとのキーフレーム配列を返す。
    座標系はPMXと同じくz反転で右手系に変換する"""
    data = open(path, "rb").read()
    r = _Reader(data)
    header = r.take(30)
    if not header.startswith(b"Vocaloid Motion Data"):
        raise ValueError("VMDファイルではありません")
    r.take(20)  # モデル名
    n = r.read("i")
    keys = {}
    skipped = set()
    for _ in range(n):
        raw = r.take(15)
        name = raw.split(b"\x00")[0].decode("cp932", errors="replace")
        frame = r.read("I")
        px, py, pz = r.read("3f")
        qx, qy, qz, qw = r.read("4f")
        r.take(64)  # ベジェ補間パラメータ(線形補間で近似するため未使用)
        bi = bone_name_to_index.get(name)
        if bi is None:
            skipped.add(name)
            continue
        # z反転の鏡映で: 平行移動はz符号反転、回転は(qx,qy)符号反転
        keys.setdefault(bi, []).append((frame, px, py, -pz, -qx, -qy, qz, qw))
    for bi in keys:
        keys[bi].sort(key=lambda k: k[0])
    if skipped:
        print(f"  VMD     : モデルに無いボーン {len(skipped)}個をスキップ "
              f"(例: {', '.join(list(skipped)[:5])})")
    return keys


def make_test_motion(bone_name_to_index):
    """VMDなしで動作確認するための組み込みモーション
    (右腕を振る+首をかしげる, 8秒ループ)"""
    def axis_angle(ax, deg):
        rad = np.radians(deg) / 2.0
        s = np.sin(rad)
        return np.array([ax[0] * s, ax[1] * s, ax[2] * s, np.cos(rad)])

    keys = {}

    def add(name, frame, q):
        bi = bone_name_to_index.get(name)
        if bi is None:
            return
        # load_vmdと同じz反転変換: (-qx,-qy,qz,qw)
        keys.setdefault(bi, []).append((frame, 0.0, 0.0, 0.0, -q[0], -q[1], q[2], q[3]))

    # 上半身(胴)は動かさない: 動かすと体の大半が「動く面」になり低密度化するため。
    # 頭と腕だけの局所モーションに留め、静止部分を高密度のまま残す
    total = 240  # 8秒
    for f in range(0, total + 1, 10):
        t = f / total * 2 * np.pi
        # 腕を肩から前後にゆっくり振る(左右で逆位相)
        add("右腕", f, axis_angle((0, 0, 1), -14 * np.sin(t) - 6))
        add("左腕", f, axis_angle((0, 0, 1), 14 * np.sin(t) + 6))
        # 肘を軽く曲げる
        add("右ひじ", f, axis_angle((0, 1, 0), 10 + 8 * np.sin(t)))
        add("左ひじ", f, axis_angle((0, 1, 0), -10 - 8 * np.sin(t)))
        # 首をかしげる
        add("頭", f, axis_angle((0, 0, 1), 10 * np.sin(t * 0.5)))
    for bi in keys:
        keys[bi].sort(key=lambda k: k[0])
    if not keys:
        print("  警告: 標準ボーン名(右腕/頭/上半身)が見つからずテストモーションを生成できません")
    return keys


def _find_tex(base, rel):
    rel = rel.replace("\\", os.sep).replace("/", os.sep)
    p = os.path.join(base, rel)
    if os.path.exists(p):
        return p
    d, f = os.path.split(p)
    if os.path.isdir(d):
        for cand in os.listdir(d):
            if cand.lower() == f.lower():
                return os.path.join(d, cand)
    return None


# ============================================================
# trimesh 系 (GLB/GLTF/OBJ/STL...) → サブメッシュ列
# ============================================================
def load_trimesh_submeshes(path):
    loaded = trimesh.load(path)
    geoms = loaded.dump(concatenate=False) if isinstance(loaded, trimesh.Scene) else [loaded]
    subs = []
    for geom in geoms:
        if not isinstance(geom, trimesh.Trimesh) or len(geom.faces) == 0:
            continue
        uv = None
        img = None
        color = np.array([0.8, 0.8, 0.8])
        vis = geom.visual
        if isinstance(vis, trimesh.visual.TextureVisuals):
            uv = np.asarray(vis.uv, dtype=np.float64) if vis.uv is not None else None
            mat = vis.material
            if mat is not None:
                img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
                bcf = getattr(mat, "baseColorFactor", None)
                if bcf is not None:
                    c = np.asarray(bcf, dtype=np.float64)[:3]
                    color = c / 255.0 if c.max() > 1.0 else c
                diffuse = getattr(mat, "diffuse", None)
                if img is None and diffuse is not None:
                    c = np.asarray(diffuse, dtype=np.float64)[:3]
                    color = c / 255.0 if c.max() > 1.0 else c
        elif hasattr(vis, "face_colors") and vis.face_colors is not None:
            fc = np.asarray(vis.face_colors, dtype=np.float64)
            if len(fc) == len(geom.faces):
                # 頂点色/面色モデル: 面色の平均…ではなく面ごとの色を保持したいので
                # 後段のCOLORモード(従来パイプライン)で処理する
                pass
        subs.append({"verts": np.asarray(geom.vertices, dtype=np.float64),
                     "faces": np.asarray(geom.faces, dtype=np.int64),
                     "uv": uv if uv is not None and len(uv) == len(geom.vertices) else None,
                     "image": img, "color": color})
    return subs


# ============================================================
# テクスチャモード: アトラス作成・UV保持削減・拡張OBJ出力
# ============================================================
def downsample(img, color_mul, max_dim):
    """RGBA→縮小。透明部分は白合成せず、最寄りの不透明色で埋める(にじみ/白点防止)。
    (H,W,3) float 0-1 を返す"""
    img = img.convert("RGBA")
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))

    a = np.asarray(img, dtype=np.float64) / 255.0
    rgb, alpha = a[:, :, :3], a[:, :, 3]
    # premultiplied で縮小し、透明texelの色(よく白)が島の縁へにじむのを防ぐ
    pm = Image.fromarray(np.clip(rgb * alpha[:, :, None] * 255, 0, 255).astype(np.uint8))
    am = Image.fromarray(np.clip(alpha * 255, 0, 255).astype(np.uint8))
    if scale < 1.0:
        pm = pm.resize((nw, nh), Image.LANCZOS)
        am = am.resize((nw, nh), Image.LANCZOS)
    pm = np.asarray(pm, dtype=np.float64) / 255.0
    am = np.asarray(am, dtype=np.float64) / 255.0
    out = pm / np.maximum(am[:, :, None], 1e-4)

    # 透明領域には最寄りの不透明色を流し込む(UVの丸めで拾っても白にならない)
    hole = am < 0.15
    if hole.any() and not hole.all():
        try:
            from scipy import ndimage
            idx = ndimage.distance_transform_edt(hole, return_distances=False, return_indices=True)
            out = out[idx[0], idx[1]]
        except ImportError:
            pass
    return np.clip(out * color_mul, 0.0, 1.0)


def drop_transparent_faces(parts, thresh=0.4):
    """テクスチャの透明部分で形を抜いている面(髪の毛先など)を削除する。
    透明はddrawで表現できず白いベタ面になってしまうため"""
    dropped = 0
    for p in parts:
        if p["image"] is None or p["uv"] is None or len(p["f"]) == 0:
            continue
        a = np.asarray(p["image"].convert("RGBA"), dtype=np.float64)[:, :, 3] / 255.0
        if a.min() > 0.9:
            continue  # 透明なし
        h, w = a.shape
        cuv = p["uv"][p["f"]]                              # (F,3,2)
        cent = cuv.mean(axis=1, keepdims=True)
        samp = np.concatenate([cuv, cent, (cuv + cent) * 0.5], axis=1)  # 角3+重心+中点3
        u = np.mod(samp[:, :, 0], 1.0)
        v = np.mod(samp[:, :, 1], 1.0)
        x = np.clip((u * (w - 1)).round().astype(int), 0, w - 1)
        y = np.clip(((1.0 - v) * (h - 1)).round().astype(int), 0, h - 1)
        keep = a[y, x].mean(axis=1) > thresh
        dropped += int((~keep).sum())
        p["f"] = p["f"][keep]
    if dropped:
        print(f"  透明面  : {dropped}面を削除 (テクスチャの透明部分で抜かれていた面)")
    return parts


def decimate_with_uv(verts, faces, uv, target_faces):
    """fast_simplificationで削減し、UVは元メッシュの最近傍点から重心座標で転写する"""
    import fast_simplification
    reduction = 1.0 - (target_faces / len(faces))
    nv, nf = fast_simplification.simplify(verts, faces.astype(np.int64), target_reduction=reduction)
    nf = nf.astype(np.int64)
    orig = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    with np.errstate(divide="ignore", invalid="ignore"):
        closest, _dist, tid = trimesh.proximity.closest_point(orig, nv)
        bary = trimesh.triangles.points_to_barycentric(orig.triangles[tid], closest)
        new_uv = (uv[faces[tid]] * bary[:, :, None]).sum(axis=1)
    # 縮退三角形でNaNになった頂点は、元メッシュの最寄り頂点のUVで代替
    bad = ~np.isfinite(new_uv).all(axis=1)
    if bad.any():
        from scipy.spatial import cKDTree
        nearest = cKDTree(verts).query(nv[bad])[1]
        new_uv[bad] = uv[nearest]
    return nv, nf, new_uv


def remove_hidden_faces(parts):
    """多視点のzバッファ投票で「どこから見ても見えない面」(服の下の体など)を落とす。
    残った面の取りこぼしを防ぐため判定は保守的(怪しい面は残す)"""
    pts_list, fid_list = [], []
    fid0 = 0
    for p in parts:
        tri = p["v"][p["f"]]                      # (F,3,3)
        cent = tri.mean(axis=1)
        samp = np.concatenate([cent[:, None, :], tri * 0.7 + cent[:, None, :] * 0.3], axis=1)
        pts_list.append(samp.reshape(-1, 3))
        fid_list.append(np.repeat(np.arange(len(p["f"])) + fid0, 4))
        fid0 += len(p["f"])
    P = np.vstack(pts_list)
    FID = np.concatenate(fid_list)
    lo, hi = P.min(axis=0), P.max(axis=0)
    height = hi[1] - lo[1]
    tol = height * 0.0015
    grid = 768

    dirs = []
    for d in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]):
        dirs.append(np.array(d, dtype=np.float64))
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                dirs.append(np.array([sx, sy, sz], dtype=np.float64) / np.sqrt(3))

    visible = np.zeros(fid0, dtype=bool)
    for d in dirs:
        # dを「視線の手前方向」とする正規直交基底
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
        r = np.cross(up, d); r /= np.linalg.norm(r)
        u = np.cross(d, r)
        x = P @ r; y = P @ u; z = P @ d
        xi = np.clip(((x - x.min()) / max(x.max() - x.min(), 1e-9) * (grid - 1)).astype(int), 0, grid - 1)
        yi = np.clip(((y - y.min()) / max(y.max() - y.min(), 1e-9) * (grid - 1)).astype(int), 0, grid - 1)
        cell = yi * grid + xi
        maxz = np.full(grid * grid, -np.inf)
        np.maximum.at(maxz, cell, z)
        visible[FID[z >= maxz[cell] - tol]] = True

    fid0 = 0
    dropped = 0
    for p in parts:
        mask = visible[fid0:fid0 + len(p["f"])]
        fid0 += len(p["f"])
        dropped += int((~mask).sum())
        p["f"] = p["f"][mask]
    if dropped:
        print(f"  隠面除去: {dropped}面を削除 (服の下の体・口内など)")
    return parts


def offset_decals(parts):
    """別マテリアルの面とほぼ同一位置・同一向きの面(目・眉などのデカール)を
    法線方向に押し出してZファイトを防ぐ。後のマテリアル(上に描かれる側)を動かす"""
    try:
        from scipy.spatial import cKDTree
    except ImportError:
        return parts
    all_v = np.vstack([p["v"] for p in parts])
    height = all_v[:, 1].max() - all_v[:, 1].min()
    near = height * 0.002   # 「重なっている」とみなす距離
    push = height * 0.0015  # 押し出し量

    cents, norms, pids = [], [], []
    for pi, p in enumerate(parts):
        tri = p["v"][p["f"]]
        c = tri.mean(axis=1)
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        cents.append(c); norms.append(n); pids.append(np.full(len(c), pi))
    C = np.vstack(cents); N = np.vstack(norms); PI = np.concatenate(pids)

    tree = cKDTree(C)
    pushed_total = 0
    for pi in range(1, len(parts)):
        p = parts[pi]
        sel = PI == pi
        myC, myN = C[sel], N[sel]
        pairs = tree.query_ball_point(myC, near)
        decal_face = np.zeros(len(myC), dtype=bool)
        for i, nb in enumerate(pairs):
            for j in nb:
                if PI[j] < pi and abs(float(myN[i] @ N[j])) > 0.9:
                    decal_face[i] = True
                    break
        if not decal_face.any():
            continue
        # デカール面の頂点を面法線の平均方向へ押し出す
        vset = np.unique(p["f"][decal_face])
        vnorm = np.zeros((len(p["v"]), 3))
        for fi in np.nonzero(decal_face)[0]:
            for vi in p["f"][fi]:
                vnorm[vi] += myN[fi]
        ln = np.linalg.norm(vnorm[vset], axis=1, keepdims=True)
        p["v"] = p["v"].copy()
        p["v"][vset] += vnorm[vset] / np.maximum(ln, 1e-12) * push
        pushed_total += int(decal_face.sum())
    if pushed_total:
        print(f"  デカール: {pushed_total}面を{push:.3f}単位 外側へ押し出し (Zファイト防止)")
    return parts


def convert_textured(subs, args, out_path, skin=None):
    texsize = args.texsize
    # ---- 各サブメッシュを「使用頂点だけの独立メッシュ」に正規化 ----
    parts = []
    total_faces = 0
    for s in subs:
        used = np.unique(s["faces"])
        remap = np.full(len(s["verts"]), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        v = s["verts"][used]
        f = remap[s["faces"]]
        uv = s["uv"][used] if s["uv"] is not None else None
        parts.append({"v": v, "f": f, "uv": uv, "image": s["image"], "color": s["color"],
                      "orig": used})
        total_faces += len(f)
    print(f"  マテリアル数: {len(parts)} / 総面数: {total_faces}")

    parts = drop_transparent_faces(parts)
    parts = [p for p in parts if len(p["f"]) > 0]
    if not args.keep_hidden:
        parts = offset_decals(parts)
        parts = remove_hidden_faces(parts)
        parts = [p for p in parts if len(p["f"]) > 0]
        total_faces = sum(len(p["f"]) for p in parts)
        print(f"  前処理後: {total_faces}面")

    # ---- 全パートを位置ウェルドで一体化 ----
    # マテリアル別に削減すると境界の辺がズレて亀裂になるため、必ず一体で扱う。
    # UVはコーナー(面の角)ごとに持つので、UV継ぎ目があっても位置は溶接できる
    all_v = np.vstack([p["v"] for p in parts])
    offs = np.cumsum([0] + [len(p["v"]) for p in parts[:-1]])
    all_f = np.vstack([p["f"] + o for p, o in zip(parts, offs)])
    fpart = np.concatenate([np.full(len(p["f"]), pi, dtype=np.int64)
                            for pi, p in enumerate(parts)])

    corner_uv = np.full((len(all_f), 3, 2), 0.5)
    row = 0
    for p in parts:
        n = len(p["f"])
        if p["uv"] is not None:
            corner_uv[row:row + n] = p["uv"][p["f"]]
        row += n

    # 元の頂点プールへの対応(スキンウェイトの転写用)
    all_orig = np.concatenate([p["orig"] for p in parts]) if skin is not None else None

    span = float((all_v.max(axis=0) - all_v.min(axis=0)).max())
    qkey = np.round(all_v / (span * 1e-6)).astype(np.int64)
    _, uidx, inv = np.unique(qkey, axis=0, return_index=True, return_inverse=True)
    wv = all_v[uidx]
    vert_pool = all_orig[uidx] if all_orig is not None else None  # 最終頂点→プール頂点
    wf = inv[all_f]
    good = (wf[:, 0] != wf[:, 1]) & (wf[:, 1] != wf[:, 2]) & (wf[:, 2] != wf[:, 0])
    wf = wf[good]
    fpart = fpart[good]
    corner_uv = corner_uv[good]

    # ---- ポリゴン削減 (-f 指定時。一体のまま削減し、UV/材質は元の面から転写) ----
    if args.faces is not None and len(wf) > args.faces:
        import fast_simplification
        from scipy.spatial import cKDTree
        nv, nf = fast_simplification.simplify(
            wv, wf, target_reduction=1.0 - args.faces / len(wf))
        nf = nf.astype(np.int64)
        # 材質割当: 重心+3角それぞれの最寄りの元の面で多数決(境界での誤割当を抑制)
        tree = cKDTree(wv[wf].mean(axis=1))
        votes = [tree.query(nv[nf].mean(axis=1))[1]]
        for k in range(3):
            votes.append(tree.query(nv[nf[:, k]])[1])
        vote_parts = fpart[np.stack(votes, axis=1)]  # (F,4)
        new_fpart = np.array([np.bincount(row).argmax() for row in vote_parts], dtype=np.int64)

        # 票が割れた面は、候補パーツの表面までの実距離が最小のものに決める
        disagree = (vote_parts != new_fpart[:, None]).any(axis=1)
        dis_idx = np.nonzero(disagree)[0]
        if len(dis_idx):
            cand = vote_parts[dis_idx]                      # (D,4)
            cents = nv[nf[dis_idx]].mean(axis=1)
            bestd = np.full(len(dis_idx), np.inf)
            best = new_fpart[dis_idx].copy()
            for pi, p in enumerate(parts):
                mask = (cand == pi).any(axis=1)
                if not mask.any():
                    continue
                orig = trimesh.Trimesh(vertices=p["v"], faces=p["f"], process=False)
                with np.errstate(divide="ignore", invalid="ignore"):
                    _c, dist, _t = trimesh.proximity.closest_point(orig, cents[mask])
                rows = np.nonzero(mask)[0]
                upd = dist < bestd[rows]
                bestd[rows[upd]] = dist[upd]
                best[rows[upd]] = pi
            new_fpart[dis_idx] = best
            print(f"  材質境界: {len(dis_idx)}面を実距離で再判定")
        # コーナーUV転写: 各コーナーを「その材質の元メッシュ」上の最近傍点へ射影
        new_cuv = np.full((len(nf), 3, 2), 0.5)
        for pi, p in enumerate(parts):
            sel = np.nonzero(new_fpart == pi)[0]
            if len(sel) == 0 or p["uv"] is None:
                continue
            pts = nv[nf[sel]].reshape(-1, 3)
            orig = trimesh.Trimesh(vertices=p["v"], faces=p["f"], process=False)
            with np.errstate(divide="ignore", invalid="ignore"):
                closest, _d, tid = trimesh.proximity.closest_point(orig, pts)
                bary = trimesh.triangles.points_to_barycentric(orig.triangles[tid], closest)
                cuv = (p["uv"][p["f"][tid]] * bary[:, :, None]).sum(axis=1)
            bad = ~np.isfinite(cuv).all(axis=1)
            if bad.any():
                nn = cKDTree(p["v"]).query(pts[bad])[1]
                cuv[bad] = p["uv"][nn]
            cuv = cuv.reshape(-1, 3, 2)
            # 3つの角が別々のUV島に投影された面は、補間すると島の間の
            # 無関係な色を拾う(色付きの点ノイズの原因)。多数派の島の角に揃える
            span = (cuv.max(axis=1) - cuv.min(axis=1)).max(axis=1)
            island = span > 0.25
            if island.any():
                c = cuv[island]
                d01 = np.linalg.norm(c[:, 0] - c[:, 1], axis=1)
                d02 = np.linalg.norm(c[:, 0] - c[:, 2], axis=1)
                d12 = np.linalg.norm(c[:, 1] - c[:, 2], axis=1)
                k = np.argmin(np.stack([d01 + d02, d01 + d12, d02 + d12], axis=1), axis=1)
                cuv[island] = c[np.arange(len(c)), k][:, None, :]
            new_cuv[sel] = cuv
        if vert_pool is not None:
            # 削減後の頂点は最寄りの削減前頂点からウェイトを引き継ぐ
            nearest = cKDTree(wv).query(nv)[1]
            vert_pool = vert_pool[nearest]
        wv, wf, fpart, corner_uv = nv, nf, new_fpart, new_cuv
        print(f"  削減後  : {len(wf)}面 (一体で削減、材質境界の亀裂なし)")

    # ---- テクスチャアトラス (縦積み) ----
    tiles = []
    for p in parts:
        if p["image"] is not None and p["uv"] is not None:
            tiles.append(downsample(p["image"], 1.0, texsize))
        else:
            tiles.append(np.tile(p["color"], (2, 2, 1)))  # 単色タイル
    W = max(t.shape[1] for t in tiles)
    H = sum(t.shape[0] for t in tiles)
    atlas = np.ones((H, W, 3), dtype=np.float64)
    y0s = []
    y = 0
    for t in tiles:
        atlas[y:y + t.shape[0], :t.shape[1]] = t
        y0s.append(y)
        y += t.shape[0]
    print(f"  アトラス: {W}x{H}")

    # ---- コーナーUVをアトラス座標へ (出力規約: v=0が下, x=u*(W-1), 行=(1-v)*(H-1)) ----
    tw_arr = np.array([t.shape[1] for t in tiles], dtype=np.float64)
    th_arr = np.array([t.shape[0] for t in tiles], dtype=np.float64)
    y0_arr = np.array(y0s, dtype=np.float64)
    tw_f = tw_arr[fpart][:, None]
    th_f = th_arr[fpart][:, None]
    y0_f = y0_arr[fpart][:, None]

    def unwrap(c):
        """面内でUVが画像端をまたぐ場合、補間が画像全体を横切らないよう連続化する"""
        c = c - np.floor(c)
        wide = (c.max(axis=1) - c.min(axis=1)) > 0.5
        cw = c[wide]
        cw[cw < 0.5] += 1.0
        c[wide] = cw
        return np.clip(c, 0.0, 1.0)

    u = unwrap(corner_uv[:, :, 0].copy())
    v = unwrap(corner_uv[:, :, 1].copy())
    px = u * (tw_f - 1)
    py = (1.0 - v) * (th_f - 1)
    atlas_uv = np.stack([px / max(W - 1, 1), 1.0 - (y0_f + py) / max(H - 1, 1)], axis=2)

    # ---- 向き補正・正規化 ----
    if args.flip:
        wf = wf[:, ::-1]
        atlas_uv = atlas_uv[:, ::-1]
    if args.zup:
        rot = trimesh.transformations.rotation_matrix(-np.pi / 2.0, [1, 0, 0])[:3, :3]
        wv = wv @ rot.T

    lo = wv.min(axis=0)
    hi = wv.max(axis=0)
    norm_off = np.array([(lo[0] + hi[0]) / 2.0, lo[1], (lo[2] + hi[2]) / 2.0])
    norm_s = args.height / (hi[1] - lo[1]) if hi[1] - lo[1] > 1e-6 else 1.0
    wv = (wv - norm_off) * norm_s
    size = wv.max(axis=0) - wv.min(axis=0)
    print(f"  サイズ  : 幅{size[0]:.2f} x 高{size[1]:.2f} x 奥{size[2]:.2f} m")

    # ---- 出力 (コーナーUVは重複をまとめて vt 化) ----
    vt_map = {}
    vt_list = []
    fuv = np.zeros((len(wf), 3), dtype=np.int64)
    for i in range(len(wf)):
        for k in range(3):
            key = (round(float(atlas_uv[i, k, 0]), 5), round(float(atlas_uv[i, k, 1]), 5))
            j = vt_map.get(key)
            if j is None:
                j = len(vt_list)
                vt_map[key] = j
                vt_list.append(key)
            fuv[i, k] = j

    tex8 = np.clip(atlas * 255.0, 0, 255).astype(np.uint8)
    with open(out_path, "w", encoding="ascii") as fp:
        fp.write(f"# model2mdraw tex-mode ({len(wv)}v {len(wf)}f atlas {W}x{H})\n")
        fp.write(f"tex {W} {H}\n")
        for trow in tex8:
            fp.write("tx " + trow.tobytes().hex() + "\n")
        for vv in wv:
            fp.write(f"v {vv[0]:.4f} {vv[1]:.4f} {vv[2]:.4f}\n")
        for t in vt_list:
            fp.write(f"vt {t[0]:.5f} {t[1]:.5f}\n")
        for i, f in enumerate(wf):
            fp.write(f"f {f[0]+1}/{fuv[i,0]+1} {f[1]+1}/{fuv[i,1]+1} {f[2]+1}/{fuv[i,2]+1}\n")

    # ---- スキン(.skin)とモーション(.anim)の出力 ----
    if skin is not None and vert_pool is not None:
        bones = skin["bones"]
        # 親が必ず先に来るよう並べ替え(プラグインは先頭から順に行列を解決する)
        order = []
        placed = np.zeros(len(bones), dtype=bool)
        while len(order) < len(bones):
            progressed = False
            for bi, b in enumerate(bones):
                if placed[bi]:
                    continue
                par = b["parent"]
                if par < 0 or (par < len(bones) and placed[par]):
                    order.append(bi)
                    placed[bi] = True
                    progressed = True
            if not progressed:  # 循環(壊れたデータ)は残りを強制追加
                for bi in range(len(bones)):
                    if not placed[bi]:
                        order.append(bi)
                        placed[bi] = True
                break
        remap = np.zeros(len(bones), dtype=np.int64)
        for new, old in enumerate(order):
            remap[old] = new

        skin_path = os.path.splitext(out_path)[0] + ".skin"
        wb = skin["wbone"][vert_pool]
        wvv = skin["wval"][vert_pool]
        with open(skin_path, "w", encoding="ascii") as fp:
            fp.write(f"skin {len(bones)} {len(wv)}\n")
            for old in order:
                b = bones[old]
                par = remap[b["parent"]] if 0 <= b["parent"] < len(bones) else -1
                bp = (b["pos"] - norm_off) * norm_s
                fp.write(f"b {par} {bp[0]:.4f} {bp[1]:.4f} {bp[2]:.4f}\n")
            for i in range(len(wv)):
                row = []
                for j in range(4):
                    bi = int(wb[i, j])
                    w = float(wvv[i, j])
                    if w > 0.0001 and 0 <= bi < len(bones):
                        row.append(f"{remap[bi]} {w:.4f}")
                if not row:
                    row.append("0 1.0")
                fp.write("w " + " ".join(row) + "\n")
        print(f"出力: {skin_path} (ボーン{len(bones)})")

        if args.vmd:
            name2idx = {b["name"]: int(remap[bi]) for bi, b in enumerate(bones)}
            if args.vmd == "test":
                keys = make_test_motion(name2idx)
                anim_path = os.path.splitext(out_path)[0] + "_test.anim"
            else:
                keys = load_vmd(args.vmd, name2idx)
                anim_path = (os.path.splitext(out_path)[0] + "_"
                             + os.path.splitext(os.path.basename(args.vmd))[0] + ".anim")
            max_frame = 0
            total_keys = 0
            with open(anim_path, "w", encoding="ascii") as fp:
                lines_out = []
                for bi in sorted(keys):
                    for (frame, px, py, pz, qx, qy, qz, qw) in keys[bi]:
                        max_frame = max(max_frame, frame)
                        total_keys += 1
                        lines_out.append(
                            f"k {bi} {frame} {px * norm_s:.4f} {py * norm_s:.4f} {pz * norm_s:.4f} "
                            f"{qx:.5f} {qy:.5f} {qz:.5f} {qw:.5f}")
                fp.write(f"anim {max_frame} 30\n")
                fp.write("\n".join(lines_out) + "\n")
            print(f"出力: {anim_path} (キー{total_keys}個, {max_frame}フレーム = {max_frame / 30:.1f}秒)")
    return len(wf)


# ============================================================
# カラーモード (テクスチャなし: 従来の面色焼き込みパイプライン)
# ============================================================
def get_face_colors(mesh):
    try:
        vis = mesh.visual
        if hasattr(vis, "face_colors") and vis.face_colors is not None:
            fc = np.asarray(vis.face_colors, dtype=np.float64)
            if len(fc) == len(mesh.faces):
                fc = fc[:, :3] / 255.0
                if np.allclose(fc, fc[0]) and np.allclose(fc[0], 102.0 / 255.0, atol=0.02):
                    return None
                return fc
    except Exception as e:
        print(f"  警告: 色の抽出に失敗 ({e})。単色で出力します")
    return None


def resample_colors(orig_mesh, orig_colors, new_mesh):
    oc = np.asarray(orig_mesh.triangles_center, dtype=np.float64)
    nc = np.asarray(new_mesh.triangles_center, dtype=np.float64)
    try:
        from scipy.spatial import cKDTree
        return orig_colors[cKDTree(oc).query(nc)[1]]
    except ImportError:
        out = np.empty((len(nc), 3), dtype=np.float64)
        for s in range(0, len(nc), 512):
            block = nc[s:s + 512]
            d2 = ((oc[None, :, :] - block[:, None, :]) ** 2).sum(axis=2)
            out[s:s + 512] = orig_colors[d2.argmin(axis=1)]
        return out


def convert_plain(path, args, out_path):
    mesh = trimesh.load(path, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        sys.exit("エラー: 三角形メッシュとして読み込めませんでした")
    face_colors = None if args.no_color else get_face_colors(mesh)
    orig_for_color = mesh.copy() if face_colors is not None else None
    if face_colors is not None:
        print("  色情報  : あり (面ごとの代表色として焼き込み)")

    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()

    if args.zup:
        rot = trimesh.transformations.rotation_matrix(-np.pi / 2.0, [1, 0, 0])
        mesh.apply_transform(rot)
        if orig_for_color is not None:
            orig_for_color.apply_transform(rot)

    if args.faces is not None and len(mesh.faces) > args.faces:
        import fast_simplification
        verts, faces = fast_simplification.simplify(
            np.asarray(mesh.vertices, dtype=np.float64),
            np.asarray(mesh.faces, dtype=np.int64),
            target_reduction=1.0 - (args.faces / len(mesh.faces)))
        mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.remove_unreferenced_vertices()
        print(f"  削減後  : {len(mesh.vertices)}頂点 / {len(mesh.faces)}面")

    trimesh.repair.fix_normals(mesh)
    if face_colors is not None:
        face_colors = resample_colors(orig_for_color, face_colors, mesh)

    lo, hi = mesh.bounds
    size = hi - lo
    mesh.apply_translation([-(lo[0] + hi[0]) / 2.0, -lo[1], -(lo[2] + hi[2]) / 2.0])
    if size[1] > 1e-6:
        mesh.apply_scale(args.height / size[1])
    lo, hi = mesh.bounds
    size = hi - lo
    print(f"  サイズ  : 幅{size[0]:.2f} x 高{size[1]:.2f} x 奥{size[2]:.2f} m")

    with open(out_path, "w", encoding="ascii") as fp:
        fp.write(f"# model2mdraw color-mode ({len(mesh.vertices)}v {len(mesh.faces)}f)\n")
        for v in mesh.vertices:
            fp.write(f"v {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}\n")
        prev = None
        for i, f in enumerate(mesh.faces):
            if face_colors is not None:
                c = face_colors[i]
                cur = f"c {c[0]:.3f} {c[1]:.3f} {c[2]:.3f}"
                if cur != prev:
                    fp.write(cur + "\n")
                    prev = cur
            fp.write(f"f {f[0] + 1} {f[1] + 1} {f[2] + 1}\n")
    return len(mesh.faces)


# ============================================================
def main():
    ap = argparse.ArgumentParser(description="3Dモデルを ddraw 描画向け拡張OBJへ変換")
    ap.add_argument("input", help="入力モデル (pmx/obj/glb/gltf/stl/ply/3mf)")
    ap.add_argument("-f", "--faces", type=int, default=None,
                    help="目標三角形数。指定したときだけポリゴン削減する (既定: 削減なし)")
    ap.add_argument("-H", "--height", type=float, default=1.0,
                    help="出力の高さ(m) (既定1.0)")
    ap.add_argument("-o", "--output", default=None, help="出力OBJパス (既定: <入力名>_mdraw.obj)")
    ap.add_argument("--texsize", type=int, default=512,
                    help="埋め込みテクスチャの1枚あたり最大辺(px) (既定512)")
    ap.add_argument("--zup", action="store_true", help="Z-up素材をY-upへ回転補正")
    ap.add_argument("--flip", action="store_true", help="面の表裏が逆のとき巻き順を反転")
    ap.add_argument("--no-color", action="store_true", help="色を焼き込まない(単色)")
    ap.add_argument("--keep-hidden", action="store_true",
                    help="隠面除去とデカール押し出しをしない (面が欠けたとき用)")
    ap.add_argument("--vmd", default=None,
                    help="VMDモーションを.animに変換して併せて出力 (PMX入力時のみ)")
    args = ap.parse_args()

    print(f"読み込み: {args.input}")
    ext = os.path.splitext(args.input)[1].lower()
    out = args.output or os.path.splitext(args.input)[0] + "_mdraw.obj"

    skin = None
    if ext == ".pmx":
        subs, skin = load_pmx(args.input)
    else:
        subs = load_trimesh_submeshes(args.input)

    has_texture = any(s["image"] is not None and s["uv"] is not None for s in subs) and not args.no_color
    if has_texture:
        print("  モード  : テクスチャ埋め込み (顔などの描き込みも線分割で再現)")
        nfaces = convert_textured(subs, args, out, skin)
    else:
        print("  モード  : 面色焼き込み (テクスチャなし)")
        nfaces = convert_plain(args.input, args, out)

    if nfaces > 8000:
        print(f"  注意    : {nfaces}面はライン描画には多すぎます。-f 4000〜8000 を推奨")

    name = os.path.splitext(os.path.basename(out))[0]
    print(f"出力: {out}")
    print()
    print("次の手順:")
    print(f"  1. {out} をサーバーの oxide/data/MeshDraw/ にコピー")
    print(f"  2. ゲーム内で /mdraw {name} [scale] [回転speed]")


if __name__ == "__main__":
    main()
