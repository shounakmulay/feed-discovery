import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, str(root / "scripts" / "collect_feeds.py"), "verify"], check=True)
master = json.loads((root / "master.json").read_text(encoding="utf-8"))
collections = sorted((root / "collections").glob("*/*.json"))
for collection in collections:
    if collection.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", collection.stem):
        raise ValueError("Unexpected collection file: " + str(collection))
    if hashlib.sha256(collection.read_bytes()).hexdigest() != collection.stem:
        raise ValueError("Collection filename does not match its content: " + str(collection))

output = root / "dist"
if output.is_symlink():
    raise ValueError("Refusing to replace a symlink at dist")
with tempfile.TemporaryDirectory(prefix="pages-build-", dir=root) as temporary:
    stage = Path(temporary)
    shutil.copy2(root / "master.json", stage / "master.json")
    for collection in collections:
        target = stage / collection.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(collection, target)
    (stage / "_headers").write_text(
        "/*\n"
        "  Access-Control-Allow-Origin: *\n"
        "  Access-Control-Expose-Headers: ETag\n\n"
        "/master.json\n"
        "  Cache-Control: private, max-age=0, must-revalidate\n\n"
        "/collections/*\n"
        "  Cache-Control: private, max-age=31536000, immutable\n",
        encoding="utf-8",
    )
    shutil.copy2(root / "cloudflare" / "_worker.js", stage / "_worker.js")
    (stage / "_routes.json").write_text(json.dumps({"version": 1, "include": ["/*"], "exclude": []}) + "\n", encoding="utf-8")
    (stage / "404.html").write_text("<!doctype html><title>Not found</title><h1>Not found</h1>\n", encoding="utf-8")
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(stage, output)

archive = shutil.make_archive(str(root / "feed-discovery-pages"), "zip", root_dir=output)
print("Prepared", len(collections), "immutable collection files, including", len(master["collections"]), "current collections")
print("Pages upload folder:", output)
print("Pages upload ZIP:", archive)
