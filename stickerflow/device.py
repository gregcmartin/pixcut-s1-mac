"""USB control and a local socket that shares the active session safely."""
import json
import logging
import os
import socket
import socketserver
import threading
from contextlib import contextmanager

from pixcut.logging_utils import SessionLogger
from pixcut.orchestrator import JobConfig, PixcutClient, _extract_alert_codes
from pixcut.transport import USBConfig, USBTransport, list_all_devices

from .storage import STATE, lock

SOCKET = STATE / 'printer.sock'
PROPS = ['printer-state', 'printer-sub-state', 'printer-state-alerts', 'media-size']


def usb_devices():
    return [{'vid': f'{v:04x}', 'pid': f'{p:04x}', 'description': d, 'pixcut': match}
            for v, p, d, match in list_all_devices()]


def make_client(log_dir=None, copies=1):
    devices = [d for d in usb_devices() if (d['vid'], d['pid']) == ('302c', '3101')]
    if not devices:
        raise RuntimeError('PixCut S1 not found over USB (302c:3101). Power it on and connect a USB data cable; close the Liene app.')
    if len(devices) > 1:
        raise RuntimeError('More than one PixCut is connected. Connect only the intended printer.')
    cfg = USBConfig(0x302c, 0x3101, interface=2, out_ep=0x06, in_ep=0x86,
                    data_interface=3, data_out_ep=0x04, data_in_ep=0x84)
    job_cfg = JobConfig(copies=copies, max_poll_s=max(900, copies * 300))
    return PixcutClient(USBTransport(cfg), SessionLogger(log_dir, keep_json=log_dir is not None), job_cfg=job_cfg)


def checked_response(response):
    if not isinstance(response, dict) or 'result' not in response or response.get('error'):
        raise RuntimeError(f'Printer did not acknowledge the request: {response}')
    def check(value):
        if isinstance(value, dict):
            if 'error-code' in value and str(value['error-code']) != '0':
                raise RuntimeError(f'Printer rejected the request: {response}')
            for v in value.values():
                check(v)
        elif isinstance(value, list):
            for v in value:
                check(v)
    check(response['result'])
    return response


def execute(client, action, job_id=None, props=None):
    if action == 'status':
        command = {'method': 'get-prop', 'params': props or PROPS}
    elif action == 'jobs':
        command = {'method': 'get-job-id-list', 'params': {}}
    elif action in ('pause', 'resume'):
        command = {'method': f'{action}-printer', 'params': {}}
    elif action == 'cancel':
        if job_id is None:
            raise ValueError('No job ID is available yet. Use Ctrl-C in the printing terminal.')
        command = {'method': 'cancel-job', 'params': {'job-id': int(job_id)}}
    else:
        raise ValueError('Unsupported control action.')
    return checked_response(client.send_command(command))


def check_ready(client, *, confirmed_media=None):
    response = execute(client, 'status')
    values = response['result']
    if not isinstance(values, list) or len(values) < 4:
        raise RuntimeError(f'Cannot verify printer readiness: {response}')
    state, sub, alerts, media = values[:4]
    if str(state) not in ('20', '30') or _extract_alert_codes(alerts):
        raise RuntimeError(f'Printer is not ready: state={state}, sub-state={sub}, alerts={alerts}. Run ./pixcut status.')
    if isinstance(media, dict):
        media = media.get('media-size')
    # 5009/3x3 is also observed on S1s being used with 4x7 media. It is not
    # reliable evidence of the loaded stock. Preserve the reading and require
    # an explicit operator declaration; do not rewrite printer properties.
    if str(media) == '5009' and confirmed_media == 'white-sticker-4x7':
        logging.getLogger(__name__).warning(
            'Printer reports ambiguous media code 5009; using operator-confirmed white 4×7 sticker stock.'
        )
        return {**response, 'host-media-confirmation': confirmed_media}
    if str(media) != '5013':
        raise RuntimeError(f'Cannot verify 4×7 sticker stock: printer reports {media!r}. Code 5009 requires explicit white 4×7 stock confirmation; other mismatches remain blocked.')
    return response


@contextmanager
def control_server(client, active):
    STATE.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(15)
            try:
                req = json.loads(self.rfile.readline(8192))
                result = execute(client, req['action'], req.get('job_id') or active.get('job_id'), req.get('props'))
                value = {'ok': True, 'response': result}
            except Exception as exc:
                value = {'ok': False, 'error': str(exc)}
            self.wfile.write(json.dumps(value).encode() + b'\n')

    with socketserver.UnixStreamServer(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(timeout=20)
            SOCKET.unlink(missing_ok=True)


def control(action, job_id=None, props=None):
    if SOCKET.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(30)
                connection.connect(str(SOCKET))
                connection.sendall(json.dumps({'action': action, 'job_id': job_id, 'props': props}).encode() + b'\n')
                with connection.makefile('rb') as stream:
                    response = json.loads(stream.readline(1048576))
            if not response['ok']:
                raise RuntimeError(response['error'])
            return response['response']
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # A stale socket is safe to bypass only if the USB lock is free.
    with lock(STATE / 'printer.lock'):
        client = make_client()
        try:
            client.open()
            return execute(client, action, job_id, props)
        finally:
            client.close()


def send(job_dir, copies, on_update, *, confirmed_media=None):
    """Persist assigned ID before uploading; never retry uncertain jobs."""
    with lock(STATE / 'printer.lock'):
        client = make_client(job_dir / 'usb-logs', copies)
        active = {}
        job_id = None
        try:
            client.open()
            readiness = check_ready(client, confirmed_media=confirmed_media)
            on_update({'phase': 'ready', 'preflight': readiness})
            client.start_heartbeat(5)
            with control_server(client, active):
                # Mark uncertainty BEFORE job creation: a lost reply may still mean acceptance.
                on_update({'phase': 'submitting'})
                job_id, payload = client.create_combo_job(job_dir / 'print.jpg', job_dir / 'cut.plt', client.job_cfg)
                active['job_id'] = job_id
                on_update({'phase': 'uploading', 'printer_job_id': job_id})
                client.upload_documents(payload, job_id=job_id)
                on_update({'phase': 'printing'})
                result = client.poll_job(job_id, client.job_cfg)
                if result.get('error'):
                    raise RuntimeError(f'Print/cut did not complete: {result}')
                return {'printer_job_id': job_id, **result}
        except KeyboardInterrupt:
            if job_id is not None:
                try:
                    on_update({'cancel_response': client.cancel_job(job_id)})
                except Exception as exc:
                    on_update({'cancel_error': str(exc)})
            raise
        finally:
            client.stop_heartbeat()
            client.close()
