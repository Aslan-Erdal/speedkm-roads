import gzip
import json
import os
import tempfile
import unittest

import build_roadtiles as b


class MaxspeedParity(unittest.TestCase):
    """Mêmes cas que test/unit/maxspeed_parser_test.dart : le script et l'app
    doivent lire les balises de la même façon."""

    def test_plain_and_units(self):
        self.assertEqual(b.parse_maxspeed("80"), 80)
        self.assertEqual(b.parse_maxspeed(" 50 "), 50)
        self.assertEqual(b.parse_maxspeed("90 km/h"), 90)
        self.assertEqual(b.parse_maxspeed("50 mph"), 80)
        self.assertEqual(b.parse_maxspeed("30mph"), 48)
        self.assertEqual(b.parse_maxspeed("70 mph"), 113)

    def test_french_symbolic(self):
        for raw, kmh in [("FR:urban", 50), ("FR:rural", 80), ("FR:trunk", 110),
                         ("FR:motorway", 130), ("FR:zone30", 30), ("FR:30", 30)]:
            self.assertEqual(b.parse_maxspeed(raw), kmh, raw)

    def test_unknown(self):
        for raw in ["walk", "none", "signals", "variable", "DE:urban", "50;70",
                    "90 @ (22:00-06:00)", None, "", "   ", "0", "999", "-30", "abc"]:
            self.assertIsNone(b.parse_maxspeed(raw), raw)

    def test_from_tags(self):
        self.assertEqual(b.maxspeed_from_tags({"maxspeed": "90"}), 90)
        self.assertEqual(b.maxspeed_from_tags({"maxspeed:type": "FR:urban"}), 50)
        self.assertEqual(b.maxspeed_from_tags({"zone:maxspeed": "FR:30"}), 30)
        self.assertEqual(b.maxspeed_from_tags({"maxspeed": "70", "maxspeed:type": "FR:rural"}), 70)
        self.assertEqual(b.maxspeed_from_tags({"maxspeed": "signals", "maxspeed:type": "FR:urban"}), 50)
        self.assertIsNone(b.maxspeed_from_tags({"maxspeed:type": "sign"}))
        self.assertIsNone(b.maxspeed_from_tags({"highway": "residential"}))

    def test_oneway(self):
        self.assertEqual(b.oneway_from_tags({"oneway": "yes"}), 1)
        self.assertEqual(b.oneway_from_tags({"oneway": "-1"}), -1)
        self.assertEqual(b.oneway_from_tags({"oneway": "no", "highway": "motorway"}), 0)
        self.assertEqual(b.oneway_from_tags({"highway": "motorway"}), 1)
        self.assertEqual(b.oneway_from_tags({"junction": "roundabout"}), 1)
        self.assertEqual(b.oneway_from_tags({"highway": "residential"}), 0)


class Tiling(unittest.TestCase):
    def test_tile_index_matches_app(self):
        # SpeedLimitRepositoryImpl.tileKey : floor(lat / 0.01)
        self.assertEqual(b.tile_index(48.8566), 4885)
        self.assertEqual(b.tile_index(2.3522), 235)
        self.assertEqual(b.tile_index(-1.5), -150)

    def test_block_of_uses_floor_division(self):
        self.assertEqual(b.block_of((4885, 235)), (488, 23))
        self.assertEqual(b.block_of((-1, -1)), (-1, -1))

    def test_short_way_in_one_tile(self):
        parts = b.split_way_by_tile([(48.8560, 2.3520), (48.8570, 2.3525)])
        self.assertEqual(set(parts), {(4885, 235)})

    def test_way_near_border_is_also_in_neighbour(self):
        # à 20 m du bord nord de la zone 4885 : visible aussi depuis la 4886
        parts = b.split_way_by_tile([(48.8598, 2.3520), (48.8599, 2.3525)])
        self.assertEqual(set(parts), {(4885, 235), (4886, 235)})

    def test_long_way_is_clipped_not_duplicated(self):
        # 100 km vers l'est : chaque zone ne garde que son tronçon
        points = [(48.855, 2.0 + i * 0.005) for i in range(0, 201)]
        parts = b.split_way_by_tile(points)
        stored = sum(len(p) for runs in parts.values() for p in runs)
        self.assertLess(stored, len(points) * 3)  # ~1× (+ marge), pas 200×
        self.assertGreater(len(parts), 50)

    def test_encode_points_deltas(self):
        self.assertEqual(
            b.encode_points([(48.85660, 2.35220), (48.85670, 2.35240)]),
            [4885660, 235220, 10, 20],
        )

    def test_unknown_class_or_degenerate_way_skipped(self):
        self.assertIsNone(b.way_record(1, {"highway": "footway"}, [(0, 0), (1, 1)]))
        self.assertIsNone(b.way_record(1, {"highway": "primary"}, [(0, 0)]))
        record = b.way_record(1, {"highway": "primary", "maxspeed": "80"}, [(0, 0), (1, 1)])
        self.assertEqual((record["hw"], record["ms"], record["ow"]), (2, 80, 0))


class Output(unittest.TestCase):
    def test_write_blocks_layout(self):
        blocks = {(488, 23): {(4885, 235): [[7, 2, 80, 0, [4885660, 235220, 10, 20]]]}}
        with tempfile.TemporaryDirectory() as out:
            b.write_blocks(blocks, out, "2026-10-10")
            path = os.path.join(out, "v1", "488", "23.json.gz")
            data = json.loads(gzip.decompress(open(path, "rb").read()))
            self.assertEqual(data["v"], 1)
            self.assertEqual(data["built"], "2026-10-10")
            self.assertEqual(data["hw"][2], "primary")
            self.assertEqual(data["t"]["4885:235"][0][0], 7)
            index = json.load(open(os.path.join(out, "index.json")))
            self.assertEqual(index["blocks"], 1)
            self.assertTrue(os.path.exists(os.path.join(out, ".nojekyll")))

    def test_output_is_deterministic(self):
        blocks = {(1, 2): {(10, 20): [[1, 0, 0, 0, [1, 2, 3, 4]]]}}
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as c:
            b.write_blocks(blocks, a, "2026-10-10")
            b.write_blocks(blocks, c, "2026-10-10")
            self.assertEqual(
                open(os.path.join(a, "v1", "1", "2.json.gz"), "rb").read(),
                open(os.path.join(c, "v1", "1", "2.json.gz"), "rb").read(),
            )


if __name__ == "__main__":
    unittest.main()
