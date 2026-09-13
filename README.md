# Green Building Lava Lamp

A lava-lamp-like simulation for the Green Building's 17×9 RGB window display.
Six colorful blobs wander, deflect each other, and blend as their colors shift.

## Run

Requires Python 3.10+ and an existing simulator instance. No packages to install.

```sh
git clone https://github.com/Rhoahndur/green-building-lava-lamp.git
cd green-building-lava-lamp
python3 lava_lamp.py crisp-owl
```

Open https://sundai.willsarg.com/crisp-owl?view=close. Replace `crisp-owl` with
your assigned instance on another setup. An instance must already exist;
GitHub sign-in alone does not create one.

Runs continuously from your machine at up to 20 FPS; press Ctrl+C to stop.
Keep the machine awake and connected. Only run one sender per instance.
Any previously uploaded simulator clip resumes when live frames stop.

## Options

- `--fps 30`: change target frame rate (network latency may lower it).
- `--seed 140`: reproduce the same motion.
- `--frames 200`: stop after 200 sent frames.
- `--base-url http://localhost:8787`: use another simulator host.
- `--dry-run --frames 600`: validate locally without sending anything.

The client sends 459-byte RGB frames directly to the simulator's HTTP API.
This repository is self-contained; neither the Tetris repo nor a simulator
checkout is required. Physical-building radio integration is not included.

Tests: `python3 -m unittest discover -s tests`.
