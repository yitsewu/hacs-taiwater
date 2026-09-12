"""Developer-only deterministic export: pip install onnx==1.20.1 numpy.

Pass the upstream ddddocr 1.5.6 wheel with --wheel. End users never run this.
Only AST literal data, ONNX initializer tensors and fixed operator metadata are
read; upstream Python is not executed and no weights are retrained.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import onnx

WHEEL_SHA256 = "f13865b00e42de5c2507c1889ba73c2bacd218a49d15b928c2a5c82667062ac5"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1]/"custom_components/taiwater/models")
    args = parser.parse_args()
    if hashlib.sha256(args.wheel.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise ValueError("upstream_wheel_hash_mismatch")
    with ZipFile(args.wheel) as wheel:
        source = wheel.read("ddddocr/common_old.onnx")
        tree = ast.parse(wheel.read("ddddocr/__init__.py").decode("utf-8"))
        license_text = wheel.read("ddddocr-1.5.6.dist-info/LICENSE")
    model = onnx.load_model_from_string(source)
    weights = {t.name: onnx.numpy_helper.to_array(t) for t in model.graph.initializer}
    nodes = []
    for node in model.graph.node:
        attrs = {}
        for a in node.attribute:
            v = onnx.helper.get_attribute_value(a)
            if isinstance(v, bytes):
                v = v.decode()
            if isinstance(v, onnx.TensorProto):
                v = onnx.numpy_helper.to_array(v).tolist()
            attrs[a.name] = v
        nodes.append({"op":node.op_type,"inputs":list(node.input),"outputs":list(node.output),"attrs":attrs})
    charsets = [ast.literal_eval(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.List) and len(n.value.elts)>1000]
    args.output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output/"ocr_weights.npz", **weights)
    (args.output/"ocr_graph.json").write_text(json.dumps({"nodes":nodes,"charset":charsets[0],
        "output":model.graph.output[0].name},ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    (args.output/"LICENSE.ddddocr.txt").write_bytes(license_text)
    provenance = {
        "source": "https://pypi.org/project/ddddocr/1.5.6/", "license": "MIT",
        "repository": "https://github.com/sml2h3/ddddocr", "wheel": args.wheel.name,
        "wheel_sha256": WHEEL_SHA256, "source_model": "ddddocr/common_old.onnx",
        "source_model_sha256": hashlib.sha256(source).hexdigest(),
        "conversion": "Lossless initializer export; NumPy evaluator uses float32 LSTM activations instead of dynamic quantization.",
        "assets": {n: hashlib.sha256((args.output/n).read_bytes()).hexdigest()
                   for n in ("ocr_weights.npz", "ocr_graph.json")},
    }
    (args.output/"source.json").write_text(json.dumps(provenance,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(provenance))


if __name__ == "__main__":
    main()
