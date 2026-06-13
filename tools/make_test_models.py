"""変換ツールのテスト用モデル生成"""
import numpy as np
import trimesh

# テスト1: 頂点色つき高ポリ球 (下=青 → 上=赤 のグラデーション)
m = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
t = (m.vertices[:, 1] - m.vertices[:, 1].min()) / np.ptp(m.vertices[:, 1])
colors = np.zeros((len(m.vertices), 4), dtype=np.uint8)
colors[:, 0] = (255 * t).astype(np.uint8)
colors[:, 2] = (255 * (1 - t)).astype(np.uint8)
colors[:, 3] = 255
m.visual.vertex_colors = colors
m.export("test_sphere.glb")
print("test_sphere.glb:", len(m.faces), "faces")

# テスト2: 色なしSTL
torus = trimesh.creation.torus(major_radius=1.0, minor_radius=0.35)
torus.export("test_torus.stl")
print("test_torus.stl:", len(torus.faces), "faces")
