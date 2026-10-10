# Données routières SpeedKM

Génère, à partir d'OpenStreetMap, les fichiers de routes et de limitations de vitesse lus par
l'application SpeedKM, pour les publier sur un hébergement statique gratuit (GitHub Pages).
L'application n'interroge alors plus le serveur public Overpass (instable) pour les régions publiées.

- `build_roadtiles.py` : le générateur (Python + pyosmium).
- `test_build_roadtiles.py` : tests (`python -m unittest`) — dont la parité de lecture de `maxspeed`
  avec l'application (`maxspeed_parser.dart`).
- `regions.txt` : régions Geofabrik à publier.
- `.github/workflows/build-roads.yml` : régénération mensuelle automatique + publication.

Format : un fichier `v1/<bx>/<by>.json.gz` par bloc de 0,1° (≈ 11 × 7 km), qui contient ses zones de
0,01° (celles de `SpeedLimitRepositoryImpl.tileDegrees`). Mesures : Île-de-France = 194 fichiers,
8 Mo au total, le plus gros (Paris) fait 317 Ko ; génération en 25 s.

Mise en place complète : voir `docs/donnees-routieres.md` du projet SpeedKM.

Données © contributeurs OpenStreetMap, licence ODbL — https://www.openstreetmap.org/copyright
