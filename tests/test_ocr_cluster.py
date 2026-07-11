import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_cluster import (
    cluster_detections,
    filter_bottom_band,
    iou_box,
    merge_box,
)


class TestOcrCluster(unittest.TestCase):
    def test_iou_identical(self):
        b = [0.1, 0.8, 0.8, 0.1]
        self.assertAlmostEqual(iou_box(b, b), 1.0, places=6)

    def test_iou_disjoint(self):
        a = [0.0, 0.0, 0.2, 0.2]
        b = [0.5, 0.5, 0.2, 0.2]
        self.assertEqual(iou_box(a, b), 0.0)

    def test_merge_box_union(self):
        a = [0.1, 0.1, 0.2, 0.2]
        b = [0.2, 0.2, 0.2, 0.2]
        m = merge_box(a, b)
        self.assertAlmostEqual(m[0], 0.1)
        self.assertAlmostEqual(m[1], 0.1)
        self.assertAlmostEqual(m[2], 0.3)
        self.assertAlmostEqual(m[3], 0.3)

    def test_cluster_merges_nearby_same_region(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
            {"t": 0.5, "box": [0.12, 0.81, 0.78, 0.1], "score": 0.8},
            {"t": 1.0, "box": [0.11, 0.80, 0.79, 0.1], "score": 0.85},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 1.0)
        self.assertGreater(segs[0]["score"], 0.0)
        x, y, w, h = segs[0]["box"]
        self.assertGreaterEqual(x, 0.0)
        self.assertLessEqual(x + w, 1.0 + 1e-6)

    def test_cluster_splits_on_gap(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
            {"t": 5.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["end"], 0.0)
        self.assertEqual(segs[1]["start"], 5.0)

    def test_cluster_splits_on_low_iou(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.3, 0.1], "score": 0.9},
            {"t": 0.5, "box": [0.6, 0.1, 0.3, 0.1], "score": 0.9},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 2)

    def test_cluster_empty(self):
        self.assertEqual(cluster_detections([], iou_threshold=0.5, max_gap_sec=0.75), [])

    def test_multi_track_same_frame_two_regions(self):
        # 同帧两个远距离框 → 两轨并行;后续各轨续上
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.3, 0.1], "score": 0.9},  # 底栏
            {"t": 0.0, "box": [0.1, 0.05, 0.3, 0.1], "score": 0.8},  # 顶栏
            {"t": 0.5, "box": [0.12, 0.81, 0.28, 0.1], "score": 0.9},
            {"t": 0.5, "box": [0.11, 0.06, 0.28, 0.1], "score": 0.8},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 2)
        # 两段都跨 0→0.5
        for s in segs:
            self.assertEqual(s["start"], 0.0)
            self.assertEqual(s["end"], 0.5)

    def test_point_segment_extension(self):
        dets = [
            {"t": 1.0, "box": [0.2, 0.85, 0.5, 0.1], "score": 0.95},
        ]
        segs = cluster_detections(
            dets, iou_threshold=0.5, max_gap_sec=0.75, sample_interval=0.5
        )
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 1.0)
        self.assertAlmostEqual(segs[0]["end"], 1.5)  # 点段 + interval
        # 有跨度的段也会在尾部补一个 interval
        dets2 = [
            {"t": 0.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
            {"t": 0.5, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
        ]
        segs2 = cluster_detections(
            dets2, iou_threshold=0.5, max_gap_sec=0.75, sample_interval=0.5
        )
        self.assertEqual(len(segs2), 1)
        self.assertAlmostEqual(segs2[0]["end"], 1.0)  # 0.5 + 0.5

    def test_filter_bottom_band(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.3, 0.1], "score": 0.9},  # keep
            {"t": 0.0, "box": [0.1, 0.05, 0.3, 0.1], "score": 0.8},  # drop
            {"t": 0.5, "box": [0.0, 0.0, 1.0, 0.6], "score": 0.3},  # y=0 drop
            {"t": 0.5, "box": [0.2, 0.72, 0.4, 0.1], "score": 0.7},  # keep
        ]
        out = filter_bottom_band(dets, min_y_ratio=0.6)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(d["box"][1] >= 0.6 for d in out))
        # min_y_ratio<=0 → no filter
        self.assertEqual(len(filter_bottom_band(dets, min_y_ratio=0)), 4)


if __name__ == "__main__":
    unittest.main()
