"""
Recovery script: undo parasitic renames caused by self-moves.
When source == destination, _resolve_conflict was renaming files with _1.
This script renames them back.
Usage:
  python recovery_rename_back.py                        # log mode, dry-run
  python recovery_rename_back.py --apply                # log mode, execute
  python recovery_rename_back.py --dir D:\\path\\to\\dir  # dir mode, dry-run
  python recovery_rename_back.py --dir D:\\path --apply  # dir mode, execute
"""
import os, re, sys

LOG_FILE = r"d:\00-Archives\Logs_ORGZ\rapport_organizator_detaille_20260519_212424.txt"
DRY_RUN = "--apply" not in sys.argv

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

# Parse --dir argument if provided
SCAN_DIR = None
if "--dir" in sys.argv:
    idx = sys.argv.index("--dir")
    if idx + 1 < len(sys.argv):
        SCAN_DIR = sys.argv[idx + 1]

"""
Recovery script: undo parasitic renames caused by self-moves.
When source == destination, _resolve_conflict was renaming files with _1.
This script renames them back.
Usage:
  python recovery_rename_back.py                               # log mode, dry-run
  python recovery_rename_back.py --apply                       # log mode, execute
  python recovery_rename_back.py --dir D:\\path\\to\\dir         # dir mode (_1), dry-run
  python recovery_rename_back.py --dir D:\\path --apply         # dir mode (_1), execute
  python recovery_rename_back.py --strip-underscore --dir D:\\path          # strip trailing _ , dry-run
  python recovery_rename_back.py --strip-underscore --dir D:\\path --apply  # strip trailing _ , execute
"""
import os, re, sys

LOG_FILE = r"d:\00-Archives\Logs_ORGZ\rapport_organizator_detaille_20260519_212424.txt"
DRY_RUN = "--apply" not in sys.argv
STRIP_UNDERSCORE = "--strip-underscore" in sys.argv

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

# Parse --dir argument if provided
SCAN_DIR = None
if "--dir" in sys.argv:
    idx = sys.argv.index("--dir")
    if idx + 1 < len(sys.argv):
        SCAN_DIR = sys.argv[idx + 1]

LINE_RE = re.compile(r"^\s+- (.+?) -> (.+?) \[")
renamed_pairs = []  # list of (orig_name, dst_path, dst_dir)

if STRIP_UNDERSCORE and SCAN_DIR:
    # Strip trailing underscore mode:
    # ComfyUI_01580_.png  -> ComfyUI_01580.png
    # ComfyUI_01580_.txt  -> ComfyUI_01580.txt
    # ComfyUI_01580__validation.json -> ComfyUI_01580_validation.json
    # Only processes images; companions handled per image found
    for root, dirs, files in os.walk(SCAN_DIR):
        dirs.sort()
        for fname in sorted(files):
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in IMAGE_EXTS:
                continue
            if not stem.endswith("_"):
                continue
            orig_stem = stem[:-1]  # remove the trailing _
            orig_name = orig_stem + ext
            dst_path = os.path.join(root, fname)
            if not os.path.isfile(os.path.join(root, orig_name)):
                renamed_pairs.append((orig_name, dst_path, root))

elif SCAN_DIR:
    # Directory scan mode: find image files ending with exactly _1 suffix
    # (the parasitic counter always starts at 1 in _resolve_conflict)
    # For each foo_1.ext where foo.ext does not exist -> candidate
    SUFFIX_RE = re.compile(r"^(.+)_1$")
    for root, dirs, files in os.walk(SCAN_DIR):
        dirs.sort()
        for fname in sorted(files):
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in IMAGE_EXTS:
                continue
            m = SUFFIX_RE.match(stem)
            if not m:
                continue
            orig_stem = m.group(1)
            orig_name = orig_stem + ext
            dst_path = os.path.join(root, fname)
            # Only rename back if the original doesn't already exist
            if not os.path.isfile(os.path.join(root, orig_name)):
                renamed_pairs.append((orig_name, dst_path, root))
else:
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            src_path = m.group(1).strip().replace("/", "\\")
            dst_path = m.group(2).strip().replace("/", "\\")
            src_name = os.path.basename(src_path)
            dst_name = os.path.basename(dst_path)
            if src_name == dst_name:
                continue
            src_stem, src_ext = os.path.splitext(src_name)
            expected = re.compile(re.escape(src_stem) + r"_\d+" + re.escape(src_ext) + r"$", re.IGNORECASE)
            if not expected.match(dst_name):
                continue
            renamed_pairs.append((src_name, dst_path, os.path.dirname(dst_path)))

print("Files to rename back: %d" % len(renamed_pairs))
errors = renamed_count = companions_count = skipped = 0

for orig_name, dst_path, dst_dir in renamed_pairs:
    target = os.path.join(dst_dir, orig_name)
    if not os.path.isfile(dst_path):
        skipped += 1; continue
    if os.path.isfile(target):
        print("  [CONFLICT] %s" % target); skipped += 1; continue
    if DRY_RUN:
        print("  [DRY] %s -> %s" % (os.path.basename(dst_path), orig_name))
    else:
        try:
            os.rename(dst_path, target)
            print("  [OK] %s -> %s" % (os.path.basename(dst_path), orig_name))
            renamed_count += 1
        except Exception as e:
            print("  [ERR] %s: %s" % (dst_path, e)); errors += 1; continue

    orig_stem = os.path.splitext(orig_name)[0]
    # Build companion rename candidates based on mode
    if STRIP_UNDERSCORE:
        # orig_stem has no trailing _ ; current file had trailing _
        bad_stem = orig_stem + "_"  # e.g. "ComfyUI_01580_"
        candidates = [
            (bad_stem + "_validation.json", orig_stem + "_validation.json"),
            (bad_stem + ".txt",             orig_stem + ".txt"),
            (bad_stem + ".json",            orig_stem + ".json"),
        ]
    else:
        candidates = [
            (orig_stem + "_validation_1.json", orig_stem + "_validation.json"),
            (orig_stem + "_1.txt",             orig_stem + ".txt"),
            (orig_stem + "_1.json",            orig_stem + ".json"),
        ]
    for comp_renamed, comp_orig in candidates:
        comp_src = os.path.join(dst_dir, comp_renamed)
        comp_tgt = os.path.join(dst_dir, comp_orig)
        if not os.path.isfile(comp_src): continue
        if os.path.isfile(comp_tgt): print("    [CMP-CONFLICT] %s" % comp_tgt); continue
        if DRY_RUN:
            print("    [DRY-CMP] %s -> %s" % (comp_renamed, comp_orig))
        else:
            try:
                os.rename(comp_src, comp_tgt)
                print("    [CMP-OK] %s -> %s" % (comp_renamed, comp_orig))
                companions_count += 1
            except Exception as e:
                print("    [CMP-ERR] %s: %s" % (comp_src, e)); errors += 1

print()
if DRY_RUN:
    print("=== DRY RUN - %d files to process. Run with --apply to execute. ===" % len(renamed_pairs))
else:
    print("=== Done: %d images | %d companions | %d skipped | %d errors ===" % (renamed_count, companions_count, skipped, errors))
