import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from . import device
from .layout import EXTENSIONS, Options, digest, fingerprint, render
from .storage import JOBS, ROOT, STATE, lock, now, read_json, write_json


def get_options(args):
    return Options(args.width_mm, args.border_mm, args.repeat, args.background, args.knife_pressure)


def prepare(source, options):
    source = Path(source).expanduser().resolve()
    if source.suffix.lower() not in EXTENSIONS:
        raise ValueError(f'Unsupported image type: {source.suffix}')
    before = source.stat()
    data = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Image is still changing; try again after the copy finishes.')
    key = fingerprint(data, options)
    JOBS.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime('%Y%m%d-%H%M%S') + '-' + key[:8] + '-' + uuid.uuid4().hex[:4]
    staging = Path(tempfile.mkdtemp(prefix='.preparing-', dir=JOBS))
    try:
        info = render(data, options, staging)
        (staging / ('source' + source.suffix.lower())).write_bytes(data)
        manifest = {
            'id': job_id, 'created': now(), 'source': str(source),
            'source_sha256': digest(data), 'fingerprint': key,
            'options': asdict(options), 'layout': info, 'status': 'prepared', 'attempts': [],
            'media': 'Liene 4×7 sticker stock', 'media_size': 5013, 'media_type': 2030,
        }
        write_json(staging / 'job.json', manifest)
        target = JOBS / job_id
        os.replace(staging, target)
        return target
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def describe(job):
    m = read_json(job / 'job.json')
    if not isinstance(m, dict):
        raise ValueError(f'Missing job.json in {job}')
    art = m['layout']['artwork_mm']
    print(f"\nJob: {m['id']}\nArtwork: {art[0]} × {art[1]} mm; border: {m['options']['border_mm']} mm")
    print(f"Sheet: 4×7 in; {m['options']['repeat']} sticker(s); {m['layout']['cut_paths']} cut path(s); knife pressure {m['options']['knife_pressure']}")
    print(f"Preview (red = cut line, not printed): {job / 'preview.png'}")
    for warning in m['layout']['warnings']:
        print(f'Note: {warning}')
    print(f"Status: {m['status']}")
    return m


def resolve_job(value):
    path = Path(value).expanduser()
    if path.is_dir() and (path / 'job.json').is_file():
        return path.resolve()
    path = JOBS / value
    if not path.is_dir() or not (path / 'job.json').is_file():
        raise ValueError(f'Job not found: {value}. Run ./pixcut history.')
    return path.resolve()


def validate_artifacts(job, manifest):
    from PIL import Image
    for name, expected in manifest['layout']['sha256'].items():
        if name not in ('print.jpg', 'cut.plt', 'preview.png'):
            raise ValueError('Unexpected artifact in manifest.')
        if digest((job / name).read_bytes()) != expected:
            raise ValueError(f'{name} changed after preparation; prepare the source again.')
    if set(manifest['layout']['sha256']) != {'print.jpg', 'cut.plt', 'preview.png'}:
        raise ValueError('Incomplete job manifest.')
    with Image.open(job / 'print.jpg') as image:
        if image.size != (1216, 2128) or image.format != 'JPEG':
            raise ValueError('Unexpected printer raster dimensions or format.')
    if (job / 'print.jpg').stat().st_size >= 1024 * 1024:
        raise ValueError('Printer JPEG exceeds the 1 MiB limit.')


def terminal_preview(job):
    from PIL import Image
    width = min(60, max(20, shutil.get_terminal_size((80, 24)).columns - 2))
    with Image.open(job / 'preview.png') as source:
        image = source.convert('RGB').resize((width, round(source.height / source.width * width)))
    print('Terminal preview (approximate; red outlines are cut paths):')
    for y in range(0, image.height - 1, 2):
        line = []
        for x in range(width):
            r, g, b = image.getpixel((x, y))
            r2, g2, b2 = image.getpixel((x, y + 1))
            line.append(f'\033[38;2;{r};{g};{b}m\033[48;2;{r2};{g2};{b2}m▀')
        print(''.join(line) + '\033[0m')


def confirm_copies(copies):
    if not sys.stdin.isatty():
        raise ValueError('Printing requires an interactive terminal. No --yes override is provided.')
    if copies is None:
        value = input('How many sheet copies? [1; Enter=1, q=skip] ').strip()
        if value.lower() == 'q':
            return None
        try:
            copies = int(value or '1')
        except ValueError:
            raise ValueError('Copies must be a whole number from 1 to 36.') from None
    if not 1 <= copies <= 36:
        raise ValueError('Copies must be 1–36 sheets.')
    expected = f'PRINT {copies}'
    answer = input(f'Review the preview and load 4×7 sticker media. Type {expected} to print and cut ({copies} sheet(s)); Enter skips: ')
    return copies if answer.strip() == expected else None


def print_job(job, copies=None, dry_run=False):
    with lock(job / '.lock'):
        manifest = describe(job)
        validate_artifacts(job, manifest)
        if dry_run:
            print('DRY RUN: files verified; no USB connection or print request made.')
            return 'dry-run'
        if sys.stdout.isatty():
            terminal_preview(job)
        if manifest['attempts']:
            print('This job has previous print attempts. Check history and the physical output before deliberately reprinting.')
        copies = confirm_copies(copies)
        if copies is None:
            print('Skipped. Prepared files are retained for later printing.')
            return 'skipped'
        validate_artifacts(job, manifest)
        attempt = {'started': now(), 'copies': copies, 'phase': 'connecting'}
        manifest['attempts'].append(attempt)
        manifest['status'] = 'in-progress'
        write_json(job / 'job.json', manifest)

        def update(values):
            attempt.update(values)
            write_json(job / 'job.json', manifest)

        try:
            result = device.send(job, copies, update)
            attempt.update({'phase': 'completed', 'result': result, 'finished': now()})
            manifest['status'] = 'completed'
            print('Printer confirmed print and cut completion.')
            return 'completed'
        except BaseException as exc:
            uncertain = attempt['phase'] not in ('connecting', 'ready')
            manifest['status'] = 'needs-review' if uncertain else 'failed-before-submission'
            attempt.update({'error': str(exc) or type(exc).__name__, 'finished': now()})
            print('Print stopped. Nothing will be automatically retried. Check ./pixcut status and ./pixcut history.', file=sys.stderr)
            raise
        finally:
            write_json(job / 'job.json', manifest)


def watch(args):
    folder = Path(args.directory).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if not args.prepare_only and not sys.stdin.isatty():
        raise ValueError('Run watch in a terminal for print confirmation, or use --prepare-only.')
    options = get_options(args)
    options.validate()
    seen_path = STATE / 'watch.json'
    with lock(STATE / 'watch.lock'):
        seen = read_json(seen_path, {})
        stable = {}
        ignored = set()
        print(f'Watching {folder}\nDrop an image here. Originals stay untouched. Ctrl-C stops the watcher.')
        first_pass = True
        while True:
            for source in sorted(folder.iterdir()):
                if not source.is_file() or source.name.startswith('.'):
                    continue
                if source.suffix.lower() not in EXTENSIONS:
                    if source.name not in ignored:
                        print(f'Ignoring unsupported file: {source.name}')
                        ignored.add(source.name)
                    continue
                try:
                    stat = source.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    previous = stable.get(source)
                    if previous is None or previous[:2] != signature:
                        stable[source] = (*signature, time.monotonic())
                        continue
                    if time.monotonic() - previous[2] < 2:
                        continue
                    data = source.read_bytes()
                    after = source.stat()
                    if signature != (after.st_size, after.st_mtime_ns):
                        continue
                    key = fingerprint(data, options)
                    if key in seen:
                        continue
                    try:
                        job = prepare(source, options)
                    except (ValueError, OSError) as exc:
                        print(f'Cannot prepare {source.name}: {exc}', file=sys.stderr)
                        seen[key] = {'source': str(source), 'error': str(exc), 'status': 'invalid'}
                        write_json(seen_path, seen)
                        continue
                    # Persist before any prompt/send: a restart must never reprint a job.
                    seen[key] = {'job': job.name, 'source': str(source), 'status': 'prepared'}
                    write_json(seen_path, seen)
                    if args.prepare_only:
                        describe(job)
                    else:
                        try:
                            seen[key]['status'] = print_job(job)
                        except (RuntimeError, ValueError, OSError) as exc:
                            seen[key]['status'] = 'needs-review'
                            print(f'Print error: {exc}', file=sys.stderr)
                        write_json(seen_path, seen)
                except FileNotFoundError:
                    continue  # Renamed/removed while scanning.
            if args.once and not first_pass:
                break
            first_pass = False
            time.sleep(2.1)


def add_layout_options(parser):
    parser.add_argument('--width-mm', type=float, default=65, help='Artwork width, excluding border (default: 65).')
    parser.add_argument('--border-mm', type=float, default=1.5, help='White border outside silhouette (default: 1.5).')
    parser.add_argument('--repeat', type=int, default=1, help='Stickers on ONE sheet; distinct from sheet copies.')
    parser.add_argument('--background', choices=['auto', 'keep', 'white'], default='auto')
    parser.add_argument('--knife-pressure', type=int, default=42, help='Upstream kiss-cut default: 42; calibrate on your media.')


def parser():
    p = argparse.ArgumentParser(prog='pixcut', description='Mac PixCut S1 CLI: prepare, inspect, confirm, print and cut over USB.')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Check dependencies, folders, and USB discovery (no printing).')
    sub.add_parser('scan', help='List USB devices.')
    status = sub.add_parser('status', help='Read live device status, or share an active print connection.')
    status.add_argument('--props', nargs='+', help='Additional/custom read-only property names.')
    sub.add_parser('jobs', help='List jobs on the printer.')
    sub.add_parser('pause', help='Experimental pause request; support is unverified on retail firmware.')
    sub.add_parser('resume', help='Send the firmware resume request.')
    cancel = sub.add_parser('cancel', help='Cancel a device job; omit ID for this CLI’s active print.')
    cancel.add_argument('job_id', nargs='?', type=int)
    prep = sub.add_parser('prepare', help='Create print JPEG, cut PLT/SVG, preview, and manifest offline.')
    prep.add_argument('image')
    add_layout_options(prep)
    printing = sub.add_parser('print', help='Confirm and print an existing prepared job.')
    printing.add_argument('job', help='Job ID or job directory.')
    printing.add_argument('--copies', type=int, help='Number of sheet copies (still requires confirmation).')
    printing.add_argument('--dry-run', action='store_true')
    preview = sub.add_parser('preview', help='Show a prepared job’s metadata and preview path.')
    preview.add_argument('job')
    preview.add_argument('--terminal', action='store_true', help='Render an approximate true-color preview in the terminal.')
    sub.add_parser('history', help='List prepared jobs and durable print attempt history.')
    watching = sub.add_parser('watch', help='Watch create/ and prompt before every print.')
    watching.add_argument('directory', nargs='?', default=str(ROOT / 'create'))
    watching.add_argument('--once', action='store_true', help='Process one stable scan, then exit.')
    watching.add_argument('--prepare-only', action='store_true', help='Prepare without prompting or printing.')
    add_layout_options(watching)
    convert = sub.add_parser('convert', help='Convert a custom 4×7 SVG cutline to PLT, offline.')
    convert.add_argument('svg')
    convert.add_argument('--out', required=True)
    convert.add_argument('--knife-pressure', type=int, default=42)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        command = args.command
        if command in ('doctor', 'scan'):
            devices = device.usb_devices()
            if command == 'doctor':
                import usb.backend.libusb1
                print(f'Python: {sys.version.split()[0]}\nWorkspace: {ROOT}\nlibusb: {bool(usb.backend.libusb1.get_backend())}')
                print(f'Inbox: {ROOT / "create"}\nImage preparation: available\nTransport: USB; Bluetooth is experimental upstream; Wi-Fi is not supported.')
            print(json.dumps(devices, indent=2))
            if not any(d['pixcut'] for d in devices):
                print('No PixCut detected. Connect the powered printer with a USB data cable.', file=sys.stderr)
                return 2
        elif command in ('status', 'jobs', 'pause', 'resume', 'cancel'):
            if command == 'pause':
                print('Pause support is unverified in the firmware; use cancel if the request is rejected.')
            print(json.dumps(device.control(command, getattr(args, 'job_id', None), getattr(args, 'props', None)), indent=2))
        elif command == 'prepare':
            describe(prepare(args.image, get_options(args)))
        elif command == 'print':
            print_job(resolve_job(args.job), args.copies, args.dry_run)
        elif command == 'preview':
            job = resolve_job(args.job)
            describe(job)
            if args.terminal:
                terminal_preview(job)
        elif command == 'watch':
            watch(args)
        elif command == 'history':
            for path in sorted(JOBS.glob('*/job.json')):
                m = read_json(path)
                status = m['status']
                if status == 'in-progress':
                    status = 'in-progress / inspect before retrying'
                print(f"{m['id']}  {status}  {Path(m['source']).name}")
                for attempt in m['attempts']:
                    print(f"  {attempt['started']} | {attempt['copies']} sheet(s) | {attempt['phase']} | printer job {attempt.get('printer_job_id', '?')}")
                    if attempt.get('error'):
                        print(f"    {attempt['error']}")
        elif command == 'convert':
            from pixcut.svg_to_plt import convert_svg_to_plt
            Options(knife_pressure=args.knife_pressure).validate()
            output = Path(args.out).expanduser()
            with output.open('x') as file:
                file.write(convert_svg_to_plt(Path(args.svg), knife_pressure=args.knife_pressure))
            print(output.resolve())
        return 0
    except KeyboardInterrupt:
        print('\nStopped. Any submitted job needs inspection before retrying.', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, EOFError, json.JSONDecodeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
