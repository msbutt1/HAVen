"""Build and serve simulated integration fixtures. Run from signals/."""
import argparse
import json
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from haven_signals.demo import run_demo, replay_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('out/team-mock'))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    result = run_demo(args.out, seed=1, run_id='team-mock-seed1')
    assert replay_demo(args.out)['matches_original']
    for name, records in [('telemetry', result['telemetry']), ('derived', result['derived'])]:
        assert all(r.get('provenance', {}).get('source') == 'simulated' for r in records)
        (args.out / (name + '.json')).write_text(json.dumps({
            'schema': 'haven.mock_bundle', 'version': 1,
            'source': 'simulated', 'hardware_verified': False,
            'recording_id': result['manifest']['recording_id'],
            'records': records}, allow_nan=False))
    print(f'SIMULATED fixtures ready: {args.out.resolve()}', flush=True)
    if args.build_only:
        return
    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(args.out)))
    print(f'SIMULATED mock HTTP: http://127.0.0.1:{args.port}/derived.json', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
