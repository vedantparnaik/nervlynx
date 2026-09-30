"""`agent`: turn what someone says into a plan of skills.

Listens on `agent.command` ({"text": "turn left and drive forward one metre"}) from the
dashboard's Talk box, the `voice` node, ROS, or anything else, and publishes a checked
plan on `agent.plan` for the `skills` node, plus a reply on `agent.say`.

Planners (`planner:`):
  rules  (default) offline, instant, and predictable. Understands stop, drive
         forward/back N metres (or cm, feet), turn left/right N degrees (or turn around),
         wait N seconds, say ..., and find/look for <thing>, joined by "and", "then", or
         commas. Anything else gets a reply listing what it can do, and nothing moves.
  llm    any OpenAI-compatible chat API (OpenAI, Ollama, llama.cpp, LM Studio, vLLM).
         The model sees the skills as tools with their limits and can only answer with
         calls to them; the plan is checked before it is sent, and the skills node checks
         it again. If the model can't be reached, the rules planner answers instead.

The agent never drives motors itself: every command still passes the skills node, the
drive deadman, `max_speed`, and the e-stop.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Iterable

from robot_core.live import LiveNode, NodeContext, Output
from robot_core.runtime import RuntimeMessage
from robot_core.skills import PLAN_TOPIC, SAY_TOPIC, SKILLS, parse_plan, speech

COMMAND_TOPIC = "agent.command"
_NUMBERS = {
  "a": 1.0, "an": 1.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0,
  "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0, "fifteen": 15.0, "twenty": 20.0, "thirty": 30.0,
  "forty": 40.0, "forty-five": 45.0, "fifty": 50.0, "sixty": 60.0, "ninety": 90.0, "half": 0.5,
  "a half": 0.5, "half a": 0.5, "quarter": 0.25, "a quarter": 0.25, "a couple": 2.0, "a couple of": 2.0,
}
_NUMBER = r"(?P<n>\d+(?:\.\d+)?|" + "|".join(sorted((re.escape(k) for k in _NUMBERS), key=len, reverse=True)) + r")"
_LENGTH_UNITS = {"m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0, "cm": 0.01, "centimeter": 0.01,
                 "centimeters": 0.01, "centimetre": 0.01, "centimetres": 0.01, "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
                 "step": 0.5, "steps": 0.5}
_SPLIT = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b|\bafter that\b)\s*")
_PEOPLE = {"me", "someone", "somebody", "anyone", "anybody", "a person", "person", "people", "a human", "human"}
_HELP = "I can: stop, drive forward or back some metres, turn left or right some degrees, wait, say something, and find something."
SYSTEM_PROMPT = (
  "You control a small wheeled robot{about}. Answer only by calling the tools, in the order they should run. "
  "Distances are in metres and angles in degrees; positive degrees turn left. Stay inside each tool's limits. "
  "If a request is unsafe, unclear, or impossible with these tools, call say to explain instead of guessing."
)


def _number(text: str) -> float:
  text = text.strip()
  return float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else _NUMBERS[text]


def _clause(text: str) -> dict[str, Any]:
  """One step of a spoken command, or ValueError."""
  t = re.sub(r"\s+", " ", text.strip().lower()).strip(" .!?")
  t = re.sub(r"^(?:please|could you|can you|now|and|robot)\s+", "", t)
  t = re.sub(r"\s+please$", "", t)
  if re.fullmatch(r"(?:stop|halt|freeze|stay|stay put|hold on|wait there)", t):
    return {"skill": "stop"}
  if match := re.fullmatch(rf"(?:wait|pause|sleep)(?: for)?(?: {_NUMBER})?(?: (?:seconds?|secs?|s))?", t):
    return {"skill": "wait", "args": {"seconds": _number(match["n"]) if match["n"] else 2.0}}
  if re.fullmatch(r"(?:turn|spin) (?:all the way )?around", t) or t == "about face":
    return {"skill": "turn", "args": {"degrees": 180.0}}
  if match := re.fullmatch(rf"(?:turn|rotate|spin)(?: to the)? (?P<dir>left|right)(?: (?:by )?{_NUMBER}(?: ?(?:degrees?|deg|°))?)?", t):
    degrees = _number(match["n"]) if match["n"] else 90.0
    return {"skill": "turn", "args": {"degrees": degrees if match["dir"] == "left" else -degrees}}
  if match := re.fullmatch(rf"(?:turn|rotate|spin)(?: by)? {_NUMBER}(?: ?(?:degrees?|deg|°))?(?: to the)? (?P<dir>left|right)", t):
    degrees = _number(match["n"])
    return {"skill": "turn", "args": {"degrees": degrees if match["dir"] == "left" else -degrees}}
  if match := re.fullmatch(r"turn (?:a (?:little|bit)|slightly) (?P<dir>left|right)|turn (?P<dir2>left|right) (?:a (?:little|bit)|slightly)", t):
    left = (match["dir"] or match["dir2"]) == "left"
    return {"skill": "turn", "args": {"degrees": 20.0 if left else -20.0}}
  units = "|".join(sorted(_LENGTH_UNITS, key=len, reverse=True))
  move = r"(?:go|drive|move|roll|head|come|back up|reverse)"
  if match := re.fullmatch(rf"{move}?(?: ?(?P<dir>forward|forwards|ahead|straight|back|backward|backwards))?(?: (?:by )?{_NUMBER} ?(?P<unit>{units}))?", t):
    if match["dir"] or match["n"] or t.startswith(("back up", "reverse")):
      backwards = (match["dir"] or "").startswith("back") or t.startswith(("back up", "reverse"))
      distance = _number(match["n"]) * _LENGTH_UNITS[match["unit"]] if match["n"] else 0.5
      return {"skill": "drive", "args": {"distance_m": -distance if backwards else distance}}
  if match := re.fullmatch(r"(?:find|look for|search for|where is|where are|look at|face|turn to|come (?:to|here)|come to)(?: the| a| an| my)? ?(?P<what>.*)", t):
    what = match["what"].strip() or "me"
    label = "person" if what in _PEOPLE or t.startswith("come") else what
    return {"skill": "find", "args": {"label": label}}
  raise ValueError(f"I don't know how to {t!r}. {_HELP}")


def rules_plan(text: str) -> list[dict[str, Any]]:
  """Steps for a spoken or typed command, or ValueError with a reply for the person.

  "say" takes the rest of the command as what to say, "and"s included.
  """
  said = re.search(r"\b(?:say|tell (?:me|us|them))\s+", text, flags=re.I)
  before, spoken = (text[: said.start()], text[said.end() :].strip()) if said else (text, "")
  steps = [_clause(clause) for clause in _SPLIT.split(before) if clause and clause.strip(" .!?")]
  if spoken:
    steps.append({"skill": "say", "args": {"text": spoken}})
  if not steps:
    raise ValueError(f"I didn't catch that. {_HELP}")
  return steps


def describe(steps: list[dict[str, Any]]) -> str:
  """A short spoken summary of a plan."""
  parts = []
  for step in steps:
    args = step.get("args") or {}
    name = step["skill"]
    if name == "drive":
      d = args["distance_m"]
      parts.append(f"driving {'back ' if d < 0 else ''}{abs(d):g} metre{'s' if abs(d) != 1 else ''}")
    elif name == "turn":
      d = args["degrees"]
      parts.append("turning around" if abs(d) == 180 else f"turning {'left' if d > 0 else 'right'} {abs(d):g} degrees")
    elif name == "wait":
      parts.append(f"waiting {args['seconds']:g} seconds")
    elif name == "find":
      parts.append(f"looking for {'you' if args.get('label') == 'person' else 'a ' + str(args.get('label'))}")
    elif name == "stop":
      parts.append("stopping")
    elif name != "say":
      parts.append(name.replace("_", " "))
  text = ", then ".join(parts)
  return (text[:1].upper() + text[1:] + ".") if text else ""


class LlmClient:
  """Minimal OpenAI-compatible chat-completions client with tool calling (standard library only)."""

  def __init__(self, *, base_url: str, model: str, api_key: str | None = None, timeout_s: float = 20.0, temperature: float = 0.0) -> None:
    self.base_url = base_url.rstrip("/")
    self.model = model
    self.api_key = api_key
    self.timeout_s = timeout_s
    self.temperature = temperature

  def plan(self, text: str, system: str, tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """(steps, what the model said) for one request."""
    body = {
      "model": self.model,
      "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
      "tools": tools,
      "tool_choice": "auto",
      "temperature": self.temperature,
    }
    headers = {"Content-Type": "application/json"}
    if self.api_key:
      headers["Authorization"] = f"Bearer {self.api_key}"
    request = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
      reply = json.loads(response.read())
    message = (reply.get("choices") or [{}])[0].get("message") or {}
    steps = []
    for call in message.get("tool_calls") or []:
      function = call.get("function") or {}
      raw = function.get("arguments") or "{}"
      args = json.loads(raw) if isinstance(raw, str) else raw
      steps.append({"skill": function.get("name"), "args": args})
    return steps, str(message.get("content") or "").strip()


class Agent(LiveNode):
  """Plans skills from text commands, offline with rules or with an LLM."""

  rate_hz = 10.0

  def __init__(
    self,
    *,
    planner: str = "rules",
    llm: dict[str, Any] | None = None,
    fallback: str = "rules",
    about: str = "",
    command_topic: str = COMMAND_TOPIC,
    max_steps: int = 10,
  ) -> None:
    """`llm`: {base_url, model, api_key_env, timeout_s, temperature}. `about` tells the
    model about this robot (what it looks like, where it is)."""
    if planner not in ("rules", "llm"):
      raise ValueError("planner must be rules or llm")
    if fallback not in ("rules", "none"):
      raise ValueError("fallback must be rules or none")
    self.planner = planner
    self.fallback = fallback
    self.about = about
    self.command_topic = command_topic
    self.input_topics = (command_topic,)
    self.max_steps = int(max_steps)
    self.client: LlmClient | None = None
    self._llm_cfg = dict(llm or {})
    if planner == "llm":
      unknown = sorted(set(self._llm_cfg) - {"base_url", "model", "api_key_env", "timeout_s", "temperature"})
      if unknown:
        raise ValueError(f"llm: unknown settings {', '.join(unknown)}")
      if not self._llm_cfg.get("model"):
        raise ValueError('llm needs a model, e.g. {base_url: "http://localhost:11434/v1", model: "llama3.1"}')
    self._lock = threading.Lock()
    self._done: list[tuple[list[dict[str, Any]] | None, str]] = []
    self._busy = False
    self.handled = 0
    self.last: dict[str, Any] | None = None

  def setup(self, ctx: NodeContext) -> None:
    if self.planner == "llm":
      cfg = self._llm_cfg
      key_env = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
      self.client = LlmClient(
        base_url=str(cfg.get("base_url", "https://api.openai.com/v1")),
        model=str(cfg["model"]),
        api_key=os.environ.get(key_env) or None,
        timeout_s=float(cfg.get("timeout_s", 20.0)),
        temperature=float(cfg.get("temperature", 0.0)),
      )

  def _check(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checked = parse_plan({"steps": steps}, max_steps=self.max_steps)
    return [{"skill": spec.name, "args": args} for spec, args in checked]

  def _rules(self, text: str) -> tuple[list[dict[str, Any]] | None, str]:
    try:
      steps = self._check(rules_plan(text))
    except ValueError as exc:
      return None, str(exc)
    return steps, describe(steps)  # the say skill speaks for itself

  def _ask_llm(self, text: str) -> None:
    assert self.client is not None
    system = SYSTEM_PROMPT.format(about=f" ({self.about})" if self.about else "")
    try:
      steps, content = self.client.plan(text, system, [spec.tool() for spec in SKILLS.values()])
      if steps:
        result: tuple[list[dict[str, Any]] | None, str] = (self._check(steps), content or describe(steps))
      else:
        result = (None, content or f"I'm not sure what to do. {_HELP}")
    except ValueError as exc:
      result = (None, f"The model asked for something I can't do: {exc}")
    except (OSError, urllib.error.URLError, KeyError, IndexError, TypeError) as exc:
      if self.fallback == "rules":
        steps_or_none, reply = self._rules(text)
        result = (steps_or_none, f"(The language model is unreachable: {exc}; using simple commands.) {reply}")
      else:
        result = (None, f"The language model is unreachable: {exc}")
    with self._lock:
      self._done.append(result)
      self._busy = False

  def on_message(self, msg: RuntimeMessage, ctx: NodeContext) -> Iterable[Output] | None:
    text = str(msg.payload.get("text", "")).strip()
    if not text:
      return None
    self.handled += 1
    if self.planner == "rules":
      return self._publish(*self._rules(text))
    with self._lock:
      if self._busy:
        return self._publish(None, "Still thinking about the last request; try again in a moment.")
      self._busy = True
    threading.Thread(target=self._ask_llm, args=(text,), name="nervlynx-agent", daemon=True).start()
    return None

  def tick(self, ctx: NodeContext) -> Iterable[Output] | None:
    with self._lock:
      done, self._done = self._done, []
    out: list[Output] = []
    for steps, reply in done:
      out.extend(self._publish(steps, reply) or [])
    return out or None

  def _publish(self, steps: list[dict[str, Any]] | None, reply: str) -> list[Output]:
    out: list[Output] = []
    if steps:
      out.append((PLAN_TOPIC, "Plan", {"steps": steps, "source": "agent"}))
    if reply:
      out.append((SAY_TOPIC, "Speech", speech(reply)))
    self.last = {"steps": steps, "reply": reply}
    return out

  def status(self) -> dict[str, Any]:
    return {
      "agent": True,
      "planner": self.planner,
      "model": self._llm_cfg.get("model") if self.planner == "llm" else None,
      "busy": self._busy,
      "handled": self.handled,
      "last": self.last,
    }
