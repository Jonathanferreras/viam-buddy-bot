# Viam shell game

Python client for the specific UF850 arm, gripper, and wrist RealSense setup used in this project. It reveals the starting cup by 20 mm for two seconds, performs randomized left–middle / middle–right swaps, and records the intended ball location privately in each run log.

## Install and run

Requires Python 3.11+, the existing Viam machine configuration, `arm`, `gripper`, `cam`, and the `builtin` motion service. The native stereo point cloud must align with the 1280×720 color image. Distances, camera/gripper transforms, table geometry, and historical placement compensation are specific to this setup. Do not use unchanged on another robot or after moving the camera mount.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt
cp .env.example robot/.env
```

Fill in `robot/.env` with your machine address, API key ID, and API key from Viam. Credentials and captured images are ignored by Git. The local copy prepared by Codex already has an ignored credential file; a clone does not.

Arrange three cups in an even row and place the ball under the specified starting cup. Clear the workspace. Only one controller should operate the robot.

```sh
# Read-only route preview: arm must already be at the overview pose.
python run.py --ball-slot middle --swaps 3

# Physical round: first opens the empty gripper and moves to the overview,
# captures a new scan, previews the complete sequence, then executes it.
python run.py --execute --ball-slot middle --swaps 3
```

`--ball-slot` is an explicit physical starting location, not a guess. After a round, the ball identity is recorded in `robot/runs/<run>/execution.json` under `private_ball_final_slot`. Inspect it after guessing. This is symbolic tracking, not visual confirmation that the ball stayed underneath. Do not reuse that assumption after an interruption, a missed grip, or moving cups manually.

Ctrl+C interrupts the child process and requests an arm stop. Do not restart a round while holding a cup; inspect the state first. Each invocation has a unique directory and a process lock. No automatic restart/replay is configured.

## Current settings

- Arm: 180 degrees/s, 300 degrees/s²; driver caps speed at 180.
- Gripper: speed 500 raw driver units, torque 10, stall wait 0.2 s.
- Release position: raw 680, estimated 80 mm from the user's 100 mm full-opening measurement. This linear scale is approximate and has not been calibrated across the stroke.
- Sliding height: world Z100 mm; retreat Z130 mm (30 mm).
- Reveal: world Z120 mm (20 mm lift), at reduced speed, two-second pause.

Edit `robot/shuffle-speed-setting.json` and `robot/fast-shuffle-tuning.json`. The active code enforces its trial bounds. Current rounds use cached, freshly collision-previewed joint trajectories, check their starting/ending joint states, and stop on grip-state loss. Cached execution does not continuously replan if someone moves an obstacle after capture.

## What has been tested

The original per-run scripts physically completed the pair-swap routines, cached trajectories, partial-opening trials, and reveal lifts. The consolidated launcher is new: import/compile checks and offline simulation tests have passed, but it has not yet executed a physical round. The ideal simulator does not model ball rolling, cup deformation, or friction.

`archive/` preserves the development scripts, including unfinished/failed calibration and mixed-pattern experiments. Those files have fixed historical paths and should not be used as entry points. Three-cup rotations are experimental and are not enabled in the main runner: their routes did not pass for the last layout. The active implementation requires a successful full-route preview and refuses failures instead of bypassing collision geometry.

## Run with Viam

The easiest deployment is this Python client on your laptop or the computer running Viam. Viam exposes the configured resources; this script connects through the Python SDK. You do not paste this whole project into a component's JSON configuration.

On the machine computer, clone the repository, install the environment and credentials, then run the same command manually. Verify that the arm driver supports `grab_with_torque`, runtime speed/acceleration commands, `MoveThroughJointPositions`, and the motion service's plan-only command used here.

For a Viam-app button/control, the next deployment step is wrapping the runner as a custom generic service module with explicit start/stop/status commands. This repository is a client application, not a deployable Viam module yet. Avoid a startup process that automatically launches physical shuffling whenever Viam restarts.

Official references: [Python SDK](https://python.viam.dev/), [build and deploy modules](https://docs.viam.com/build-modules/overview/), [monitor and operate](https://docs.viam.com/monitor/overview/).

## Offline simulation

```sh
python -m unittest discover -s simulation -p 'test_*.py'
```

The Go simulation source is preserved in `simulation/`; its RDK/NLopt build requires additional native dependencies described there. Compiled binaries and the large build cache are not committed.
