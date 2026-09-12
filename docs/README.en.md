# Taiwan Water: built-in local OCR

[繁體中文](../README.md) · [![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=yitsewu&repository=hacs-taiwater&category=integration) · [MIT License](../LICENSE)

Version 0.4.0, tested with Home Assistant Core 2026.9.1. This is an unofficial integration for Taiwan Water Corporation.

## Install

Add `https://github.com/yitsewu/hacs-taiwater` as a HACS custom Integration repository, download and restart HA normally. Alternatively, use the release ZIP and copy the entire `custom_components/taiwater` directory, including `models`, into `/config/custom_components`.

Add Taiwan Water Corporation under Settings → Devices & services. Enter the water ID and customer name. OCR runs inside HA Core on your own host. No app/add-on, container, shell command, OCR URL or manual model download is required. Schedule and history options remain configurable. Existing accounts and saved bills survive upgrades.

HA resolves `numpy==2.3.2` and `Pillow==12.3.0` through manifest requirements, matching Core 2026.9.1 constraints. The integration never runs pip, changes shared package versions, modifies BLAS settings, or requires changing VM CPU flags. Dependency resolution needs working package sources; the model is bundled and inference is offline.

## Model and recovery

MIT-licensed ddddocr 1.5.6 `common_old.onnx` weights are exported without training. Source hashes, the original license and a developer-only exporter are included. SHA-256 verification and NumPy `allow_pickle=False` protect model loading. No ONNX Runtime, OpenCV, ddddocr runtime or bundled native executable is needed. Quantized convolutions are retained; LSTM activations use float32 rather than dynamic quantization, so numeric identity to ONNX Runtime is not claimed. Recognition is tested against known synthetic text.

One lazy model is shared across accounts, with serialized executor inference and a 30-second bounded lock wait. Failed initialization/inference resets the cache with a 30-second retry backoff. Limits: 2 MB encoded image, 4 million pixels, and resized input 64 pixels high by at most 512 wide. No CAPTCHA, cookie, raw HTML or account data is logged.

Manual verification remains available. An optional external OCR endpoint can be selected in advanced options; leaving it empty always uses built-in OCR, even when an OCR add-on exists. The `ocr_backend` sensor and new query records identify built-in, external or manual operation. OCR recognition, upstream acceptance, query success and statistics publication remain separate. Failed queries retain saved bills.

## Verified platforms

Official unmodified Core 2026.9.1 containers: Python 3.14.6, musl, amd64 and aarch64, with HA's actual requirements resolver and no external OCR. All three synthetic texts match exactly. Warm inference is approximately 0.30–0.34 seconds on amd64 and 0.22–0.25 on aarch64 CI runners; whole-process peak RSS is about 175 MiB, not incremental OCR memory. The same Core image also passes under QEMU's SSE2/SSE3 `qemu64` CPU baseline. Emulator timing is not physical-device performance. Other Core releases and 32-bit platforms are unverified.

Back up HA before upgrading. Replace the complete integration and restart; account entry, history, scheduling, allocation preferences and background statistics are retained. Monthly amounts are bill allocations, not actual monthly meter readings. Carbon requires upstream values or a configured factor with source/year. External long-term statistics are rebuilt in the background.

New accounts default to equal allocation across covered months (60 m³ / TWD 600 over two months becomes 30 m³ / TWD 300 each). Existing choices remain unchanged; legacy accounts without an explicit option retain the old day-based default. Change allocation in integration options; reload rebuilds owned statistics. The Rebuild statistics button retries from saved bills without contacting Taiwan Water. Display name changes retain all entity/statistic IDs.

## License

Code is licensed under [MIT](../LICENSE). The bundled OCR model retains its [upstream MIT notice](../custom_components/taiwater/models/LICENSE.ddddocr.txt). Taiwan Water Corporation owns its logo; the logo is excluded from the code license.
