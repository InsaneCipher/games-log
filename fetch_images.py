"""
Download a cover image for every game in data/<year>.json into images/.

Covers come from IGDB (the games database run by Twitch). You need a free
Twitch developer app to use it:
  1. Go to https://dev.twitch.tv/console/apps and register an application
     (any name; OAuth redirect URL can be http://localhost; category "Application Integration").
  2. Copy the Client ID, then click "New Secret" to get a Client Secret.
  3. Put them in igdb_config.json next to this script:
        { "client_id": "...", "client_secret": "..." }
     (that file is listed in .gitignore so it never gets pushed to GitHub)
     or set the TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET environment variables.

Usage:
  python fetch_images.py            # fetch covers for games that don't have one yet
  python fetch_images.py --force    # re-download everything
  python fetch_images.py --dry-run  # just list what would be searched, no network
  python fetch_images.py --redo "Overwatch 2" "The Finals"   # re-fetch just these
  python fetch_images.py --lookup "Overwatch"   # list IGDB matches with their ids

Output:
  images/<slug>.jpg      one cover per unique game name
  images/manifest.json   maps each game name in your JSON to its image file,
                         plus which IGDB game it matched so you can spot mistakes

Fixing a wrong or missing match:
  Add the game to data/image_overrides.json, keyed by the exact "name" in your
  year file. The value can be a different search term, or an IGDB game id:
     {
       "Mafia": "Mafia: Definitive Edition",
       "Gambit": 123456
     }
  Then run with --force, or delete that game's image and run normally.
  (Find a game's id on igdb.com: it's shown on the game's page.)
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
IMAGES_DIR = ROOT / "images"
MANIFEST_PATH = IMAGES_DIR / "manifest.json"
OVERRIDES_PATH = DATA_DIR / "image_overrides.json"
CONFIG_PATH = ROOT / "igdb_config.json"

# t_cover_big is 264x374; t_cover_big_2x is 528x748 (sharper on phones/high-DPI screens)
IMAGE_SIZE = "t_cover_big_2x"
REQUEST_DELAY = 0.3  # IGDB allows 4 requests/second


# ---------- helpers ----------

def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalise(text):
    """Lower-case, strip accents and punctuation, for comparing titles."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r" version$", "", text)  # "Pokemon Platinum Version" == "Pokemon Platinum"
    words = [ROMAN.get(w, w) for w in text.split()]  # "Dungeons II" == "Dungeons 2"
    return " ".join(words)


ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def split_year(name):
    """'Doom (2016)' -> ('Doom', 2016); 'Portal 2' -> ('Portal 2', None)."""
    m = re.match(r"^(.*?)\s*\((\d{4})\)$", name)
    return (m.group(1), int(m.group(2))) if m else (name, None)


def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def http_post(url, body=None, headers=None):
    data = body.encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "games-log-cover-fetcher"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())


# ---------- reading your game list ----------

def all_game_names():
    """Every unique game name across all year files, in the order they first appear."""
    years = load_json(DATA_DIR / "years.json", [])
    seen = {}
    for year in years:
        year_data = load_json(DATA_DIR / f"{year}.json", {"months": []})
        for month in year_data.get("months", []):
            for game in month.get("games", []):
                name = game.get("name", "").strip()
                if name and name not in seen:
                    seen[name] = True
    return list(seen)


# ---------- IGDB ----------

# IGDB game_type ids: main game, standalone expansion, remake, remaster, expanded game, port
FULL_GAME_TYPES = "0,4,8,9,10,11"
GAME_TYPE_NAMES = {
    0: "main game", 1: "DLC", 2: "expansion", 3: "bundle", 4: "standalone", 5: "mod",
    6: "episode", 7: "season", 8: "remake", 9: "remaster", 10: "expanded", 11: "port",
    12: "fork", 13: "pack", 14: "update",
}

class IGDB:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        token_url = "https://id.twitch.tv/oauth2/token?" + urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        })
        self.token = http_post(token_url)["access_token"]

    def query(self, body):
        time.sleep(REQUEST_DELAY)
        return http_post(
            "https://api.igdb.com/v4/games",
            body,
            {
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )

    def lookup(self, term):
        """Print IGDB's results for a term, to help pick an override."""
        safe = term.replace('"', '\\"')
        results = self.query(
            f'search "{safe}"; fields name,first_release_date,game_type,version_parent,cover.image_id; limit 25;'
        )
        if not results:
            print("No results.")
        for g in results:
            year = time.gmtime(g["first_release_date"]).tm_year if g.get("first_release_date") else "????"
            kind = GAME_TYPE_NAMES.get(g.get("game_type"), "?")
            if g.get("version_parent"):
                kind = "edition"
            cover = "" if g.get("cover") else "  (no cover)"
            print(f"  id {g['id']:>7}  {year}  {kind:12} {g['name']}{cover}")

    def by_id(self, game_id):
        results = self.query(f"fields name,first_release_date,cover.image_id; where id = {int(game_id)};")
        return results[0] if results else None

    def search(self, title, year=None):
        safe = title.replace('"', '\\"')
        fields = "fields name,first_release_date,cover.image_id;"
        # 1. Exact title (case-insensitive), skipping special editions.
        #    IGDB's fuzzy search often ranks bundles/DLC above the base game,
        #    so this catches e.g. "Overwatch 2" or "The Finals" first.
        results = self.query(f'{fields} where name ~ "{safe}" & cover != null & version_parent = null; limit 20;')
        if results:
            return pick_best(results, title, year)
        # 2. Fuzzy search, only full games (no DLC, bundles, seasons, packs or special editions).
        try:
            results = self.query(
                f'search "{safe}"; {fields} where cover != null & version_parent = null '
                f"& game_type = ({FULL_GAME_TYPES}); limit 20;"
            )
        except urllib.error.HTTPError:
            results = []
        if results:
            return pick_best(results, title, year)
        results = self.query(f'search "{safe}"; {fields} where cover != null & version_parent = null; limit 20;')
        if results:
            return pick_best(results, title, year)
        # 3. Fuzzy search, anything with a cover.
        results = self.query(f'search "{safe}"; {fields} where cover != null; limit 20;')
        return pick_best(results, title, year)


def pick_best(results, title, year):
    """Choose the result that best matches the title (and year, if given)."""
    target = normalise(title)
    best, best_score = None, -1
    for rank, game in enumerate(results):
        if not game.get("cover"):
            continue
        name = normalise(game.get("name", ""))
        if name == target:
            score = 100
        elif name.startswith(target) or target.startswith(name):
            score = 60
        elif target in name or name in target:
            score = 40
        else:
            score = 10
        if year and game.get("first_release_date"):
            release_year = time.gmtime(game["first_release_date"]).tm_year
            if release_year == year:
                score += 50
            elif abs(release_year - year) == 1:
                score += 20  # release dates sometimes differ by region
        score -= rank * 0.5  # tie-break on IGDB's own relevance order
        if score > best_score:
            best, best_score = game, score
    return best


# ---------- main ----------

def load_credentials():
    config = load_json(CONFIG_PATH, {})
    client_id = os.environ.get("TWITCH_CLIENT_ID") or config.get("client_id")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET") or config.get("client_secret")
    if not client_id or not client_secret:
        sys.exit(
            "Missing IGDB credentials. Create igdb_config.json with your Twitch "
            '"client_id" and "client_secret" (see the instructions at the top of this script).'
        )
    return client_id, client_secret


def main():
    parser = argparse.ArgumentParser(description="Fetch game cover images from IGDB.")
    parser.add_argument("--force", action="store_true", help="re-download images that already exist")
    parser.add_argument("--dry-run", action="store_true", help="list searches without contacting IGDB")
    parser.add_argument("--redo", nargs="+", metavar="NAME", help='re-fetch only these games, e.g. --redo "Overwatch 2" "The Finals"')
    parser.add_argument("--lookup", metavar="TERM", help='show IGDB results for a search term, with ids for overrides')
    args = parser.parse_args()

    if args.lookup:
        IGDB(*load_credentials()).lookup(args.lookup)
        return

    IMAGES_DIR.mkdir(exist_ok=True)
    names = all_game_names()
    overrides = load_json(OVERRIDES_PATH, {})
    manifest = load_json(MANIFEST_PATH, {})

    todo = []
    if args.redo:
        unknown = [n for n in args.redo if n not in names]
        if unknown:
            sys.exit("Not in your year files (check spelling): " + ", ".join(unknown))
        names = args.redo
        args.force = True
    for name in names:
        entry = manifest.get(name)
        has_image = entry and (ROOT / entry["file"]).exists()
        if args.force or not has_image:
            todo.append(name)

    print(f"{len(names)} unique games, {len(todo)} need an image.")
    if args.dry_run:
        for name in todo:
            override = overrides.get(name)
            title, year = split_year(override if isinstance(override, str) else name)
            how = f"IGDB id {override}" if isinstance(override, int) else f'search "{title}"' + (f" ({year})" if year else "")
            print(f"  {name:45} -> {how}")
        return
    if not todo:
        return

    igdb = IGDB(*load_credentials())
    failed, check = [], []

    for i, name in enumerate(todo, 1):
        override = overrides.get(name)
        try:
            if isinstance(override, int):
                game = igdb.by_id(override)
            else:
                title, year = split_year(override if isinstance(override, str) else name)
                game = igdb.search(title, year)
        except urllib.error.HTTPError as e:
            print(f"[{i}/{len(todo)}] {name}: IGDB error {e.code}")
            failed.append(name)
            continue

        if not game or not game.get("cover"):
            print(f"[{i}/{len(todo)}] {name}: no match found")
            failed.append(name)
            continue

        filename = f"{slugify(name)}.jpg"
        url = f"https://images.igdb.com/igdb/image/upload/{IMAGE_SIZE}/{game['cover']['image_id']}.jpg"
        try:
            download(url, IMAGES_DIR / filename)
        except urllib.error.URLError as e:
            print(f"[{i}/{len(todo)}] {name}: download failed ({e})")
            failed.append(name)
            continue

        release_year = time.gmtime(game["first_release_date"]).tm_year if game.get("first_release_date") else None
        manifest[name] = {
            "file": f"images/{filename}",
            "matched": game["name"],
            "year": release_year,
            "igdb_id": game["id"],
        }
        flag = ""
        if override is None and normalise(game["name"]) != normalise(split_year(name)[0]):
            flag = "   <- check this"
            check.append(f"{name}  ->  {game['name']} ({release_year})")
        print(f"[{i}/{len(todo)}] {name} -> {game['name']} ({release_year}){flag}")

        # save as we go, so a crash halfway doesn't lose progress
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(manifest.items())), f, ensure_ascii=False, indent=2)

    print()
    if check:
        print("Matched a different title, worth a look:")
        for line in check:
            print("  " + line)
    if failed:
        print("No image found (add these to data/image_overrides.json):")
        for name in failed:
            print("  " + name)
    if not check and not failed:
        print("All done, every game matched exactly.")


if __name__ == "__main__":
    main()
