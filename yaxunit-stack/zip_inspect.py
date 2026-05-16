import zipfile, sys
p = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runs/latest/input.zip"
with zipfile.ZipFile(p) as z:
    for n in z.infolist()[:8]:
        print(repr(n.filename), "flag_utf8=", bool(n.flag_bits & 0x800))
