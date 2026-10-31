# Driver provenance

- Source: https://github.com/popcornhax/PixCut-App
- Pinned commit: `9abe50ad2f02f6b51ec0b84257e2dd472cd6b3ab`
- License: MIT; retained at `vendor/PixCut-App/LICENSE`.
- The upstream checkout is unmodified. `setup.sh` pins this exact revision.
- Local modules use its USB transport, protocol client, printer raster padding,
  and PLT serializer. The local workflow owns image normalization, physical
  sizing, contours, confirmation, tracking, and print failure reporting.
- Python environment: local `.venv`, pinned in `requirements.lock`.

Protocol details are documented in the upstream
[USB protocol reference](https://github.com/popcornhax/PixCut-App/blob/9abe50ad2f02f6b51ec0b84257e2dd472cd6b3ab/docs/pixcut-usb-protocol.md).
