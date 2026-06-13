"""拡張OBJ(変換結果)をレンダリングしてPNG保存する確認ツール。

既定: ピクセル単位の正確なラスタライズ (変換データそのものの品質を見る)
--lines: プラグインのスキャンライン線描画をシミュレート (ゲーム内の見え方の参考)

使い方:
  python preview_obj.py miku.obj [出力.png] [--yaw 度] [--px 高さピクセル] [--lines]
"""
import argparse
import numpy as np
from PIL import Image


def load_ext_obj(path):
    """プラグインのLoadObjと同じ変換(X反転・巻き順反転)で読む"""
    verts, uvs, faces, face_uvs, face_colors = [], [], [], [], []
    tex = None
    tw = th = 0
    rows = []
    cur = (1.0, 1.0, 1.0)
    has_c = False
    for raw in open(path, encoding="ascii"):
        line = raw.strip()
        if line.startswith("tex "):
            t = line.split()
            tw, th = int(t[1]), int(t[2])
        elif line.startswith("tx "):
            rows.append(np.frombuffer(bytes.fromhex(line[3:]), dtype=np.uint8).reshape(-1, 3))
        elif line.startswith("vt "):
            t = line.split()
            uvs.append((float(t[1]), float(t[2])))
        elif line.startswith("c "):
            t = line.split()
            cur = (float(t[1]), float(t[2]), float(t[3]))
            has_c = True
        elif line.startswith("v "):
            t = line.split()
            verts.append((-float(t[1]), float(t[2]), float(t[3])))
        elif line.startswith("f "):
            t = line.split()[1:]
            vi = [int(p.split("/")[0]) - 1 for p in t]
            vi.reverse()
            faces.append(vi)
            if all("/" in p and p.split("/")[1] for p in t):
                ti = [int(p.split("/")[1]) - 1 for p in t]
                ti.reverse()
                face_uvs.append(ti)
            else:
                face_uvs.append(None)
            face_colors.append(cur)
    if rows:
        tex = np.vstack([r[None, :tw] for r in rows]).astype(np.float64) / 255.0
    return (np.array(verts), np.array(uvs) if uvs else None, faces, face_uvs,
            face_colors if has_c else None, tex, tw, th)


def sample_tex(tex, tw, th, u, v):
    x = int(np.clip(round(u * (tw - 1)), 0, tw - 1))
    y = int(np.clip(round((1.0 - v) * (th - 1)), 0, th - 1))
    return tex[y, x]


def render_raster(verts, uvs, faces, face_uvs, face_colors, tex, tw, th, W, H, lo, hi, scale, nocull):
    """ピクセル単位の正確な描画 (変換データの品質確認用)"""
    img = np.full((H, W, 3), 0.15)
    zbuf = np.full((H, W), -1e9)
    light = np.array([0.4, 0.8, 0.3])
    light /= np.linalg.norm(light)

    for fi, f in enumerate(faces):
        fuv = face_uvs[fi]
        for ti in range(2, len(f)):
            tri = (f[0], f[ti - 1], f[ti])
            A, B, C = verts[tri[0]], verts[tri[1]], verts[tri[2]]
            n = np.cross(B - A, C - A)
            nl = np.linalg.norm(n)
            if nl < 1e-12:
                continue
            n /= nl
            if n[2] < 0 and not nocull:
                continue
            bright = 0.30 + 0.70 * abs(n @ light)

            pts = np.array([[(p[0] - lo[0]) * scale + 10, (hi[1] - p[1]) * scale + 10]
                            for p in (A, B, C)])
            zs = np.array([A[2], B[2], C[2]])
            if fuv is not None and tex is not None:
                cuv = np.array([uvs[fuv[0]], uvs[fuv[ti - 1]], uvs[fuv[ti]]])
            else:
                cuv = None
            d = (pts[1, 1] - pts[2, 1]) * (pts[0, 0] - pts[2, 0]) + \
                (pts[2, 0] - pts[1, 0]) * (pts[0, 1] - pts[2, 1])
            if abs(d) < 1e-9:
                continue
            minx = max(int(pts[:, 0].min()), 0)
            maxx = min(int(np.ceil(pts[:, 0].max())), W - 1)
            miny = max(int(pts[:, 1].min()), 0)
            maxy = min(int(np.ceil(pts[:, 1].max())), H - 1)
            for y in range(miny, maxy + 1):
                for x in range(minx, maxx + 1):
                    w1 = ((pts[1, 1] - pts[2, 1]) * (x + .5 - pts[2, 0]) +
                          (pts[2, 0] - pts[1, 0]) * (y + .5 - pts[2, 1])) / d
                    w2 = ((pts[2, 1] - pts[0, 1]) * (x + .5 - pts[2, 0]) +
                          (pts[0, 0] - pts[2, 0]) * (y + .5 - pts[2, 1])) / d
                    w3 = 1 - w1 - w2
                    if w1 < -1e-3 or w2 < -1e-3 or w3 < -1e-3:
                        continue
                    z = w1 * zs[0] + w2 * zs[1] + w3 * zs[2]
                    if z <= zbuf[y, x]:
                        continue
                    zbuf[y, x] = z
                    if cuv is not None:
                        u = w1 * cuv[0, 0] + w2 * cuv[1, 0] + w3 * cuv[2, 0]
                        v = w1 * cuv[0, 1] + w2 * cuv[1, 1] + w3 * cuv[2, 1]
                        img[y, x] = sample_tex(tex, tw, th, u, v)  # unlit
                    else:
                        col = np.array(face_colors[fi]) if face_colors else np.array([1.0, 1, 1])
                        img[y, x] = np.clip(col * bright, 0, 1)
    return img


def render_lines(verts, uvs, faces, face_uvs, face_colors, tex, tw, th, W, H, lo, hi, scale, nocull):
    """プラグインのスキャンライン+線分割採色をシミュレート"""
    img = np.full((H, W, 3), 0.15)
    zbuf = np.full((H, W), -1e9)
    light = np.array([0.4, 0.8, 0.3])
    light /= np.linalg.norm(light)

    def to_px(p):
        return ((p[0] - lo[0]) * scale + 10, (hi[1] - p[1]) * scale + 10)

    for fi, f in enumerate(faces):
        fuv = face_uvs[fi]
        a3, b3, c3 = verts[f[0]], verts[f[1]], verts[f[2]]
        n = np.cross(b3 - a3, c3 - a3)
        nl = np.linalg.norm(n)
        if nl < 1e-12:
            continue
        n /= nl
        if n[2] < 0 and not nocull:
            continue
        bright = 0.30 + 0.70 * abs(n @ light)

        for ti in range(2, len(f)):
            tri = (f[0], f[ti - 1], f[ti])
            A, B, C = verts[tri[0]], verts[tri[1]], verts[tri[2]]
            d = [np.sum((A - B) ** 2), np.sum((B - C) ** 2), np.sum((C - A) ** 2)]
            k = int(np.argmax(d))
            if k == 0:
                P, Q, R = A, B, C
                order = (0, 1, 2)
            elif k == 1:
                P, Q, R = B, C, A
                order = (1, 2, 0)
            else:
                P, Q, R = C, A, B
                order = (2, 0, 1)
            if fuv is not None and tex is not None:
                corners = (fuv[0], fuv[ti - 1], fuv[ti])
                uvP = np.array(uvs[corners[order[0]]])
                uvQ = np.array(uvs[corners[order[1]]])
                uvR = np.array(uvs[corners[order[2]]])
            else:
                uvP = uvQ = uvR = None

            pq = (Q - P) / max(np.linalg.norm(Q - P), 1e-9)
            foot = P + pq * ((R - P) @ pq)
            sweep_px = np.linalg.norm((foot - R)[:2]) * scale
            nlines = max(1, int(np.ceil(sweep_px / 0.9)))
            for li in range(1, nlines + 1):
                t = (li - 0.5) / nlines
                s = R + (P - R) * t
                e = R + (Q - R) * t
                ln_px = np.linalg.norm((e - s)[:2]) * scale
                steps = max(1, int(ln_px))
                for st in range(steps + 1):
                    u_ = st / max(steps, 1)
                    p3 = s + (e - s) * u_
                    x, y = to_px(p3)
                    xi, yi = int(x), int(y)
                    if not (0 <= yi < H and 0 <= xi < W) or p3[2] <= zbuf[yi, xi]:
                        continue
                    zbuf[yi, xi] = p3[2]
                    if uvP is not None:
                        uvS = uvR + (uvP - uvR) * t
                        uvE = uvR + (uvQ - uvR) * t
                        uv = uvS + (uvE - uvS) * u_
                        img[yi, xi] = sample_tex(tex, tw, th, uv[0], uv[1])  # unlit
                    else:
                        col = np.array(face_colors[fi]) if face_colors else np.array([1.0, 1, 1])
                        img[yi, xi] = np.clip(col * bright, 0, 1)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default=None)
    ap.add_argument("--yaw", type=float, default=0.0, help="Y軸回転(度)")
    ap.add_argument("--px", type=int, default=700, help="モデル高さの描画ピクセル数")
    ap.add_argument("--lines", action="store_true", help="ゲーム内の線描画をシミュレート")
    ap.add_argument("--nocull", action="store_true", help="裏面カリングを無効化")
    args = ap.parse_args()
    out = args.output or args.input.rsplit(".", 1)[0] + "_preview.png"

    verts, uvs, faces, face_uvs, face_colors, tex, tw, th = load_ext_obj(args.input)
    print(f"{len(verts)}頂点 {len(faces)}面 tex={'%dx%d' % (tw, th) if tex is not None else 'なし'}")

    yaw = np.radians(args.yaw)
    rot = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    verts = verts @ rot.T

    lo, hi = verts.min(axis=0), verts.max(axis=0)
    scale = args.px / (hi[1] - lo[1])
    W = int((hi[0] - lo[0]) * scale) + 20
    H = args.px + 20

    fn = render_lines if args.lines else render_raster
    img = fn(verts, uvs, faces, face_uvs, face_colors, tex, tw, th, W, H, lo, hi, scale, args.nocull)
    Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)).save(out)
    print(f"保存: {out} ({'線描画シミュレート' if args.lines else 'ピクセル正確'})")


if __name__ == "__main__":
    main()
