"""Small, single-threaded NumPy evaluator for the bundled ddddocr graph only.

No ONNX loader, executable model, OpenCV, BLAS thread settings or native binaries.
Integer convolutions match the exported quantized graph. LSTM weights are
dequantized to float32; recurrent activation quantization is intentionally omitted.
"""
from collections import Counter

import numpy as np


def sigmoid(x):
    return 1 / (1 + np.exp(-np.clip(x, -80, 80)))


def quantize(x):
    low, high = min(float(x.min()), 0), max(float(x.max()), 0)
    scale = np.float32((high - low) / 255 if high != low else 1)
    zero = np.uint8(np.clip(np.rint(-low / scale), 0, 255))
    return np.clip(np.rint(x / scale) + zero, 0, 255).astype(np.uint8), scale, zero


def convolution(x, w, xzero, wzero, attrs):
    x = x.astype(np.int32) - xzero
    w = w.astype(np.int32) - wzero
    top, left, bottom, right = attrs["pads"]
    sy, sx = attrs["strides"]
    kh, kw = w.shape[2:]
    x = np.pad(x, ((0, 0), (0, 0), (top, bottom), (left, right)))
    oh, ow = (x.shape[2] - kh) // sy + 1, (x.shape[3] - kw) // sx + 1
    result = np.zeros((x.shape[0], w.shape[0], oh, ow), dtype=np.int32)
    for y in range(kh):
        for z in range(kw):
            result += np.einsum("nchw,oc->nohw", x[:, :, y:y+oh*sy:sy, z:z+ow*sx:sx],
                                w[:, :, y, z], optimize=False)
    return result


def lstm(values, attrs):
    x, w, r, bias, _, h0, c0, _, ws, wz, rs, rz = values
    hidden = attrs["hidden_size"]
    output = np.empty((x.shape[0], 2, x.shape[1], hidden), dtype=np.float32)
    final_h, final_c = h0.copy(), c0.copy()
    for direction in range(2):
        weight = (w[direction].astype(np.float32) - wz[direction]) * ws[direction]
        recurrent = (r[direction].astype(np.float32) - rz[direction]) * rs[direction]
        projected = np.einsum("tbi,ij->tbj", x, weight, optimize=False)
        b = bias[direction, :4*hidden] + bias[direction, 4*hidden:]
        h, c = h0[direction].copy(), c0[direction].copy()
        for t in range(len(x)) if direction == 0 else reversed(range(len(x))):
            gates = projected[t] + np.einsum("bi,ij->bj", h, recurrent, optimize=False) + b
            i, o, f, cell = np.split(gates, 4, axis=-1)
            c = sigmoid(f) * c + sigmoid(i) * np.tanh(cell)
            h = sigmoid(o) * np.tanh(c)
            output[t, direction] = h
        final_h[direction], final_c[direction] = h, c
    return output, final_h, final_c


class Model:
    """Evaluate a fixed, integrity-checked graph; discard intermediates at last use."""

    def __init__(self, graph, weights):
        self.graph = graph
        self.weights = weights
        self.uses = Counter(i for node in graph["nodes"] for i in node["inputs"] if i)

    def predict(self, image):
        data = {**self.weights, "input1": image}
        uses = self.uses.copy()
        for node in self.graph["nodes"]:
            values = [data[i] if i else None for i in node["inputs"]]
            a, op = node["attrs"], node["op"]
            x = values[0]
            if op == "DynamicQuantizeLinear":
                outputs = quantize(x)
            elif op == "ConvInteger":
                outputs = (convolution(*values, a),)
            elif op == "DynamicQuantizeLSTM":
                outputs = lstm(values, a)
            elif op == "MatMulInteger":
                outputs = (np.einsum("bi,ij->bj", x.astype(np.int32)-values[2],
                                     values[1].astype(np.int32)-values[3], optimize=False),)
            elif op in {"Add", "Mul", "Div"}:
                outputs = ({"Add": np.add, "Mul": np.multiply, "Div": np.divide}[op](x, values[1]),)
            elif op == "Floor":
                outputs = (np.floor(x),)
            elif op == "Cast":
                outputs = (x.astype({1: np.float32, 6: np.int32, 7: np.int64}[a["to"]]),)
            elif op == "Sigmoid":
                outputs = (sigmoid(x),)
            elif op == "Reshape":
                outputs = (x.reshape(tuple(x.shape[i] if v == 0 else v for i, v in enumerate(values[1]))),)
            elif op == "Transpose":
                outputs = (x.transpose(a["perm"]),)
            elif op == "Shape":
                outputs = (np.array(x.shape, dtype=np.int64),)
            elif op == "Gather":
                outputs = (np.take(x, values[1], axis=a["axis"]),)
            elif op == "Unsqueeze":
                outputs = (np.expand_dims(x, tuple(a["axes"])),)
            elif op == "Concat":
                outputs = (np.concatenate(values, axis=a["axis"]),)
            elif op == "ConstantOfShape":
                outputs = (np.full(tuple(x), a.get("value", [0])[0], dtype=np.float32),)
            else:
                raise ValueError("unsupported_model_operator")
            data.update(zip(node["outputs"], outputs, strict=True))
            for key in node["inputs"]:
                uses[key] -= 1
                if uses[key] == 0 and key not in self.weights:
                    data.pop(key, None)
        indices = data[self.graph["output"]].argmax(axis=2).reshape(-1)
        result, previous = [], 0
        for index in indices:
            if index != previous and index:
                result.append(self.graph["charset"][index])
            previous = index
        return "".join(result)
