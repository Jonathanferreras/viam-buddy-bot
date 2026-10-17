"""The voice/tap shell game, step by step (run by the buddy bot's voice router).

  play.py start [--swaps 3] [--ball-timeout 180]
      open the gripper and go to the overview pose, scan the cups, lift the middle cup and wait for
      the red ball, cover it, scan again, then shuffle (round.py, without its opening peek).
  play.py reveal --run robot/runs/<run> --slot left|middle|right
      lift the cup the player picked, then put it back.
  play.py putdown
      after a stop: lower a cup arm 18 is still holding straight down, let go, back to the overview.

Progress goes to stdout as "STAGE <id> <text>" lines; each script's full output is saved in the run
folder (<script>.log). Ends with "Run log: <folder>". Ctrl+C stops the arm.
"""
import argparse, asyncio, fcntl, json, os, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROBOT = ROOT / 'robot'
sys.path.insert(0, str(ROOT))
from run import prepare, stop   # noqa: E402  (the original launcher's overview move and arm stop)


def stage(sid, text):
    print(f'STAGE {sid} {text}', flush=True)


def shuffle_progress(total):
    moved = {'n': 0}

    def translate(line):
        if line == 'started':
            return 'STAGE shuffle Shuffling'
        if line == 'transfer_finished':
            moved['n'] += 1
            return f'STAGE shuffle Shuffling — move {moved["n"]} of {total}'
        if line == 'finished':
            return 'STAGE shuffle Done shuffling'
        return None
    return translate


def step(folder, script, env, translate=None):
    """Run one robot/ script, saving its output, passing its STAGE lines up; stop on failure."""
    last = ''
    with (folder / (script + '.log')).open('w') as log:
        p = subprocess.Popen([sys.executable, '-u', str(ROBOT / script)], cwd=str(ROBOT), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            log.write(line)
            log.flush()
            s = line.rstrip()
            if s.startswith('STAGE '):
                print(s, flush=True)
            elif translate:
                t = translate(s)
                if t:
                    print(t, flush=True)
            if s.strip():
                last = s.strip()
        rc = p.wait()
    if rc:
        why = last[len('STOPPED:'):].strip() if last.startswith('STOPPED:') else last
        raise SystemExit(f'Stopped: {why or script + " failed"}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('what', choices=('start', 'reveal', 'putdown'))
    ap.add_argument('--swaps', type=int, choices=range(2, 7), default=3)
    ap.add_argument('--ball-timeout', type=float, default=180)
    ap.add_argument('--run', help='reveal: the round folder')
    ap.add_argument('--slot', choices=('left', 'middle', 'right'), help='reveal: which cup')
    args = ap.parse_args()
    lock = open(ROBOT / '.run.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Stopped: another round is running')
    try:
        if args.what == 'start':
            start(args)
        elif args.what == 'reveal':
            reveal(args)
        else:
            putdown()
    except KeyboardInterrupt:
        asyncio.run(stop())
        raise SystemExit('Stopped: interrupted; arm stop requested')


def start(args):
    stage('ready', 'Getting ready')
    asyncio.run(prepare(True))
    name = 'runs/' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    folder = ROBOT / name
    folder.mkdir(parents=True)
    (folder / 'overview.json').write_text(json.dumps({'overview_completed': True}))
    env = dict(os.environ, SHUFFLE_RUN=name, SHUFFLE_EXECUTE='1', SHUFFLE_BALL_SLOT='middle',
               SHUFFLE_SWAPS=str(args.swaps), SHUFFLE_SKIP_REVEAL='1', BALL_TIMEOUT=str(args.ball_timeout))
    stage('scan', 'Finding the cups')
    step(folder, 'capture_stereo.py', env)
    step(folder, 'detect_cups_auto.py', env)
    step(folder, 'setup_ball.py', env)
    stage('shuffle', 'Checking where the cups are')
    step(folder, 'capture_stereo.py', env)
    step(folder, 'detect_cups_auto.py', env)
    stage('shuffle', 'Planning every move')
    step(folder, 'round.py', env, shuffle_progress(args.swaps * 3))
    print('Run log:', folder, flush=True)
    report = json.loads((folder / 'execution.json').read_text())
    if report.get('error') or not report.get('completed'):
        raise SystemExit('Stopped: ' + str(report.get('error') or 'the shuffle didn’t finish'))
    try:      # back to the overview pose so the camera shows every cup while the player guesses
        step(folder, 'look_at_cups.py', env)
    except SystemExit as e:
        print(f'note: couldn’t go back to the overview pose ({e}); guessing from here', flush=True)
    stage('guess', 'Tap the cup you think hides the ball')


def reveal(args):
    if not args.run or not args.slot:
        raise SystemExit('Stopped: reveal needs --run and --slot')
    folder = Path(args.run)
    folder = folder if folder.is_absolute() else ROOT / folder
    env = dict(os.environ, REVEAL_RUN=str(folder), REVEAL_SLOT=args.slot)
    step(folder, 'reveal_cup.py', env)
    print('Run log:', folder, flush=True)


def putdown():
    folder = ROBOT / ('runs/' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-putdown')
    folder.mkdir(parents=True, exist_ok=True)
    step(folder, 'putdown_cup.py', dict(os.environ))
    print('Run log:', folder, flush=True)


if __name__ == '__main__':
    main()
