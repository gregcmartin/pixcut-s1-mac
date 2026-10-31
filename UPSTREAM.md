# Driver provenance

- Source: https://github.com/popcornhax/PixCut-App
- Reviewed and installed commit: `9abe50ad2f02f6b51ec0b84257e2dd472cd6b3ab`
- License: MIT; retained at `vendor/PixCut-App/LICENSE`.
- The upstream checkout is unmodified. `setup.sh` pins this exact revision.
- Local modules use its USB transport, protocol client, printer raster padding,
  and PLT serializer. The local workflow owns image normalization, physical
  sizing, contours, confirmation, tracking, and print failure reporting.
- Python environment: local `.venv`, pinned in `requirements.lock`.

Other projects reviewed:

- https://github.com/eastbaymakersclub/pixcut-s1 — USB/BLE research and a reported
  BLE print/cut smoke test. Not installed or verified on this printer.
- https://github.com/honeymaro/pixcut — TypeScript cut-path generation; not a
  complete printer transport.
- https://www.liene-life.com/pages/pixcut-help-center — manufacturer setup and
  media information. Wireless connection is Bluetooth, not Wi-Fi.

USB endpoints: vendor/product `302c:3101`, control interface 2 (`06/86`), data
interface 3 (`04/84`). Logical 4×7 sheet is 1200×2100 px at 300 DPI; printer
raster is 1216×2128 with 8 px horizontal / 14 px vertical padding. PLT axes
are rotated relative to the image, using 1016 units/inch. These values come
from the pinned upstream implementation/protocol notes. Physical registration
and knife pressure must be checked on this particular printer's first sheet.

`pause-printer` is exposed for exploration but explicitly unverified in the
upstream firmware notes. Firmware rejection is returned as a CLI error.
