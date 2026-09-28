#!/usr/bin/env python3
import argparse
from pathlib import Path
from review_render import Renderer, bundle_at
from review_store import Store

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--target", required=True)
p.add_argument("--output", required=True)
p.add_argument("--pr")
p.add_argument("--generation", type=int)
a = p.parse_args()
s = Store(a.data)
b = bundle_at(s, a.pr, a.generation) if a.pr else None
Path(a.output).write_bytes(Renderer(s).render(a.target, b))
