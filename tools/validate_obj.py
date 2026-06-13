"""出力OBJをプラグイン(LoadObj)と同じロジックで読み、整合性を検証する"""
import sys
import numpy as np

path = sys.argv[1]
verts = []
faces = []
face_colors = None
current = (1.0, 1.0, 1.0)
for raw in open(path, encoding="ascii"):
    line = raw.strip()
    if line.startswith("c "):
        t = line.split()
        if face_colors is None:
            face_colors = [(1.0, 1.0, 1.0)] * len(faces)
        current = (float(t[1]), float(t[2]), float(t[3]))
    elif line.startswith("v "):
        t = line.split()
        verts.append((-float(t[1]), float(t[2]), float(t[3])))  # プラグインはX反転
    elif line.startswith("f "):
        t = line.split()
        idx = [int(p.split("/")[0]) - 1 for p in t[1:]]
        idx.reverse()  # プラグインは巻き順反転
        faces.append(idx)
        if face_colors is not None:
            face_colors.append(current)

verts = np.array(verts)
print(f"{path}: {len(verts)}頂点 {len(faces)}面 色={'あり' if face_colors else 'なし'}")
assert all(len(f) == 3 for f in faces), "三角形以外の面がある"
assert all(0 <= i < len(verts) for f in faces for i in f), "頂点インデックス範囲外"
if face_colors:
    assert len(face_colors) == len(faces), "色と面の数が不一致"
    uniq = len(set(face_colors))
    print(f"  色の種類: {uniq}")

# 裏面カリング整合性: 巻き順が一貫し体積が正なら法線は外向き
import trimesh
m = trimesh.Trimesh(vertices=verts, faces=np.array(faces), process=False)
consistent = m.is_winding_consistent
vol = m.volume if consistent else 0.0
print(f"  巻き順一貫: {consistent}, 体積: {vol:.4f}")
if consistent and vol > 0:
    print("  OK: 裏面カリングは正常に働く見込み")
else:
    print("  警告: ゲーム内で面が欠けたら /mdraw cull を試す")
