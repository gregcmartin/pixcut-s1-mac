"""Deterministic artwork preparation and kiss-cut geometry; no device access."""
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pillow_heif import register_heif_opener
from scipy.ndimage import binary_fill_holes, binary_propagation
from shapely.geometry import Polygon
from shapely.ops import unary_union
from skimage.measure import find_contours

from pixcut.image_to_cut import add_printer_margins
from pixcut.svg_to_plt import points_to_plt, _pressure_switch_reset_commands

register_heif_opener()
DPI = 300
PX_MM = DPI / 25.4
SHEET = (1200, 2100)
EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif', '.tif', '.tiff', '.bmp'}
PIPELINE_VERSION = 3


@dataclass(frozen=True)
class Options:
    width_mm: float = 65.0
    border_mm: float = 0.0
    repeat: int | None = None  # None fills the sheet at the requested size.
    background: str = 'auto'
    knife_pressure: int = 42
    pressure_steps: tuple[int, ...] | None = None

    def validate(self):
        if not math.isfinite(self.width_mm) or not 5 <= self.width_mm <= 88:
            raise ValueError('Artwork width must be 5–88 mm.')
        if not math.isfinite(self.border_mm) or not 0 <= self.border_mm <= 5:
            raise ValueError('Border must be 0–5 mm.')
        if self.repeat is not None and not 1 <= self.repeat <= 100:
            raise ValueError('Stickers per sheet must be auto or 1–100.')
        if not 1 <= self.knife_pressure <= 100:
            raise ValueError('Knife pressure must be 1–100.')
        if self.pressure_steps is not None:
            if not 2 <= len(self.pressure_steps) <= 6 or any(not 1 <= p <= 50 for p in self.pressure_steps):
                raise ValueError('Pressure calibration requires 2–6 pressures, each from 1 to 50.')
            if self.repeat != len(self.pressure_steps):
                raise ValueError('Pressure calibration needs exactly one sticker per pressure value.')
        if self.background not in ('auto', 'keep', 'white'):
            raise ValueError('Background must be auto, keep, or white.')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(data, options):
    return digest(data + json.dumps({'pipeline': PIPELINE_VERSION, **asdict(options)}, sort_keys=True).encode())


def load_art(data, options):
    warnings = []
    with Image.open(io.BytesIO(data)) as src:
        if getattr(src, 'n_frames', 1) > 1:
            warnings.append('Only the first frame/page is used.')
        img = ImageOps.exif_transpose(src).convert('RGBA')
    if options.background == 'keep':
        base = Image.new('RGBA', img.size, 'white')
        img = Image.alpha_composite(base, img)
    elif options.background == 'white':
        arr = np.array(img)
        near_white = (arr[:, :, :3] > 240).all(axis=2) | (arr[:, :, 3] < 10)
        seed = np.zeros(near_white.shape, dtype=bool)
        seed[0, :] = near_white[0, :]
        seed[-1, :] = near_white[-1, :]
        seed[:, 0] = near_white[:, 0]
        seed[:, -1] = near_white[:, -1]
        exterior = binary_propagation(seed, mask=near_white)
        arr[exterior, 3] = 0
        img = Image.fromarray(arr)
        warnings.append('Edge-connected near-white background removed; inspect the cut preview.')
    alpha = img.getchannel('A')
    bbox = alpha.point(lambda p: 255 if p > 10 else 0).getbbox()
    if not bbox:
        raise ValueError('Image is empty or fully transparent.')
    if alpha.getextrema()[0] == 255 and options.background == 'auto':
        warnings.append('Opaque artwork: using its rectangular outline. Use --background white only for a plain white background.')
    img = img.crop(bbox)
    width = round(options.width_mm * PX_MM)
    height = max(1, round(img.height * width / img.width))
    if height + 2 * options.border_mm * PX_MM > SHEET[1] - 10 * PX_MM:
        raise ValueError('Artwork is too tall at this width. Reduce --width-mm.')
    if img.width < width:
        warnings.append(f'Artwork is being enlarged from {img.width}px to {width}px; edges may look soft.')
    return img.resize((width, height), Image.Resampling.LANCZOS), warnings


def render(data, options, destination):
    options.validate()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    img, warnings = load_art(data, options)
    mask = binary_fill_holes(np.array(img.getchannel('A')) > 10)
    contours = find_contours(np.pad(mask, 1).astype(float), 0.5)
    polygons = []
    for c in contours:
        p = Polygon([(float(x) - 1, float(y) - 1) for y, x in c]).buffer(0)
        if p.area >= 0.5 * PX_MM ** 2:
            polygons.append(p)
    if not polygons:
        raise ValueError('No cuttable silhouette found (minimum component area: 0.5 mm²).')
    shape = unary_union(polygons).buffer(options.border_mm * PX_MM, join_style=1).simplify(0.15 * PX_MM, preserve_topology=True)
    parts = [shape] if shape.geom_type == 'Polygon' else list(shape.geoms)
    # Cut outer silhouettes only: interior white details and transparent holes stay intact.
    paths = [list(p.exterior.coords) for p in parts if p.geom_type == 'Polygon']
    if len(paths) > 1:
        warnings.append(f'{len(paths)} separate cut outlines per sticker; disconnected artwork becomes separate pieces.')
    x0, y0, x1, y1 = shape.bounds
    cell_w, cell_h = math.ceil(x1 - x0), math.ceil(y1 - y0)
    edge, gap = math.ceil(5 * PX_MM), math.ceil(3 * PX_MM)
    cols = int((SHEET[0] - 2 * edge + gap) // (cell_w + gap))
    rows = int((SHEET[1] - 2 * edge + gap) // (cell_h + gap))
    capacity = cols * rows
    if capacity < 1:
        raise ValueError('Artwork plus border does not fit the safe sheet area. Reduce --width-mm or --border-mm.')
    count = capacity if options.repeat is None else options.repeat
    if count > capacity:
        raise ValueError(f'Only {capacity} sticker(s) fit at this size; reduce --repeat or --width-mm. Nothing was omitted.')
    cols = min(cols, count)
    sheet = Image.new('RGBA', SHEET, 'white')
    all_paths = []
    path_pressures = []
    pressure_samples = []
    for i in range(count):
        row, col = divmod(i, cols)
        row_count = min(cols, count - row * cols)
        start_x = (SHEET[0] - (row_count * cell_w + (row_count - 1) * gap)) / 2
        dx = round(start_x + col * (cell_w + gap) - x0)
        dy = round(edge + row * (cell_h + gap) - y0)
        sheet.alpha_composite(img, (dx, dy))
        all_paths.extend([[(x + dx, y + dy) for x, y in p] for p in paths])
        pressure = options.pressure_steps[i] if options.pressure_steps else options.knife_pressure
        path_pressures.extend([pressure] * len(paths))
        if options.pressure_steps:
            pressure_samples.append({'sticker': i + 1, 'knife_pressure': pressure,
                                     'label_px': [round(x0 + dx), round(y1 + dy + 4)]})
    for path in all_paths:
        if any(not (edge - 1 <= x <= SHEET[0] - edge + 1 and edge - 1 <= y <= SHEET[1] - edge + 1) for x, y in path):
            raise ValueError('Cut path exceeds the safe sheet area.')
    padded = add_printer_margins(sheet.convert('RGB'))
    for quality in (95, 92, 90, 85, 80, 75, 70, 60, 50):
        out = io.BytesIO()
        padded.save(out, 'JPEG', quality=quality, optimize=True, dpi=(DPI, DPI))
        if len(out.getvalue()) < 1024 * 1024:
            break
    else:
        raise ValueError('Could not encode the printer image below 1 MiB.')
    (destination / 'print.jpg').write_bytes(out.getvalue())
    # Firmware PLT coordinates: rotate -90°, 1016 units/inch, 40 units/mm.
    plt_paths = [[(7112 - y * 1016 / DPI, x * 1016 / DPI) for x, y in p] for p in all_paths]
    if options.pressure_steps:
        commands = ['IN', 'VER0.1.0']
        previous_pressure = None
        for path, pressure in zip(plt_paths, path_pressures):
            if pressure != previous_pressure:
                if previous_pressure is not None:
                    # Upstream's blade-up nudge/return ensures inline pressure
                    # transitions reseat the blade; 4 units is 0.1 mm.
                    commands.extend(_pressure_switch_reset_commands(path[0], nudge_u=4))
                commands.append(f'KP{pressure}')
                previous_pressure = pressure
            commands.append(f'U{round(path[0][0])},{round(path[0][1])}')
            commands.extend(f'D{round(x)},{round(y)}' for x, y in path)
        commands.append(' U6476,0 @ ')
        cut_plt = ' '.join(commands)
    else:
        cut_plt = points_to_plt(plt_paths, kp=options.knife_pressure)
    (destination / 'cut.plt').write_text(cut_plt)
    svg_paths = []
    for p in all_paths:
        points = ' '.join(f'{x:.3f},{y:.3f}' for x, y in p)
        svg_paths.append(f'<polygon points="{points}" fill="none" stroke="#e33456" stroke-width="2"/>')
    (destination / 'cut.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4in" height="7in" viewBox="0 0 1200 2100">' + ''.join(svg_paths) + '</svg>')
    preview = sheet.convert('RGB')
    draw = ImageDraw.Draw(preview)
    for path in all_paths:
        draw.line(path, fill='#e33456', width=3)
    for sample in pressure_samples:
        draw.text(tuple(sample['label_px']), f"Sticker {sample['sticker']}: KP{sample['knife_pressure']}",
                  fill='#222222', font=ImageFont.load_default(size=20))
    preview.save(destination / 'preview.png', dpi=(DPI, DPI))
    img.save(destination / 'artwork.png')
    return {
        'artwork_mm': [round(img.width / PX_MM, 2), round(img.height / PX_MM, 2)],
        'cut_bounds_mm': [round(cell_w / PX_MM, 2), round(cell_h / PX_MM, 2)],
        'cut_paths': len(all_paths), 'sheet_capacity': capacity,
        'stickers_per_sheet': count, 'layout_mode': 'auto-fill' if options.repeat is None else 'fixed',
        'pressure_samples': pressure_samples,
        'jpeg_bytes': len(out.getvalue()), 'jpeg_quality': quality,
        'warnings': warnings,
        'sha256': {name: digest((destination / name).read_bytes()) for name in ('print.jpg', 'cut.plt', 'preview.png')},
    }
