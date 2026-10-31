import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from stickerflow import cli, device
from stickerflow.layout import Options, load_art, render
from stickerflow.storage import read_json


def png(image):
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def sticker():
    image = Image.new('RGBA', (400, 400))
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 20, 380, 380), fill='#2244aa')
    draw.ellipse((160, 160, 240, 240), fill='white')
    return image


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.source = self.root / 'test.png'
        self.source.write_bytes(png(sticker()))
        self.patches = [patch.object(cli, 'JOBS', self.jobs), patch.object(cli, 'STATE', self.root / 'state')]
        for p in self.patches:
            p.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def prepare(self, **kw):
        return cli.prepare(self.source, Options(**kw))

    def test_print_raster_and_cut_alignment(self):
        job = self.prepare()
        m = read_json(job / 'job.json')
        with Image.open(job / 'print.jpg') as image:
            self.assertEqual(image.size, (1216, 2128))
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertLess(m['layout']['jpeg_bytes'], 1024 ** 2)
        coords = [(int(x), int(y)) for x, y in re.findall(r'[UD](\d+),(\d+)', (job / 'cut.plt').read_text())]
        self.assertTrue(all(0 <= x <= 7112 and 0 <= y <= 4064 for x, y in coords))
        self.assertEqual(m['layout']['cut_paths'], 2)
        self.assertAlmostEqual(m['layout']['artwork_mm'][0], 65, delta=0.1)
        self.assertAlmostEqual(m['layout']['cut_bounds_mm'][0], 65, delta=0.3)
        cli.validate_artifacts(job, m)

    def test_holes_do_not_make_inner_cuts(self):
        image = sticker()
        ImageDraw.Draw(image).ellipse((160, 160, 240, 240), fill=(0, 0, 0, 0))
        info = render(png(image), Options(), self.root / 'render')
        self.assertEqual(info['cut_paths'], info['stickers_per_sheet'])

    def test_white_removal_preserves_enclosed_white_detail(self):
        image = Image.new('RGB', (200, 200), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 180, 180), fill='black')
        draw.rectangle((70, 70, 130, 130), fill='white')
        result, _ = load_art(png(image), Options(background='white'))
        self.assertEqual(result.getpixel((result.width // 2, result.height // 2)), (255, 255, 255, 255))

    def test_palette_transparency(self):
        image = Image.new('P', (200, 200), 0)
        image.putpalette([255, 255, 255, 0, 0, 255] + [0] * 762)
        ImageDraw.Draw(image).ellipse((30, 30, 170, 170), fill=1)
        image.info['transparency'] = 0
        result, _ = load_art(png(image), Options())
        self.assertEqual(result.getpixel((0, 0))[3], 0)

    def test_opaque_default_does_not_remove_art(self):
        image = Image.new('RGB', (200, 200), 'white')
        result, warnings = load_art(png(image), Options())
        self.assertEqual(result.getchannel('A').getextrema(), (255, 255))
        self.assertTrue(any('Opaque' in w for w in warnings))

    def test_empty_image_rejected(self):
        self.source.write_bytes(png(Image.new('RGBA', (100, 100))))
        with self.assertRaisesRegex(ValueError, 'transparent'):
            self.prepare()
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_overflow_rejected_without_silently_dropping_stickers(self):
        with self.assertRaisesRegex(ValueError, 'Nothing was omitted'):
            self.prepare(width_mm=65, repeat=100)

    def test_repeat_geometry(self):
        job = self.prepare(width_mm=30, repeat=6)
        self.assertEqual(read_json(job / 'job.json')['layout']['cut_paths'], 6)

    def test_auto_fill_three_landscape_stickers_without_resizing(self):
        self.source.write_bytes(png(Image.new('RGB', (1450, 1085), '#cc2020')))
        job = self.prepare()
        manifest = read_json(job / 'job.json')
        layout = manifest['layout']
        self.assertEqual(layout['stickers_per_sheet'], 3)
        self.assertEqual(layout['sheet_capacity'], 3)
        self.assertEqual(layout['cut_paths'], 3)
        self.assertAlmostEqual(layout['artwork_mm'][0], 65, delta=0.1)
        self.assertAlmostEqual(layout['artwork_mm'][1], 48.68, delta=0.1)
        self.assertEqual(manifest['options']['border_mm'], 0)
        # Three distinct, nonoverlapping cut regions with the expected gap.
        from xml.etree import ElementTree
        root = ElementTree.fromstring((job / 'cut.svg').read_text())
        bounds = []
        for polygon in root:
            points = [tuple(map(float, pair.split(','))) for pair in polygon.attrib['points'].split()]
            bounds.append((min(y for x, y in points), max(y for x, y in points)))
        bounds.sort()
        self.assertTrue(all(bounds[i + 1][0] - bounds[i][1] >= 35 for i in range(2)))
        self.assertGreaterEqual(bounds[0][0], 59)
        self.assertLessEqual(bounds[-1][1], 2041)
        cli.validate_artifacts(job, manifest)

    def test_explicit_single_sticker_remains_available(self):
        job = self.prepare(repeat=1)
        manifest = read_json(job / 'job.json')
        self.assertEqual(cli.stickers_per_sheet(manifest), 1)
        self.assertEqual(manifest['layout']['cut_paths'], 1)
        self.assertGreater(manifest['layout']['sheet_capacity'], 1)

    def test_legacy_job_count_is_not_replaced_by_capacity(self):
        manifest = {'options': {'repeat': 1}, 'layout': {'sheet_capacity': 3}}
        self.assertEqual(cli.stickers_per_sheet(manifest), 1)

    def test_copy_confirmation_reports_total_stickers(self):
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='PRINT 2'), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.confirm_copies(2, per_sheet=3), 2)
        self.assertIn('2 sheet(s) × 3 sticker(s) per sheet = 6 stickers', output.getvalue())

    def test_cli_defaults_to_auto_fill(self):
        for command in (['prepare', 'art.png'], ['watch']):
            self.assertIsNone(cli.get_options(cli.parser().parse_args(command)).repeat)
        self.assertIsNone(cli.repeat_value('auto'))
        self.assertEqual(cli.repeat_value('1'), 1)

    def test_auto_fill_rejects_oversized_border(self):
        with self.assertRaisesRegex(ValueError, 'safe sheet area'):
            self.prepare(width_mm=88, border_mm=5)

    def test_pressure_calibration_preserves_art_and_cut_geometry(self):
        self.source.write_bytes(png(Image.new('RGB', (1450, 1085), '#cc2020')))
        baseline = self.prepare(repeat=3)
        calibration = self.prepare(repeat=3, pressure_steps=(42, 44, 46))
        self.assertEqual((baseline / 'print.jpg').read_bytes(), (calibration / 'print.jpg').read_bytes())
        self.assertEqual((baseline / 'cut.svg').read_bytes(), (calibration / 'cut.svg').read_bytes())
        commands = (calibration / 'cut.plt').read_text().split()
        self.assertEqual([c for c in commands if c.startswith('KP')], ['KP42', 'KP44', 'KP46'])
        baseline_draws = re.findall(r'D\d+,\d+', (baseline / 'cut.plt').read_text())
        self.assertEqual([c for c in commands if c.startswith('D')], baseline_draws)
        for pressure in ('KP44', 'KP46'):
            i = commands.index(pressure)
            self.assertTrue(all(c.startswith('U') for c in commands[i-3:i]))
            self.assertEqual(commands[i-3], commands[i-1])
            self.assertEqual(commands[i-1], commands[i+1])
            start = tuple(map(int, commands[i-3][1:].split(',')))
            nudge = tuple(map(int, commands[i-2][1:].split(',')))
            self.assertEqual(nudge, (start[0] + 4, start[1]))
        manifest = read_json(calibration / 'job.json')
        self.assertEqual([s['knife_pressure'] for s in manifest['layout']['pressure_samples']], [42, 44, 46])
        cli.validate_artifacts(calibration, manifest)
        self.assertEqual(Options().knife_pressure, 42)

    def test_pressure_calibration_rejects_invalid_profiles(self):
        for options in (Options(repeat=3, pressure_steps=(42, 44)),
                        Options(repeat=2, pressure_steps=(42, 51)),
                        Options(repeat=2, pressure_steps=(0, 44)),
                        Options(repeat=1, pressure_steps=(44,))):
            with self.assertRaises(ValueError):
                options.validate()

    def test_invalid_numeric_options(self):
        for options in (Options(width_mm=float('nan')), Options(border_mm=-1), Options(repeat=0), Options(knife_pressure=101)):
            with self.assertRaises(ValueError):
                options.validate()

    def test_dry_run_never_accesses_device(self):
        job = self.prepare()
        with patch.object(device, 'send') as send:
            self.assertEqual(cli.print_job(job, dry_run=True), 'dry-run')
            send.assert_not_called()

    def test_noninteractive_print_is_rejected(self):
        job = self.prepare()
        with patch('sys.stdin.isatty', return_value=False), patch.object(device, 'send') as send:
            with self.assertRaisesRegex(ValueError, 'interactive'):
                cli.print_job(job, copies=1)
            send.assert_not_called()

    def test_decline_never_submits(self):
        job = self.prepare()
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value=''), patch.object(device, 'send') as send:
            self.assertEqual(cli.print_job(job, copies=1), 'skipped')
            send.assert_not_called()

    def test_changed_artifacts_cannot_print(self):
        job = self.prepare()
        (job / 'cut.plt').write_text('different geometry')
        with patch.object(device, 'send') as send:
            with self.assertRaisesRegex(ValueError, 'changed'):
                cli.print_job(job, copies=1)
            send.assert_not_called()

    def test_uncertain_submission_is_recorded(self):
        job = self.prepare()
        def fail(path, copies, update, **kwargs):
            update({'phase': 'submitting'})
            raise TimeoutError('Lost response after submission')
        with patch.object(cli, 'confirm_copies', return_value=2), patch.object(device, 'send', side_effect=fail):
            with self.assertRaises(TimeoutError):
                cli.print_job(job)
        manifest = read_json(job / 'job.json')
        self.assertEqual(manifest['status'], 'needs-review')
        self.assertEqual(len(manifest['attempts']), 1)

    def test_confirmed_completion_is_recorded(self):
        job = self.prepare()
        with patch.object(cli, 'confirm_copies', return_value=1), patch.object(device, 'send', return_value={'printer_job_id': 12}):
            self.assertEqual(cli.print_job(job), 'completed')
        self.assertEqual(read_json(job / 'job.json')['status'], 'completed')

    def test_watcher_deduplicates_across_restarts_and_renames(self):
        inbox = self.root / 'create'
        inbox.mkdir()
        (inbox / 'one.png').write_bytes(self.source.read_bytes())
        args = cli.parser().parse_args(['watch', str(inbox), '--once', '--prepare-only'])
        cli.watch(args)
        (inbox / 'one.png').rename(inbox / 'renamed.png')
        cli.watch(args)
        self.assertEqual(len(list(self.jobs.glob('*/job.json'))), 1)

    def test_protocol_error_does_not_report_success(self):
        for response in ({'error': {'code': -1}}, {'result': [{'error-code': 42}]}, {'method': 'event.rpt_err'}):
            with self.assertRaises(RuntimeError):
                device.checked_response(response)

    def test_unknown_or_wrong_media_is_not_submitted(self):
        client = Mock()
        for values in (['20', '2000', '::0', {}], ['20', '2000', '::0', {'media-size': 5012}], ['40', '3030', '::0', {'media-size': 5013}]):
            client.send_command.return_value = {'result': values}
            with self.assertRaises(RuntimeError):
                device.check_ready(client)
        client.send_command.return_value = {'result': ['20', '2000', '::0', {'media-size': 5013}]}
        self.assertIn('result', device.check_ready(client))

    def test_ambiguous_media_requires_explicit_confirmation(self):
        client = Mock()
        client.send_command.return_value = {'result': ['20', '2000', '::0', {'media-size': 5009}]}
        with self.assertRaises(RuntimeError):
            device.check_ready(client)
        response = device.check_ready(client, confirmed_media='white-sticker-4x7')
        self.assertEqual(response['result'][3]['media-size'], 5009)
        self.assertEqual(response['host-media-confirmation'], 'white-sticker-4x7')
        for values in (['40', '3030', '::0', {'media-size': 5009}], ['60', '3015', '::5401', {'media-size': 5009}], ['20', '2000', '::0', {'media-size': 5012}], ['20', '2000', '::0', {}]):
            client.send_command.return_value = {'result': values}
            with self.assertRaises(RuntimeError):
                device.check_ready(client, confirmed_media='white-sticker-4x7')

    def test_optional_border_is_still_supported(self):
        job = self.prepare(border_mm=1.5)
        self.assertAlmostEqual(read_json(job / 'job.json')['layout']['cut_bounds_mm'][0], 68, delta=0.3)

    def test_session_tracks_id_before_upload_and_closes(self):
        client = Mock()
        client.job_cfg = object()
        client.create_combo_job.return_value = (42, {'jpg': b'jpeg', 'plt': b'cut'})
        client.poll_job.return_value = {'last_info': {'job-state': 9}}
        updates = []
        def uploaded(*args, **kwargs):
            self.assertEqual(updates[-1]['printer_job_id'], 42)
        client.upload_documents.side_effect = uploaded
        with patch.object(device, 'STATE', self.root / 'device'), patch.object(device, 'make_client', return_value=client), patch.object(device, 'check_ready', return_value={'ready': True}), patch.object(device, 'control_server', return_value=contextlib.nullcontext()):
            result = device.send(self.root, 2, updates.append)
        self.assertEqual(result['printer_job_id'], 42)
        self.assertEqual([v['phase'] for v in updates], ['ready', 'submitting', 'uploading', 'printing'])
        client.close.assert_called_once()
        client.stop_heartbeat.assert_called_once()

    def test_session_cancels_when_interrupted_during_upload(self):
        client = Mock()
        client.create_combo_job.return_value = (43, {})
        client.upload_documents.side_effect = KeyboardInterrupt()
        with patch.object(device, 'STATE', self.root / 'device'), patch.object(device, 'make_client', return_value=client), patch.object(device, 'check_ready'), patch.object(device, 'control_server', return_value=contextlib.nullcontext()):
            with self.assertRaises(KeyboardInterrupt):
                device.send(self.root, 1, Mock())
        client.cancel_job.assert_called_once_with(43)
        client.close.assert_called_once()

    def test_local_controls_share_active_connection(self):
        client = Mock()
        client.send_command.return_value = {'result': [{'error-code': 0}]}
        state = self.root / 'ipc'
        with patch.object(device, 'STATE', state), patch.object(device, 'SOCKET', state / 'printer.sock'), patch.object(device, 'make_client') as make:
            with device.control_server(client, {'job_id': 44}):
                result = device.control('cancel')
                self.assertIn('result', result)
                make.assert_not_called()
        client.send_command.assert_called_once_with({'method': 'cancel-job', 'params': {'job-id': 44}})
        self.assertFalse((state / 'printer.sock').exists())


if __name__ == '__main__':
    unittest.main()
