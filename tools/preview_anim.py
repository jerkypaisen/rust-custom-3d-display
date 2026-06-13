"""プラグインと同じロジックでスキニングを再現し、指定フレームの姿勢を描画する検証ツール。
使い方: python preview_anim.py miku8k.obj miku8k_test.anim 60 out.png
"""
import sys
import numpy as np
import preview_obj as P


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz])


def qrot(q, v):
    u = q[:3]
    w = q[3]
    return 2 * np.dot(u, v) * u + (w * w - np.dot(u, u)) * v + 2 * w * np.cross(u, v)


def slerp(a, b, t):
    d = np.dot(a, b)
    if d < 0:
        b = -b
        d = -d
    if d > 0.9995:
        r = a + t * (b - a)
        return r / np.linalg.norm(r)
    th = np.arccos(np.clip(d, -1, 1))
    return (np.sin((1 - t) * th) * a + np.sin(t * th) * b) / np.sin(th)


def load_skin(path):
    lines = open(path).read().split("\n")
    nb, nv = int(lines[0].split()[1]), int(lines[0].split()[2])
    parent = np.zeros(nb, int)
    rest = np.zeros((nb, 3))
    li = 1
    for b in range(nb):
        t = lines[li].split(); li += 1
        parent[b] = int(t[1])
        rest[b] = (-float(t[2]), float(t[3]), float(t[4]))  # プラグインと同じX反転
    weights = []
    for v in range(nv):
        t = lines[li].split(); li += 1
        n = (len(t) - 1) // 2
        weights.append([(int(t[1 + j * 2]), float(t[2 + j * 2])) for j in range(n)])
    return parent, rest, weights


def load_anim(path, nb):
    frames = [[] for _ in range(nb)]
    pos = [[] for _ in range(nb)]
    rot = [[] for _ in range(nb)]
    maxf = 1
    for line in open(path):
        t = line.split()
        if not t:
            continue
        if t[0] == "anim":
            maxf = int(t[1])
        elif t[0] == "k":
            b = int(t[1])
            frames[b].append(int(t[2]))
            pos[b].append(np.array([-float(t[3]), float(t[4]), float(t[5])]))
            rot[b].append(np.array([float(t[6]), -float(t[7]), -float(t[8]), float(t[9])]))
    return frames, pos, rot, maxf


def sample(frames, pos, rot, b, f):
    fs = frames[b]
    if not fs:
        return np.zeros(3), np.array([0, 0, 0, 1.0])
    if f <= fs[0]:
        return pos[b][0], rot[b][0]
    if f >= fs[-1]:
        return pos[b][-1], rot[b][-1]
    import bisect
    hi = bisect.bisect_right(fs, f)
    lo = hi - 1
    u = (f - fs[lo]) / max(fs[hi] - fs[lo], 1)
    return pos[b][lo] + (pos[b][hi] - pos[b][lo]) * u, slerp(rot[b][lo], rot[b][hi], u)


def main():
    obj, anim, frame, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    verts, uvs, faces, face_uvs, fc, tex, tw, th = P.load_ext_obj(obj)
    parent, rest, weights = load_skin(obj.rsplit(".", 1)[0] + ".skin")
    frames, pos, rot, maxf = load_anim(anim, len(parent))
    print(f"{len(verts)}頂点 {len(parent)}ボーン frame={frame}/{maxf}")

    nb = len(parent)
    gR = [None] * nb
    gP = [None] * nb
    for i in range(nb):
        t, r = sample(frames, pos, rot, i, frame)
        p = parent[i]
        if p < 0:
            gR[i] = r
            gP[i] = rest[i] + t
        else:
            gR[i] = qmul(gR[p], r)
            gP[i] = gP[p] + qrot(gR[p], rest[i] - rest[p] + t)

    skinned = np.empty_like(verts)
    moved = 0
    for v in range(len(verts)):
        acc = np.zeros(3)
        for b, w in weights[v]:
            acc += w * (qrot(gR[b], verts[v] - rest[b]) + gP[b])
        skinned[v] = acc
        if np.linalg.norm(acc - verts[v]) > 0.02:
            moved += 1
    print(f"動いた頂点: {moved}/{len(verts)}")

    img = P.render_raster(skinned, uvs, faces, face_uvs, fc, tex, tw, th,
                          *_canvas(skinned), nocull=False)
    from PIL import Image
    Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)).save(out)
    print(f"保存: {out}")


def _canvas(verts, px=700):
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    scale = px / (hi[1] - lo[1])
    W = int((hi[0] - lo[0]) * scale) + 20
    H = px + 20
    return W, H, lo, hi, scale


if __name__ == "__main__":
    main()
