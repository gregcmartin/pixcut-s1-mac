# PixCut S1 — Mac CLI

Drop artwork into `create/`, prepare a sticker sheet, review it in the terminal,
choose a copy count, and confirm printing. All image processing runs locally.

**Status: USB communication verified; physical printing not yet verified.**
A connected PixCut S1 on firmware `1.0.34_0073` responds to identity, status,
media, and job-list queries. Artwork preparation and previews have been checked,
but no physical job has yet been sent from this implementation.

## Install and connect

Requires macOS, [Homebrew](https://brew.sh), and a PixCut S1 with a USB data cable.

```sh
git clone https://github.com/gregcmartin/pixcut-s1-mac.git
cd pixcut-s1-mac
./setup.sh
```

Install the ribbon and cutting blade, load **Liene 4×7 sticker paper** in its
cassette, and leave the front/rear paper path unobstructed. Power on the printer
and connect it to the Mac with a **USB data cable**. Close the Liene app if it
is holding the connection.

```sh
./pixcut doctor
./pixcut status
./pixcut watch
```

Copy a generated image into `create/`. The watcher waits for the file to stop
changing, generates the sheet, displays an approximate color preview, asks for
**sheet copies**, then asks you to type `PRINT 1` (or the chosen number).
Enter at the final prompt skips; `q` at the copy-count prompt also skips.
**No confirmation means no print.** Ctrl-C stops the watcher.

The watcher is a foreground terminal process. Nothing runs automatically when
the Mac starts. No cloud account, GUI app, or printer driver registration is
required. The repository is independent of and unaffiliated with Liene.

## Artwork and sizing

Default: one artwork placement per sheet, **65 mm wide**, with **no added
border**, 5 mm minimum sheet margins, and upstream knife
pressure 42. Physical cut alignment and pressure need checking on the first
sheet. Width excludes the border; height follows the image aspect ratio.

PNG with real transparency is best for a shaped laptop sticker. PNG, JPEG,
WebP, HEIC/HEIF, TIFF, and BMP are supported. Orientation and palette
transparency are handled. Only the first image/page of multi-frame files is used.

```sh
# Prepare without connecting to or printing on the device.
./pixcut prepare create/my-sticker.png --width-mm 65 --border-mm 0

# The command prints a JOB_ID; use that ID below.
./pixcut preview JOB_ID --terminal
./pixcut print JOB_ID --dry-run
./pixcut print JOB_ID

# Six smaller artwork placements on one sheet; copies counts whole sheets.
./pixcut prepare create/my-sticker.png --width-mm 30 --repeat 6
./pixcut print JOB_ID --copies 2

# Remove near-white background connected to the image's edges.
./pixcut prepare create/my-sticker.jpg --background white

# Watch with custom preparation defaults.
./pixcut watch --width-mm 60 --border-mm 2
./pixcut watch --background white

# Offline batch preparation, without prompts or USB access.
./pixcut watch --once --prepare-only
```

`--background auto` preserves transparency; opaque artwork gets a rectangular
outline. `--background keep` composites onto white and cuts a rectangle.
`--background white` is edge-connected color removal, **not AI subject
segmentation**. It preserves enclosed white details but can remove white parts
that touch the background. Inspect the preview. For complex backgrounds,
export a transparent version of the artwork before dropping it here.

Separate artwork islands produce separate cut outlines, with a warning.
Interior holes are not cut. Tiny components under 0.5 mm² are excluded from
cut geometry. A requested repeat count that will not fit is rejected without
silently dropping items. For oversized/tall art, reduce the width.

Terminal previews are approximate. `jobs/JOB_ID/preview.png` is the full
resolution preview. Its red paths show where the knife will cut; they are
absent from the print JPEG. `cut.svg` contains the vector outlines.

## Printer controls

```sh
./pixcut scan
./pixcut status
./pixcut status --props model firmware-revision serial-number media-size big-data
./pixcut jobs
./pixcut cancel          # Current job submitted by this CLI
./pixcut cancel 42       # A specific device job ID
./pixcut resume
./pixcut pause           # Experimental: retail firmware support is unverified
./pixcut history
./pixcut --help
```

Use another terminal for status/cancel during a print. The active process
shares its USB connection through a local socket to avoid competing readers.
Ctrl-C during a submitted print attempts cancellation, including during upload;
inspect device status afterward. A timeout/disconnect does not prove the
printer stopped. The CLI never automatically resends an uncertain job.

This version sends print-and-kiss-cut jobs on 4×7 sticker stock. It does not
perform firmware updates, calibration writes, photo-only or cut-only jobs,
or automated AI background removal. Lower-level upstream tools and protocol
documentation remain available under `vendor/PixCut-App/` for later expansion.

## Files, recovery, and repeat printing

- `create/`: inbox; original files are never deleted or moved.
- `jobs/JOB_ID/`: original snapshot, normalized artwork, padded print JPEG,
  PLT/SVG cut paths, preview, and `job.json`.
- `job.json`: dimensions, hashes, settings, copy counts, device job IDs,
  errors, and completion evidence.
- `jobs/JOB_ID/usb-logs/`: protocol traces from physical print attempts.
- `.state/`: watcher deduplication, process locks, and active control socket.

Images are deduplicated by contents and preparation settings across watcher
restarts, including renamed copies. Skipped/failed jobs stay available through
`history`; explicitly run `print JOB_ID` to try again. To change preparation,
run `prepare` with different options or change the image. Prepared files are
hash-checked immediately before printing. Copy count is limited to 1–36 sheets
per confirmation. Inspect output and remaining supplies before reprinting.

`needs-review` means the printer may have accepted the job before communication
failed. `in-progress` can also remain after a crash. Inspect the printer and
`jobs` before retrying either state. `completed` is recorded only after the
driver observes a cut phase, a completion report, and return to idle.

The final print prompt also confirms that **white 4×7 sticker paper** is loaded.
Some S1s report media code `5009` / `3x3` during 4×7 use, so that reading alone
cannot verify the physical paper. For this specific reading, the CLI requires
the operator's stock confirmation and records it alongside the unchanged
device response. Unknown readings and reported 4×6 photo stock remain blocked,
as do printer faults/busy states. No device settings are rewritten.
See the [upstream observations](https://github.com/eastbaymakersclub/pixcut-s1#job-sending).

## Connectivity and dependencies

This implementation uses USB, based on the MIT-licensed
[PixCut-App](https://github.com/popcornhax/PixCut-App) driver. The S1's advertised
wireless connection is Bluetooth; it has no advertised Wi-Fi interface.
[Another project](https://github.com/eastbaymakersclub/pixcut-s1) reports direct
BLE printing, but Bluetooth has not been integrated or tested here.
[Liene setup documentation](https://www.liene-life.com/pages/pixcut-help-center).

Setup uses Homebrew libusb and a local Python 3.12 environment. Dependencies
and the driver commit are pinned. See [UPSTREAM.md](UPSTREAM.md) for provenance
and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution.

If `doctor` detects no printer, check power and the data cable; a charge-only
cable will not work. `doctor` returns exit code 2 when absent. Status queries
and print attempts return nonzero on errors.

## Validation

```sh
PYTHONPATH=vendor/PixCut-App:. .venv/bin/python -m unittest discover -s tests -v
```

Automated tests cover geometry, transparency, repeat overflow, manifest
integrity, confirmation, error reporting, wrong media, and watcher
deduplication. An upstream cat sheet was prepared, visually inspected, and
dry-run checked. The copy-count and skip prompts were exercised in a terminal.

**Physical print/cut, registration, and blade pressure still require the first
approved sheet.** These are reverse-engineered
protocol tools, not an official Liene driver.
