# Skills, agents, and voice

Tell the robot "turn left and drive forward one metre", by typing it on the dashboard,
saying it to the robot, or asking a language model to work out the steps. Three nodes
share the work, and none of them can drive the motors directly:

| Node | Listens to | Publishes | Does |
| --- | --- | --- | --- |
| `agent` | `agent.command` `{"text"}` | `agent.plan`, `agent.say` | Turns words into a checked plan of skills |
| `skills` | `agent.plan` | `cmd.drive` (and whatever skills send), `agent.status`, `agent.say` | Runs the plan one step at a time |
| `voice` | `agent.say` | `agent.command` | Microphone and speaker on the robot |

```yaml
nodes:
  - {name: skills, plugin: skills, params: {speed_mps: 0.4, turn_dps: 150}}
  - {name: agent, plugin: agent}
  - {name: voice, plugin: voice, only: robot, params: {wake_word: robot}}   # optional
```

With `nervlynx sim`, or `nervlynx run --control`, the dashboard shows **Talk to the
robot**: type a command (or use your phone keyboard's microphone), read the reply and
the plan's progress, and tick **Speak replies** to hear them.

## Safety

- The `skills` node is the only thing that runs skills, and skills only publish ordinary
  messages, so the drive deadman, `max_speed`, and the e-stop apply as always.
- Every argument is checked against the skill's limits before anything moves: `drive`
  is limited to 3 m, `turn` to 360 degrees, a plan to 10 steps and 3 minutes.
- An e-stop cancels the whole plan; a plan sent while the e-stop is latched is refused.
- If anyone else drives (the joystick, another node), the plan stops at once:
  `stopped: http took over the driving`.
- The `agent` never publishes `cmd.drive`. An LLM can only answer with calls to the
  skills, and its plan is checked twice (by the agent, then by the skills node).

## Built-in skills

| Skill | Arguments | Does |
| --- | --- | --- |
| `stop` | | Stop and forget the rest of the plan |
| `drive` | `distance_m` (-3..3), `speed` (0.1..1, 0.4) | Drive straight; negative is backwards |
| `turn` | `degrees` (-360..360), `speed` (0.1..1, 0.5) | Turn in place; positive is left |
| `wait` | `seconds` (0..30) | Stand still |
| `say` | `text` | Speak (dashboard, and the robot's speaker with `voice`) |
| `find` | `label` (`person`), `timeout_s` (1..30, 10) | Turn until a detection with that label is straight ahead |

`drive` and `turn` use odometry (`odom`) when there is some, and stop a little early
because a robot keeps rolling after a stop (`coast_s`, 0.25 s). Without odometry they
are timed from the `skills` node's `speed_mps` and `turn_dps` (how fast the robot drives
and turns at full speed), so measure those once: drive for two seconds at full speed and
measure how far it went.

## Writing a skill

A skill is a generator in `nodes/*.py`, next to your `@node` functions. It runs a little
on each tick of the `skills` node (20 times a second); each `yield` hands back control.

```python
from nervlynx import skill


@skill(params={"times": (1, 5)})
def wave(robot, *, times=2):
  """Wave the arm."""
  for _ in range(times):
    robot.send("cmd.servo", {"arm": 150})
    yield from robot.wait(0.5)
    robot.send("cmd.servo", {"arm": 30})
    yield from robot.wait(0.5)
  return f"waved {times} times"
```

- Arguments after `robot` must be keyword-only; their defaults (or type hints) set their
  types. `params` bounds them: a `(min, max)` range or a list of allowed values.
- The first line of the docstring is what a language model is told the skill does.
- `robot` gives you `drive(linear, angular)`, `stop()`, `send(topic, payload)`,
  `say(text)`, `wait(seconds)`, `now_s`, `odom`, `detections(label)`, `speed_mps`,
  `turn_dps`, and `coast_s`.
- The return value is reported in `agent.status` when the skill finishes.

## Planners

`planner: rules` (the default) needs nothing else and answers instantly. It understands
`stop`, `drive/go/move forward|back [N m|cm|feet|steps]`, `back up`,
`turn left|right [N degrees]`, `turn around`, `turn a little left`, `wait N seconds`,
`find|look for <thing>`, `come here`, and `say ...` (which takes the rest of the command),
joined by "and", "then", or commas. Anything else gets a reply listing what it can do,
and nothing moves.

`planner: llm` uses any OpenAI-compatible chat API. The model gets the skills as tools,
with their limits, and a short description of the robot:

```yaml
- name: agent
  plugin: agent
  params:
    planner: llm
    about: a small two-wheel robot with a camera, in a living room
    llm:
      base_url: http://localhost:11434/v1   # Ollama on this computer (or another on the mesh)
      model: llama3.1
      # base_url: https://api.openai.com/v1, model: gpt-4o-mini, api_key_env: OPENAI_API_KEY
      timeout_s: 20
```

The request runs on its own thread, so a slow model never stalls the robot. If the model
can't be reached, the rules planner answers instead (`fallback: none` turns that off).
A model that asks for something outside a skill's limits is told no, and nothing moves.

## Voice on the robot

The `voice` node listens with Vosk, offline, through `arecord`, and speaks with
espeak-ng:

```bash
~/.venv/bin/pip install vosk
sudo apt install espeak-ng
curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip -d ~
```

| Param | Default | Meaning |
| --- | --- | --- |
| `model` | `~/vosk-model-small-en-us-0.15` | The unzipped Vosk model folder |
| `wake_word` | `robot` | Only phrases starting with it count ("robot, turn left"); saying it alone makes the next phrase count. `null` sends everything |
| `device` | `default` | ALSA capture device (`arecord -l` lists them) |
| `speak` / `voice` / `words_per_minute` | true / `en` / 160 | Speak `agent.say` replies with espeak-ng |

The wake word keeps the robot from acting on its own replies or on background talk. Off
the robot the node listens to nothing, so the same config runs in the simulator. The
voice node has been tested with stand-ins for Vosk and the microphone, not yet with a
real microphone.

In a browser, the dashboard's **Talk** button uses the browser's speech recognition,
which browsers only allow on https pages or on localhost. On a phone over plain http, use
the keyboard's dictation key in the text box instead.
