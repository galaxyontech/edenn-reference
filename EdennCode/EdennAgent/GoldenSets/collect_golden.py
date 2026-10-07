"""Golden dataset collector — wide range, licensed sources only.

Categories: ads (PRIMARY — matches ads-API authorization scope), psa,
vlog/home-movie, short-form, film, product/industrial. Sources: archive.org
(metadata API picks the mp4 automatically) + NASA images API. Each item is
trimmed to <=240s for the working set (full duration recorded in manifest).
"""
import json, subprocess, time, urllib.parse, urllib.request
from pathlib import Path

OUT = Path("/tmp/workdir/67137980-ee28-428d-a9d4-c08ff4503054/scratchpad/golden")
OUT.mkdir(exist_ok=True)

# (key, source, id, category, license, notes, trim_start, trim_len)
ITEMS = [
    # --- ADS (primary) ---
    ("ads_1960s_daytime", "archive", "Early1960sDaytimeCommercials", "ads",
     "public domain", "12 US daytime spots 1960-62", 5, 240),
    ("ads_classic_1948", "archive", "ClassicT1948", "ads",
     "public domain", "classic TV commercials reel pt1", 10, 240),
    ("ads_5060s", "archive", "Televisi1960", "ads",
     "public domain", "1950s-60s broadcast spots", 10, 240),
    ("ads_design_for_dreaming", "archive", "Designfo1956", "product",
     "public domain (Prelinger)", "GM 1956 industrial musical — product film", 30, 240),
    # --- PSA ---
    ("psa_duck_and_cover", "archive", "DuckandC1951", "psa",
     "public domain (Prelinger)", "civil defense PSA", 30, 240),
    # --- VLOG / home movie ---
    ("vlog_disneyland_family", "archive", "HMPhalFamilyDisneyl11824", "vlog",
     "public domain (Prelinger home movies)", "family trip, long takes", 30, 240),
    ("vlog_vacation_97518", "archive", "HMVacation97518", "vlog",
     "public domain (Prelinger home movies)", "vacation home movie", 30, 240),
    # --- SHORT-FORM / modern social ---
    ("short_artemis1_recap", "nasa", "NASA’s Artemis I Moon Mission - Launch to Splashdown Highlights",
     "short-form", "public domain (NASA)", "modern 2min mission recap", 0, 240),
    ("short_artemis_prep_5min", "nasa", "KSC-20250128-MH-NAS02-0001-Artemis_Success_and_Preparation_Short_Versions-M11615",
     "short-form", "public domain (NASA)", "modern 5min promo cut", 0, 240),
    # --- LONG broadcast (already-proven long->short source) ---
    ("long_artemis2_day1", "nasa", "Artemis_II_Flight_Day_1_NoLowerThirds",
     "broadcast", "public domain (NASA)", "modern crewed-launch broadcast", 60, 240),
    # --- FILM ---
    ("film_tears_of_steel", "url",
     "https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov",
     "film", "CC-BY (Blender Foundation)", "modern live-action short film", 60, 240),
]


def archive_mp4_url(identifier: str):
    meta = json.load(urllib.request.urlopen(
        f"https://archive.org/metadata/{urllib.parse.quote(identifier)}", timeout=30))
    files = meta.get("files") or []
    # prefer named mp4 derivatives, largest first
    mp4s = [f for f in files if str(f.get("name", "")).lower().endswith(".mp4")]
    if not mp4s:
        return None, None
    mp4s.sort(key=lambda f: int(f.get("size") or 0), reverse=True)
    name = mp4s[0]["name"]
    server = meta.get("d1") or "archive.org"
    return (f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}",
            int(mp4s[0].get("size") or 0))


def nasa_mp4_url(nasa_id: str):
    url = "https://images-api.nasa.gov/asset/" + urllib.parse.quote(nasa_id)
    d = json.load(urllib.request.urlopen(url, timeout=30))
    hrefs = [i["href"] for i in d["collection"]["items"]]
    for pref in ("~large.mp4", "~orig.mp4", "~medium.mp4"):
        for h in hrefs:
            if h.endswith(pref):
                return h.replace("http://", "https://").replace(
                    "images-assets.nasa.gov", "images-assets.nasa.gov"), None
    return None, None


LOCAL = {
    "ads_1960s_daytime": "/tmp/workdir/67137980-ee28-428d-a9d4-c08ff4503054/scratchpad/cases/ad_1960s_audio.mp4",
}

def fetch_with_fallback(url, dest, ss, tlen):
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(ss), "-i", url,
                        "-t", str(tlen), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dest)],
                       check=True, capture_output=True, timeout=900)
        return
    except Exception:
        tmp = dest.with_suffix(".dl")
        subprocess.run(["curl", "-sL", "--max-time", "600", "-o", str(tmp), url],
                       check=True, timeout=700)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(ss), "-i", str(tmp),
                        "-t", str(tlen), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dest)],
                       check=True, capture_output=True, timeout=900)
        tmp.unlink(missing_ok=True)

manifest = []
for key, source, ident, category, license_, notes, ss, tlen in ITEMS:
    dest = OUT / f"{key}.mp4"
    entry = {"key": key, "source": source, "id": ident, "category": category,
             "license": license_, "notes": notes}
    try:
        if dest.exists() and dest.stat().st_size > 500_000:
            print(f"[skip] {key} (cached)", flush=True)
        else:
            if source == "archive":
                url, _ = archive_mp4_url(ident)
            elif source == "nasa":
                url, _ = nasa_mp4_url(ident)
            else:
                url = ident
            t0 = time.time()
            if key in LOCAL:
                subprocess.run(["cp", LOCAL[key], str(dest)], check=True)
                entry["url"] = "local-reuse:" + LOCAL[key]
            else:
                if not url:
                    raise RuntimeError("no mp4 found")
                entry["url"] = url
                fetch_with_fallback(url, dest, ss, tlen)
            print(f"[ok] {key} ({time.time()-t0:.0f}s)", flush=True)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", str(dest)],
            capture_output=True, text=True, timeout=60)
        info = json.loads(probe.stdout or "{}")
        entry["duration_s"] = round(float(info.get("format", {}).get("duration") or 0), 1)
        entry["streams"] = [s.get("codec_type") for s in info.get("streams", [])]
        entry["size_mb"] = round(dest.stat().st_size / 1e6, 1)
        entry["path"] = str(dest)
        entry["status"] = "ok"
    except Exception as exc:
        entry["status"] = f"FAILED: {str(exc)[:140]}"
        print(f"[FAIL] {key}: {str(exc)[:140]}", flush=True)
    manifest.append(entry)

(OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
ok = sum(1 for m in manifest if m["status"] == "ok")
print(f"DONE: {ok}/{len(manifest)} collected -> {OUT}/manifest.json", flush=True)
