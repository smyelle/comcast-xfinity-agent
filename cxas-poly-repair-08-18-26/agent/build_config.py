"""The build's switches, resolved once from the command line and then frozen.

WHY THIS EXISTS. Every one of these switches used to be an environment variable read at
the point it took effect -- `SPIKE_DEMO`, `SPIKE_FAKE_LEGS`, `SPIKE_SLOW_LEGS`,
`SPIKE_LOCAL_SPECIALISTS`, `SPIKE_LOCAL_DIAG`, `DEMO_SWEEP_SECONDS`, `SCRUB_SECRETS`.
Twelve reads across four modules, none of them in `--help`, and composing them correctly
was folklore. `--demo` on its own rewrote the sweep and nothing else: the account ->
scenario bindings, the fixture bake and the faked sweep legs all hung off `SPIKE_DEMO`,
so `--demo` produced a build that recognised a test account and then routed it to
whatever the live hub returned. It stayed broken for months because the mode had no test
and the switch was invisible.

Setting the variable from `main()` is NOT a fix, and this file exists because that was
tried. `build.py` imports `app`, and importing `app` constructs the whole `App` --
including every tool body `source_tools` emits. Those bodies are what read the switch, so
by the time `main()` parses its arguments the decision has already been made. Measured at
the commit that added the flag: `--demo` alone baked no `DEMO_ACCOUNTS` at all and
`tests/demo_check.py` scored 3/8, while the same build with the environment variable set
scored 8/8.

So the config is resolved BEFORE the emitters are imported and handed to them as one
frozen object. `current()` is how a module-level emitter reaches it; `activate()` refuses
to run once anything has read it, which turns that ordering trap into a build error
instead of a demo that lies.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import typing

#: Where the specialist pair runs. Cloud Run is the real deployment and the default; the
#: in-sandbox path is the fallback for a demo with no Cloud Run, Firestore or IAM.
SPECIALISTS = ("cloudrun", "local")

#: Whether the lowered sweep legs answer from their recorded fixtures or call out.
LEGS = ("live", "fake")

#: What the emitted engine tool ACTUALLY is, recorded alongside the flags. `--pack-engine`
#: is a request, and a request is not an outcome: packing needs a python 3.12 interpreter
#: (what CES runs) and this repo's own venv is 3.13, so a build host that cannot find one
#: emits source. Three states rather than a boolean, because "off" and "asked for and not
#: done" are different facts and only one of them is a surprise.
PACKED, PACK_SKIPPED, PACK_OFF = "packed", "skipped", "off"
PACKING_STATES = (PACKED, PACK_SKIPPED, PACK_OFF)

#: The manifest key the outcome above is stamped under. Deliberately NOT a `BuildConfig`
#: field: every other key in the manifest is an input, and this one is a result.
PACKING_KEY = "engine_packing"

#: Written into every emitted app dir. A built app that cannot say where its specialists
#: run or whether its legs are faked is a build that can lie about it.
MANIFEST_NAME = "build_manifest.json"

#: The session variable the specialist caller reads its own app id out of, and the name
#: `--ces-app` bakes a default into. A tool body cannot ask CES which app is running it —
#: `context` carries `variables` and `state` and nothing else — so the id has to be PUT
#: where the body can see it.
CES_APP_VARIABLE = "ces_app"

#: What a CES app id looks like. Checked because the failure it prevents is silent and
#: WRONG rather than loud: the proxy only falls back when the app is EMPTY, so a mistyped
#: one is used as sent, both specialist sessions fail to open, and the derivation reads
#: two empty legs as a healthy line — an all-clear the caller is then told about.
_APP_RESOURCE = re.compile(r"projects/[^/]+/locations/[^/]+/apps/[^/]+\Z")


@dataclasses.dataclass(frozen=True)
class BuildConfig:
  """Every switch that changes what the build emits, resolved and frozen.

  Frozen because the emitters run at import time and at emit time, minutes apart in
  wall-clock terms and dozens of frames apart in the call stack. A mutable config would
  let a later stage disagree with an earlier one, which is the exact shape of the failure
  this replaces.
  """

  #: Demo composition: account -> scenario bindings baked into the account gate, every
  #: carried tool answering from its recorded fixture, and the diagnostic fan-out resolved
  #: from `mock_config_string` instead of the live backends.
  demo: bool = False
  #: Make the multi-level intent router the root flow. OFF by default: the build is then
  #: the flat single-flow `repair` agent (the pre-router shape -- `repair` owns the opening
  #: turn and handles reboot-on-request and a human hand-off through its own rung and
  #: escalate rail). On, the router owns routing and the opening greeting.
  steering: bool = False
  #: Bake the opening greeting OFF: the account ask is emitted in its greeting-free form
  #: ("To get started, ...") with no `{welcome_lead}`, so even a DIRECT call to this build
  #: never greets. For a build deployed purely as a transfer target, where the upstream
  #: agent has already welcomed the caller and you do not want to depend on it seeding the
  #: `skip_greeting` variable per call. OFF by default: the greeting is then runtime-gated
  #: (spoken on a direct call, dropped when a hand-off seeds `skip_greeting`).
  skip_greeting: bool = False
  #: One of `LEGS`. Defaults to `fake` under `--demo` -- a demo build whose two lowered
  #: legs still call live backends is the original incident.
  legs: str = "live"
  #: One of `SPECIALISTS`.
  specialists: str = "cloudrun"
  #: `demo_delay` in the baked scenario: `on` for the recorded default, or seconds.
  specialist_delay: str = "on"
  #: Carry the specialists' raw answers back in the result, under undeclared keys.
  specialist_diag: bool = False
  #: Seconds a lowered sweep leg sleeps, so the group has idle turns to reassure over.
  leg_delay: float = 0.0
  #: Seconds the demo diagnostic stub sleeps, simulating the real fan-out.
  sweep_delay: float = 0.0
  #: Blank the two variable defaults that are live credentials. For an artifact that is
  #: going to be SHARED; a build for deploy needs them.
  scrub_secrets: bool = False
  #: A `cujs.yaml` journey baked into the variable defaults.
  cuj: typing.Optional[str] = None
  #: Emit the slot-filling engine as precompiled bytecode instead of source. ON, because
  #: CES re-parses a tool's module on EVERY invocation and this one is 555KB / 49,860 AST
  #: nodes -- 165->74ms and 169->70ms per engine call, measured on this agent, all of it in
  #: front of the first spoken word. Whether it HAPPENED is a separate manifest key; see
  #: `PACKING_STATES`.
  pack_engine: bool = True
  #: The CES app this build will be pushed to, baked as `CES_APP_VARIABLE`'s default so
  #: the specialist caller can tell the proxy whose specialists to open. Empty leaves the
  #: proxy on its own default app, which is what every build did before.
  ces_app: str = ""

  @property
  def fake_legs(self) -> bool:
    return self.legs == "fake"

  @property
  def local_specialists(self) -> bool:
    return self.specialists == "local"

  def as_manifest(self) -> dict:
    return dataclasses.asdict(self)

  def summary(self) -> str:
    """One line for the build log, so a terminal scrollback records what was built."""
    return (f"demo={str(self.demo).lower()} "
            f"steering={str(self.steering).lower()} "
            f"skip_greeting={str(self.skip_greeting).lower()} legs={self.legs} "
            f"specialists={self.specialists} "
            f"specialist_delay={self.specialist_delay} "
            f"specialist_diag={str(self.specialist_diag).lower()} "
            f"leg_delay={format_seconds(self.leg_delay)} "
            f"sweep_delay={format_seconds(self.sweep_delay)} "
            f"scrub_secrets={str(self.scrub_secrets).lower()} "
            f"pack_engine={str(self.pack_engine).lower()} "
            f"cuj={self.cuj or '-'} "
            f"ces_app={self.ces_app or '-'}")


def format_seconds(value: float) -> str:
  """`3.0` as `3`, `2.5` as `2.5`.

  A leg's delay is rendered into the emitted tool body, and the environment variable this
  replaced carried whatever the caller typed. `%g` is what makes `--leg-delay 3` emit the
  same bytes `SPIKE_SLOW_LEGS=3` did, which is how the switch-for-switch equivalence was
  proved.
  """
  return f"{float(value):g}"


def add_arguments(parser) -> None:
  """Declare every switch on `parser`. This IS the documentation of the build's modes."""
  parser.add_argument("--out", default="./built",
                      help="Destination app dir (wiped and rewritten).")
  parser.add_argument(
      "--demo", action="store_true",
      help="Console-demoable build: bake cujs.yaml's account -> scenario bindings into "
           "the account gate, answer every carried tool from its recorded fixture, and "
           "resolve the diagnostic sweep from mock_config_string instead of the live "
           "backends. Implies --legs=fake. A console session never fires a tool fake, so "
           "without this an interactive conversation reaches unreachable backends.")
  parser.add_argument(
      "--steering", action="store_true",
      help="Make the multi-level intent router the root flow. OFF by default, which emits "
           "the flat single-flow repair agent (the pre-router shape): repair owns the "
           "opening turn and handles reboot-on-request and a human hand-off itself. On, "
           "the router owns routing and the opening greeting, and routes the deferred "
           "golden categories.")
  parser.add_argument(
      "--skip-greeting", action="store_true", dest="skip_greeting",
      help="Bake the opening greeting OFF: the account ask is emitted greeting-free ('To "
           "get started, ...') so even a direct call to this build never says 'Welcome to "
           "Xfinity'. For a build deployed purely as a transfer target, so it does not "
           "depend on the upstream agent seeding the skip_greeting variable per call. OFF "
           "by default, where the greeting is runtime-gated: spoken on a direct call, "
           "dropped when a hand-off (transferToNga / A2A) seeds skip_greeting.")
  parser.add_argument(
      "--legs", choices=LEGS, default=None,
      help="Whether the two lowered sweep legs answer from their recorded fixtures "
           "(fake) or call their backends (live). Default: fake with --demo, else live.")
  parser.add_argument(
      "--specialists", choices=SPECIALISTS, default="cloudrun",
      help="Where the specialist pair runs. cloudrun (default) goes through the "
           "specialist proxy, which is the deployed shape. local runs the pair as "
           "ordinary in-sandbox tool calls behind the same contract -- no Cloud Run, no "
           "Firestore, no IAM -- for a demo on a machine that has none of them.")
  parser.add_argument(
      "--specialist-delay", default="on", metavar="on|SECONDS",
      dest="specialist_delay",
      help="DEMO builds: how long the specialists' RECORDED answer pretends to take, as "
           "`demo_delay` in the baked scenario. `on` (default) takes the tuned value; a "
           "number overrides it. A fixture answers in microseconds, so without this the "
           "job lands on the first poll and the reassurance ladder is never reached.")
  parser.add_argument(
      "--specialist-diag", action="store_true", dest="specialist_diag",
      help="--specialists=local only: carry the specialists' raw answers back in the "
           "result under undeclared keys, for diagnosing a status you did not expect.")
  parser.add_argument(
      "--leg-delay", type=float, default=0.0, metavar="SECONDS", dest="leg_delay",
      help="Seconds each lowered sweep leg sleeps. Every backend reachable from a dev "
           "desk answers in well under a second, so this is the only way the reassurance "
           "path has idle turns to be observed on at all.")
  parser.add_argument(
      "--sweep-delay", type=float, default=0.0, metavar="SECONDS", dest="sweep_delay",
      help="DEMO builds: seconds the diagnostic stub sleeps, simulating the real "
           "fan-out. The auth proxy is unreachable from dev, so this is the only way to "
           "see (and to fix) what a caller sits through.")
  parser.add_argument(
      "--scrub-secrets", action="store_true", dest="scrub_secrets",
      help="Blank the two variable defaults whose values are live credentials. For an "
           "artifact that will be SHARED: a build for DEPLOY needs them, because the "
           "gateway specialist reads one at run time.")
  parser.add_argument(
      "--pack-engine", action=argparse.BooleanOptionalAction, default=True,
      dest="pack_engine",
      help="Emit the slot-filling engine tool as precompiled bytecode instead of source. "
           "ON. CES re-parses a python tool's module on every invocation, and the engine "
           "is 555KB / 49,860 AST nodes: 165->74ms and 169->70ms per engine call measured "
           "on this agent, ~11 calls a session, all of it ahead of the first spoken word. "
           "--no-pack-engine emits the readable source, which is what to reach for when "
           "reading or patching the deployed body. Packing needs a python 3.12 "
           "interpreter (what CES runs); the build finds one through uv when it is not "
           "running on one itself, and says so loudly if it cannot.")
  parser.add_argument(
      "--ces-app", default="", metavar="RESOURCE", dest="ces_app",
      help="The CES app this build is pushed to (projects/P/locations/L/apps/A), baked "
           "as the ces_app variable's default. The specialist proxy serves whichever app "
           "asks; a caller that sends none gets the app the proxy itself was deployed "
           "pinned to, so its specialists fail. Only knowable for a push to an app that "
           "already exists -- a brand-new app is pushed once, then rebuilt with its id.")
  parser.add_argument(
      "--cuj", default=None,
      help="Bake a cujs.yaml CUJ into the variable defaults, so a console session opens "
           "on that journey with nothing to seed. Use with --demo (a console session "
           "never fires the tool fakes).")


def resolve(args) -> BuildConfig:
  """Turn parsed arguments into the frozen config, refusing combinations that no-op.

  A flag that silently does nothing is the failure this whole file is about, so every
  such combination is an error rather than a warning. `--cuj` without `--demo` is the one
  exception: it is degraded rather than inert (the variables are baked, they just are not
  reached by a console session), and it already warns.
  """
  legs = args.legs or ("fake" if args.demo else "live")
  config = BuildConfig(
      demo=bool(args.demo),
      steering=bool(args.steering),
      skip_greeting=bool(args.skip_greeting),
      legs=legs,
      specialists=args.specialists,
      specialist_delay=str(args.specialist_delay),
      specialist_diag=bool(args.specialist_diag),
      leg_delay=float(args.leg_delay),
      sweep_delay=float(args.sweep_delay),
      scrub_secrets=bool(args.scrub_secrets),
      pack_engine=bool(args.pack_engine),
      cuj=args.cuj,
      ces_app=str(args.ces_app or ""))

  problems = []
  if config.ces_app and not _APP_RESOURCE.match(config.ces_app):
    problems.append(f"--ces-app {config.ces_app!r} is not a CES app resource; it must "
                    "read projects/<project>/locations/<location>/apps/<app>. The proxy "
                    "only falls back when it is given NO app, so this one would be used "
                    "as sent: neither specialist opens, and two empty legs derive a "
                    "healthy line the caller is then told about")
  if config.specialist_diag and not config.local_specialists:
    problems.append("--specialist-diag needs --specialists=local; the Cloud Run proxy "
                    "returns its own diagnostics and this flag would do nothing")
  if config.sweep_delay and not config.demo:
    problems.append("--sweep-delay needs --demo; it slows the demo stub, and a build "
                    "without --demo calls the real fan-out, which has its own latency")
  if config.specialist_delay != "on" and not config.demo:
    problems.append("--specialist-delay needs --demo; it tunes the RECORDED answer's "
                    "latency, and a build without --demo waits for the real pair")
  if problems:
    raise SystemExit("build: " + "\n       ".join(problems))
  return config


#: The switches that used to be environment variables, and the flags that replaced them.
#: Kept so a build can REFUSE one rather than ignore it: this repo's own scenario records
#: and everybody's shell history are full of `SPIKE_DEMO=1 SPIKE_LOCAL_SPECIALISTS=1
#: python build.py ...`, and quietly emitting a live-backend build for that command line
#: would be the original incident over again, with the artifact and the intent swapped.
RETIRED_ENV = {
    "SPIKE_DEMO": "--demo",
    "SPIKE_FAKE_LEGS": "--legs=fake",
    "SPIKE_SLOW_LEGS": "--leg-delay=<seconds>",
    "SPIKE_LOCAL_SPECIALISTS": "--specialists=local",
    "SPIKE_LOCAL_DIAG": "--specialist-diag",
    "DEMO_SWEEP_SECONDS": "--specialist-delay=<seconds>",
    "SCRUB_SECRETS": "--scrub-secrets",
}


def reject_retired_env(environ=None) -> None:
  """Stop the build if a caller set a switch that is no longer read.

  Reads the environment only to REFUSE it. Nothing here decides anything about the
  artifact, which is the property that makes it not a switch.
  """
  environ = os.environ if environ is None else environ
  found = [(name, flag) for name, flag in sorted(RETIRED_ENV.items())
           if environ.get(name)]
  if not found:
    return
  lines = "\n".join(f"       {name}={environ[name]}  ->  {flag}" for name, flag in found)
  raise SystemExit(
      "build: these switches are flags now, and setting them in the environment does "
      "nothing:\n" + lines + "\n       Re-run with the flags. `build.py --help` lists "
      "every mode.")


_DEFAULT = BuildConfig()
_active: typing.Optional[BuildConfig] = None
_observed = False


def activate(config: BuildConfig) -> BuildConfig:
  """Install the resolved config. Must happen before any emitter is imported."""
  global _active
  if _observed and config != current():
    raise SystemExit(
        "build: the build config was activated after something had already read it, so "
        "part of this build was composed against the defaults and part against the "
        "flags. That is the failure `--demo` shipped with for months -- it set its "
        "switch in main(), after `import app` had already emitted every tool body from "
        "it. Activate before importing `app`.")
  _active = config
  return config


def current() -> BuildConfig:
  """The active config, or the defaults for anything that is not a build."""
  global _observed
  _observed = True
  return _active if _active is not None else _DEFAULT


def reset_for_test(config: typing.Optional[BuildConfig] = None) -> None:
  """Put the module back to a known state. For `tests/config_check.py` only."""
  global _active, _observed
  _active, _observed = config, False


def write_manifest(out_dir: str, config: BuildConfig, engine_packing: str) -> str:
  """Stamp the resolved config, and what packing actually did, into the emitted app dir.

  A built app dir used to be silent about how it was built: nothing in it said whether
  its legs were faked or where its specialists ran, which is precisely why `--demo` could
  lie undetected. This is the artifact answering for itself.

  `engine_packing` is the OUTCOME, not the flag. It is a separate argument rather than a
  config field for the reason the flag exists at all: an app was once deployed named
  `comcast-DEMO-packed` while serving source, and nothing in the artifact contradicted the
  name. A build that asked for bytecode and emitted source has to be readable as such off
  the artifact alone, so the pairing is checked here -- `off` for a build that did not ask,
  `packed` or `skipped` for one that did, and nothing else is writable.
  """
  if engine_packing not in PACKING_STATES:
    raise ValueError(f"engine_packing must be one of {PACKING_STATES}, not "
                     f"{engine_packing!r}")
  if config.pack_engine != (engine_packing != PACK_OFF):
    raise ValueError(
        f"manifest would say pack_engine={config.pack_engine} and "
        f"{PACKING_KEY}={engine_packing!r}, which cannot both be true. A build that asked "
        f"for packing records {PACKED!r} or {PACK_SKIPPED!r}; one that did not records "
        f"{PACK_OFF!r}.")
  data = config.as_manifest()
  data[PACKING_KEY] = engine_packing
  path = os.path.join(out_dir, MANIFEST_NAME)
  with open(path, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
  return path


def _manifest_data(out_dir: str) -> dict:
  with open(os.path.join(out_dir, MANIFEST_NAME)) as fh:
    return json.load(fh)


def read_manifest(out_dir: str) -> BuildConfig:
  """The config a built app dir was emitted from."""
  data = _manifest_data(out_dir)
  data.pop(PACKING_KEY, None)
  return BuildConfig(**data)


def read_packing(out_dir: str) -> str:
  """What a built app dir says its engine tool actually IS. One of `PACKING_STATES`."""
  return _manifest_data(out_dir)[PACKING_KEY]
