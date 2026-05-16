"""Print a flat summary of every bench.json under /tmp/engram_bench_out.
Used to populate docs/BENCHMARKS.md by hand."""
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/engram_bench_out")
for sub in sorted(root.iterdir()):
    bench = sub / "bench.json"
    if not bench.exists():
        continue
    d = json.loads(bench.read_text())
    print(f"\n## {d['alias']}")
    print(f"   size {d['size_mb']} MB, elapsed {d['elapsed_s']}s")
    c = d["counts"]
    print(f"   projects={c['project']} files={c['file']} "
          f"resources={c['resource']} entities={c['entity']} "
          f"edges={c['edge']} techs={c['technology']}")
    print(f"   top kinds: {[(k['kind'], k['n']) for k in d['resource_kinds'][:5]]}")
    print(f"   edges by type: {[(e['rel_type'], e['n']) for e in d['edge_types']]}")
    print(f"   cross-format services: {len(d['cross_format_services'])}")
    for s in d["cross_format_services"][:5]:
        print(f"     - {s['name']} (formats={s['formats']})")
