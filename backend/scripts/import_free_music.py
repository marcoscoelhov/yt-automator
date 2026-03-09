#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
MUSIC_DIR = os.path.join(BACKEND_DIR, "assets", "music")
TRACKS_DIR = os.path.join(MUSIC_DIR, "tracks")
CATALOG_PATH = os.path.join(MUSIC_DIR, "catalog.json")


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "track"


def load_catalog() -> dict:
    if not os.path.exists(CATALOG_PATH):
        return {"provider": "Custom", "tracks": []}
    with open(CATALOG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_catalog(data: dict) -> None:
    os.makedirs(MUSIC_DIR, exist_ok=True)
    with open(CATALOG_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def normalize_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def download_to_temp(url: str) -> str:
    os.makedirs(TRACKS_DIR, exist_ok=True)
    suffix = os.path.splitext(url.split("?")[0])[1] or ".bin"
    fd, temp_path = tempfile.mkstemp(prefix="music_import_", suffix=suffix)
    os.close(fd)
    urllib.request.urlretrieve(url, temp_path)
    return temp_path


def transcode_to_mp3(source_path: str, output_path: str) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            source_path,
            "-vn",
            "-af",
            "aresample=48000,loudnorm=I=-28:TP=-2.0:LRA=9",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            output_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg failed")[:1200])


def upsert_track(catalog: dict, track: dict) -> None:
    tracks = catalog.setdefault("tracks", [])
    for index, existing in enumerate(tracks):
        if existing.get("id") == track["id"]:
            tracks[index] = track
            return
    tracks.append(track)


def main() -> int:
    parser = argparse.ArgumentParser(description="Importa uma trilha gratuita para o catalogo local.")
    parser.add_argument("--id", required=True, help="ID interno da faixa.")
    parser.add_argument("--title", required=True, help="Titulo da faixa.")
    parser.add_argument("--url", required=True, help="URL final do arquivo de audio (mp3/m4a/wav).")
    parser.add_argument("--source-page-url", default="", help="Pagina oficial da faixa no banco de musicas.")
    parser.add_argument("--provider", default="Pixabay Music", help="Nome do banco de musicas.")
    parser.add_argument("--license", default="Pixabay Content License", help="Descricao curta da licenca.")
    parser.add_argument("--license-url", default="https://pixabay.com/service/license-summary/", help="URL da licenca.")
    parser.add_argument("--moods", default="corporate,focused", help="Moods separados por virgula.")
    parser.add_argument("--themes", default="finance,business,education", help="Temas separados por virgula.")
    parser.add_argument("--tags", default="", help="Tags livres separadas por virgula.")
    parser.add_argument("--energy", default="medium", choices=["low", "medium", "high"], help="Energia da trilha.")
    parser.add_argument("--content-id-registered", action="store_true", help="Marcar se a faixa usa Content ID.")
    args = parser.parse_args()

    track_id = slugify(args.id)
    file_name = f"{track_id}.mp3"
    output_path = os.path.join(TRACKS_DIR, file_name)

    temp_path = download_to_temp(args.url)
    try:
        transcode_to_mp3(temp_path, output_path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    catalog = load_catalog()
    catalog["provider"] = catalog.get("provider") or args.provider
    catalog["license"] = catalog.get("license") or args.license
    catalog["license_url"] = catalog.get("license_url") or args.license_url

    upsert_track(
        catalog,
        {
            "id": track_id,
            "title": args.title.strip(),
            "provider": args.provider.strip(),
            "source_page_url": args.source_page_url.strip(),
            "local_path": f"tracks/{file_name}",
            "moods": normalize_csv(args.moods),
            "themes": normalize_csv(args.themes),
            "tags": normalize_csv(args.tags),
            "energy": args.energy,
            "content_id_registered": bool(args.content_id_registered),
            "license": args.license.strip(),
            "license_url": args.license_url.strip(),
        },
    )
    save_catalog(catalog)

    print(f"Faixa importada: {output_path}")
    print(f"Catalogo atualizado: {CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Erro ao importar trilha: {exc}", file=sys.stderr)
        raise SystemExit(1)
