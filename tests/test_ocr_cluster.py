import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_cluster import cluster_detections, iou_box, merge_box


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


if __name__ == "__main__":
    unittest.main()
