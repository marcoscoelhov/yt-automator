"""Smoke test rápido do motor de layers.

Uso:
  python3 backend/smoke_layers.py

Gera alguns PNGs em backend/temp/ para inspeção visual.
"""

import os
import sys
import time

# garante import "backend.*" mesmo rodando via: python3 backend/smoke_layers.py
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from backend.main import Scene, TEMP_DIR, _render_layer_scene_to_png


def main():
    os.makedirs(TEMP_DIR, exist_ok=True)

    scenes = [
        Scene(id=1, template="avatar_center", avatar_pose="neutral_arms_crossed", props=["moneybag", "chart_up"]),
        Scene(id=2, template="avatar_left_prop_right", avatar_pose="pointing", props=["debt_pile", "warning_sign"]),
        Scene(id=3, template="avatar_right_prop_left", avatar_pose="explaining_hand_up", props=["piggy_bank", "calendar"]),
        Scene(id=4, template="icons_with_red_x", avatar_pose="smiling", props=["coin_stack", "calendar", "red_x"]),
        Scene(id=5, template="icons_with_red_x", avatar_pose="surprised", props=["red_x"]),
        Scene(id=6, template="metaphor_single_prop", avatar_pose="thinking_hand_chin", props=["house"]),
    ]

    out_paths = []
    ts = int(time.time() * 1000)
    for sc in scenes:
        out_path = os.path.join(TEMP_DIR, f"smoke_layers_{ts}_scene_{sc.id}.png")
        _render_layer_scene_to_png(sc, out_path)
        out_paths.append(out_path)

    print("OK. Arquivos gerados:")
    for p in out_paths:
        print(" -", p)


if __name__ == "__main__":
    main()
