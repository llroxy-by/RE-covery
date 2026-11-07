"""生成固定测试样例图（合成，不含任何真实拍摄画面，天然脱敏）。

    python -m vision.tests.make_fixtures

产物写入 vision/tests/fixtures/：s0.jpg、s1_moved.jpg、s1_occluded.jpg、s1_extra.jpg
以及 expected.json（每个样例的预期差异结论）。真实相机照片请另存，注意脱敏。
"""

from __future__ import annotations

import json
from pathlib import Path

from vision.sources import MockSource

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config" / "desktop.json")
                    .read_text(encoding="utf-8"))

CASES = {
    "s0.jpg": ("ok", "baseline"),
    "s1_ok.jpg": ("ok", "unchanged"),
    "s1_moved.jpg": ("moved", "misplaced"),
    "s1_swapped.jpg": ("swapped", "misplaced"),
    "s1_occluded.jpg": ("occluded", "uncertain"),
    "s1_extra.jpg": ("extra", "uncertain"),
    "s1_dim.jpg": ("dim", "uncertain"),
}


def main() -> None:
    import cv2

    FIXTURES.mkdir(parents=True, exist_ok=True)
    expected = {}
    for name, (scenario, status) in CASES.items():
        src = MockSource(CONFIG["zones"], CONFIG["resolution"], scenario)
        frame = src.read()
        path = FIXTURES / name
        if not cv2.imwrite(str(path), frame.image):
            raise RuntimeError(f"写入失败：{path}")
        expected[name] = {"scenario": scenario, "expected_status_vs_s0": status}
        print(f"wrote {path.name}  scenario={scenario}")
    (FIXTURES / "expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {FIXTURES / 'expected.json'}")


if __name__ == "__main__":
    main()
