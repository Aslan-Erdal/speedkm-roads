#!/usr/bin/env python3
"""Génère les données routières de SpeedKM (limitations de vitesse) à partir d'un
extrait OpenStreetMap (.osm.pbf), au format lu par l'application.

Sortie : un fichier par bloc de 0,1° (≈ 11 × 7 km), `v1/<bx>/<by>.json.gz`, à
publier tel quel sur un hébergement statique (GitHub Pages, Cloudflare R2…).
Chaque bloc contient ses zones de 0,01° (≈ 1,1 × 0,7 km), identiques à celles
que l'application demandait à Overpass : mêmes routes, mêmes règles de lecture
de `maxspeed` (voir `parse_maxspeed`, qui reprend `maxspeed_parser.dart`).

Usage : build_roadtiles.py --out site extrait1.osm.pbf [extrait2.osm.pbf ...]
Données © contributeurs OpenStreetMap, licence ODbL.
"""
import argparse
import gzip
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date

FORMAT_VERSION = 1
TILE_DEGREES = 0.01  # = SpeedLimitRepositoryImpl.tileDegrees
TILES_PER_BLOCK = 10
MARGIN_DEGREES = 0.0005  # voies voisines de la zone, comme la marge de l'app

HIGHWAYS = [
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "motorway_link", "trunk_link",
    "primary_link", "secondary_link", "tertiary_link",
]
HIGHWAY_INDEX = {name: i for i, name in enumerate(HIGHWAYS)}

_FR_SYMBOLIC = {
    "fr:urban": 50, "fr:rural": 80, "fr:trunk": 110, "fr:motorway": 130,
    "fr:zone30": 30,
}


def _validated(kmh):
    return kmh if 0 < kmh <= 200 else None


def parse_maxspeed(raw):
    """Miroir de `parseMaxspeed` (lib/features/speed_limit/domain/maxspeed_parser.dart)."""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or ";" in value or "@" in value:
        return None
    m = re.fullmatch(r"(\d{1,3})", value)
    if m:
        return _validated(int(m.group(1)))
    m = re.fullmatch(r"(\d{1,3})\s*km/h", value)
    if m:
        return _validated(int(m.group(1)))
    m = re.fullmatch(r"(\d{1,3})\s*mph", value)
    if m:
        return _validated(int(math.floor(int(m.group(1)) * 1.609344 + 0.5)))
    if value in _FR_SYMBOLIC:
        return _FR_SYMBOLIC[value]
    m = re.fullmatch(r"fr:(\d{1,3})", value)
    if m:
        return _validated(int(m.group(1)))
    return None


def maxspeed_from_tags(tags):
    """Miroir de `maxspeedFromTags` : maxspeed, sinon maxspeed:type, sinon zone:maxspeed."""
    for key in ("maxspeed", "maxspeed:type", "zone:maxspeed"):
        value = parse_maxspeed(tags.get(key))
        if value is not None:
            return value
    return None


def oneway_from_tags(tags):
    """Miroir de `onewayFromTags` : 1 sens du tracé, -1 inverse, 0 double sens."""
    oneway = tags.get("oneway")
    if oneway == "-1":
        return -1
    if oneway in ("yes", "1", "true"):
        return 1
    if oneway == "no":
        return 0
    if tags.get("junction") == "roundabout":
        return 1
    if tags.get("highway") == "motorway":
        return 1
    return 0


def tile_index(degrees):
    """Indice de zone d'une coordonnée (même calcul que `tileKey` de l'app)."""
    return math.floor(degrees / TILE_DEGREES)


def segment_tiles(a, b):
    """Zones (ix, iy) dont la zone élargie de la marge touche le segment a→b."""
    lat_lo, lat_hi = min(a[0], b[0]) - MARGIN_DEGREES, max(a[0], b[0]) + MARGIN_DEGREES
    lon_lo, lon_hi = min(a[1], b[1]) - MARGIN_DEGREES, max(a[1], b[1]) + MARGIN_DEGREES
    for ix in range(tile_index(lat_lo), tile_index(lat_hi) + 1):
        for iy in range(tile_index(lon_lo), tile_index(lon_hi) + 1):
            yield ix, iy


def split_way_by_tile(points):
    """{(ix, iy): [sous-tracés]} : pour chaque zone, les tronçons du tracé qui la
    touchent (une voie longue n'est donc jamais dupliquée en entier)."""
    segments = defaultdict(list)
    for i in range(len(points) - 1):
        for tile in segment_tiles(points[i], points[i + 1]):
            segments[tile].append(i)
    result = {}
    for tile, indexes in segments.items():
        runs, start, prev = [], indexes[0], indexes[0]
        for i in indexes[1:]:
            if i != prev + 1:
                runs.append((start, prev))
                start = i
            prev = i
        runs.append((start, prev))
        result[tile] = [points[a : b + 2] for a, b in runs]
    return result


def encode_points(points):
    """[lat0, lon0, dlat1, dlon1, …] en 1e-5° (≈ 1 m) : écarts courts, JSON compact."""
    flat, prev = [], (0, 0)
    for lat, lon in points:
        cur = (round(lat * 1e5), round(lon * 1e5))
        flat.extend((cur[0] - prev[0], cur[1] - prev[1]))
        prev = cur
    return flat


def way_record(way_id, tags, points):
    """Enregistrement compact d'une voie, ou None si elle n'est pas utile."""
    highway = tags.get("highway")
    if highway not in HIGHWAY_INDEX or len(points) < 2:
        return None
    return {
        "id": way_id,
        "hw": HIGHWAY_INDEX[highway],
        "ms": maxspeed_from_tags(tags) or 0,
        "ow": oneway_from_tags(tags),
        "points": points,
    }


def block_of(tile):
    return tile[0] // TILES_PER_BLOCK, tile[1] // TILES_PER_BLOCK


def read_ways(pbf_path, node_cache=None):
    """Itère (id, tags, [(lat, lon)…]) des routes carrossables d'un extrait."""
    import osmium

    # Les nœuds sont lus (pour leurs coordonnées) avant d'être écartés par le filtre.
    processor = (
        osmium.FileProcessor(pbf_path, osmium.osm.NODE | osmium.osm.WAY)
        .with_locations(f"sparse_file_array,{node_cache}" if node_cache else "flex_mem")
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for way in processor:
        if not way.is_way():
            continue  # nœuds portant highway=* (feux, passages piétons…)
        highway = way.tags.get("highway")
        if highway not in HIGHWAY_INDEX:
            continue
        try:
            points = [(n.lat, n.lon) for n in way.nodes if n.location.valid()]
        except osmium.InvalidLocationError:
            continue
        yield way.id, {k: v for k, v in way.tags}, points


def build(pbf_paths, out_dir, built=None, node_cache=None, log=print):
    built = built or date.today().isoformat()
    blocks = defaultdict(lambda: defaultdict(list))  # bloc → zone → voies
    seen = {}
    for path in pbf_paths:
        log(f"Lecture de {path}")
        count = 0
        for way_id, tags, points in read_ways(path, node_cache):
            if way_id in seen:  # voie présente dans deux extraits voisins
                continue
            seen[way_id] = True
            record = way_record(way_id, tags, points)
            if record is None:
                continue
            count += 1
            for tile, parts in split_way_by_tile(points).items():
                for part in parts:
                    blocks[block_of(tile)][tile].append(
                        [way_id, record["hw"], record["ms"], record["ow"], encode_points(part)]
                    )
        log(f"  {count} routes")
    write_blocks(blocks, out_dir, built)
    return len(blocks)


def write_blocks(blocks, out_dir, built):
    total = 0
    for (bx, by), tiles in sorted(blocks.items()):
        payload = {
            "v": FORMAT_VERSION,
            "built": built,
            "hw": HIGHWAYS,
            "t": {f"{ix}:{iy}": ways for (ix, iy), ways in sorted(tiles.items())},
        }
        directory = os.path.join(out_dir, f"v{FORMAT_VERSION}", str(bx))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{by}.json.gz")
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as f:
            f.write(data)
        total += os.path.getsize(path)
    index = {"version": FORMAT_VERSION, "built": built, "blocks": len(blocks), "bytes": total}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(index, f)
    with open(os.path.join(out_dir, ".nojekyll"), "w"):
        pass
    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write(
            "# Données routières SpeedKM\n\n"
            "Routes et limitations de vitesse extraites d'OpenStreetMap, par blocs de 0,1°.\n\n"
            "© contributeurs OpenStreetMap — licence ODbL : https://www.openstreetmap.org/copyright\n"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pbf", nargs="+", help="extrait(s) .osm.pbf")
    parser.add_argument("--out", required=True, help="dossier de sortie")
    parser.add_argument("--built", help="date de génération (AAAA-MM-JJ)")
    parser.add_argument("--node-cache", help="fichier d'index des nœuds sur disque (grands extraits)")
    args = parser.parse_args(argv)
    blocks = build(args.pbf, args.out, args.built, args.node_cache)
    print(f"{blocks} blocs écrits dans {args.out}")


if __name__ == "__main__":
    sys.exit(main())
