#!/usr/bin/env python3
"""Optional one-time Daytona setup. Creates a billable snapshot in your account.

Requires: pip install daytona; DAYTONA_API_KEY in environment.
SDK reference: https://www.daytona.io/docs/en/declarative-builder/
This setup has not been cloud-tested in the supplied build environment.
"""
import os
import argparse
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description='Create a private Daytona snapshot; consumes account resources.')
    parser.add_argument('--kind',choices=['browser','codeaf'],default='browser')
    args=parser.parse_args()
    if not os.environ.get('DAYTONA_API_KEY'):
        raise SystemExit('DAYTONA_API_KEY is missing. Configure it locally; do not paste it into chat.')
    try:
        from daytona import Daytona, Image, CreateSnapshotParams
    except ImportError:
        raise SystemExit('Install the optional setup dependency: python -m pip install daytona')
    env='DAYTONA_BROWSER_SNAPSHOT' if args.kind=='browser' else 'DAYTONA_CODEAF_SNAPSHOT'
    name=os.environ.get(env) or ('pokescout-browser-v02' if args.kind=='browser' else 'pokescout-codeaf-v082')
    dockerfile=str(Path(__file__).resolve().with_name('Dockerfile.browser' if args.kind=='browser' else 'Dockerfile'))
    Daytona().snapshot.create(CreateSnapshotParams(name=name,image=Image.from_dockerfile(dockerfile)),on_logs=print)
    print('Created snapshot: '+name)
    print('Set '+env+' to this snapshot name. Browser snapshots are used by browser-demo --daytona; CodeAF is legacy development only.')

if __name__=='__main__':main()
