"""
doctor_cli.py — Terminal tool for doctors: type or paste a prescription, get the patient
explanation in real time. Installed as a single command (default name "rx") by install.sh.

    rx                              interactive: paste prescriptions one after another
    rx "Amoxicilline 1 g : 1 cp x 2/j pendant 7 jours"    explain this text and exit
    rx -f ordonnance.txt            explain a text file
    rx -a dictee.m4a                explain a voice recording
    rx -p ollama -m <model>         choose provider / model for this run
    rx --settings                   show the settings file and current configuration

Defaults (provider, model, API keys...) come from the settings file `.env` next to this
script; command-line flags override them, and real environment variables override the file.

How to take each medicine is shown as soon as the prescription is read (a few seconds);
what each medicine is for appears one by one as it arrives. No advice is added: the output
only restates the prescription in simple words. Each
explanation is then shown to the doctor, who approves it, edits it, or rejects it. Only an
approved explanation is saved to outputs/ for the patient, stamped "Checked by your doctor".

Interactive input:
    - Type or paste the prescription (several lines are fine).
    - Press Enter on two empty lines in a row (or Ctrl-D) to get the explanation.
    - ":audio path/to/file.m4a" explains a voice recording instead.
    - "q" or Ctrl-D on an empty prompt quits.
"""

from __future__ import annotations

import argparse
import itertools
import shlex
import subprocess
import tempfile
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = PROJECT_DIR / ".env"


def load_settings(path: Path = SETTINGS_FILE) -> None:
    """Read KEY=VALUE lines into the environment (real environment variables win)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# The pipeline reads its configuration at import time, so load the settings first.
load_settings()
import ai_pipeline as pipeline  # noqa: E402

OUTPUT_DIR = PROJECT_DIR / "outputs"
COMMAND_NAME = os.getenv("RX_COMMAND_NAME", "rx")

_COLOR = sys.stdout.isatty()


def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _style(text, "1")


def dim(text: str) -> str:
    return _style(text, "2")


def warn(text: str) -> str:
    return _style(text, "33")


def _print_lines(lines: list[str]) -> None:
    for line in lines:
        if line.lstrip().startswith(("Sorry", "CHECK BEFORE", "NOT SURE", "HIGH RISK")):
            print(warn(line))
        elif line.lstrip().startswith(("CORRECTED", "PRIVACY")):
            print(dim(line))
        elif line in pipeline.HEADINGS or re.match(r"\d+\. ", line):
            print(bold(line))  # section headings and medicine names
        else:
            print(line)


class Spinner:
    """Shows "<message> 3s" with a spinner while waiting. No-op when not in a terminal."""

    def __init__(self, message: str):
        self.message = message
        self._stop = threading.Event()
        self.lock = threading.Lock()  # hold while printing so the spinner can't write mid-line
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._start = time.monotonic()

    def _run(self) -> None:
        for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.wait(0.1):
                break
            elapsed = time.monotonic() - self._start
            with self.lock:
                sys.stdout.write(f"\r\033[K{dim(f'{frame} {self.message} {elapsed:.0f}s')}")
                sys.stdout.flush()

    def __enter__(self):
        if _COLOR:
            self._thread.start()
        return self

    def update(self, message: str) -> None:
        self.message = message

    def clear(self) -> None:
        if _COLOR:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        self.clear()

    def __exit__(self, *exc):
        self.stop()


def read_prescription() -> str | None:
    """Read a multi-line prescription. Returns None when the doctor wants to quit."""
    print(bold("\nPrescription") + dim("  (Enter twice to explain · :audio <file> · q to quit)"))
    lines: list[str] = []
    empty_streak = 0
    while True:
        try:
            line = input("> " if not lines else "  ")
        except EOFError:
            print()
            break
        if not lines and line.strip().lower() in ("q", "quit", "exit"):
            return None
        if not line.strip():
            if not lines:
                continue
            empty_streak += 1
            if empty_streak >= 2:
                break
            lines.append("")
            continue
        empty_streak = 0
        lines.append(line)
    text = "\n".join(lines).strip()
    return text if text else None


def explain(text: str) -> dict:
    """Stream the explanation to the screen and return the final "done" event."""
    start = time.monotonic()
    timings: dict[str, float] = {}
    result: dict = {}

    with Spinner("Reading the prescription…") as spinner:
        for event in pipeline.stream_prescription(text):
            if event["type"] == "done":
                spinner.stop()
            spinner.lock.acquire()
            spinner.clear()
            try:
                _handle(event, spinner, start, timings)
            finally:
                spinner.lock.release()
            if event["type"] == "done":
                result = event
    return result


def _handle(event: dict, spinner: Spinner, start: float, timings: dict) -> None:
    if event["type"] == "dosing":
        timings["dosing"] = time.monotonic() - start
        prescription = event["prescription"]
        meds = prescription["medicines"]
        if illness := pipeline.render_conditions(prescription["conditions"]):
            print()
            _print_lines(illness)
        if meds:
            count = len(meds)
            print()
            _print_lines([
                "Your medicines",
                f"You have {count} medicine{'s' if count != 1 else ''}.",
                "",
                "How to take them",
            ])
            for i, med in enumerate(meds, 1):
                _print_lines(pipeline.render_medicine(i, med, part="dosing"))
        spinner.update("Looking up what each medicine is for…")

    elif event["type"] == "medicine":
        if not timings.get("details_heading"):
            timings["details_heading"] = 1
            print()
            _print_lines(["What they are for"])
        med = event["medicine"]
        lines = pipeline.render_medicine(event["index"] + 1, med, part="details")
        if not med.get("purpose"):
            lines.append(dim("   (ask the pharmacist what it is for)"))
        _print_lines(lines)

    elif event["type"] == "done":
        print()
        _print_lines(pipeline.render_footer(event["prescription"]))
        print()
        _print_lines(event["review"])
        total = time.monotonic() - start
        print(dim(f"\nDosing in {timings['dosing']:.1f}s · complete in {total:.1f}s"))


def doctor_review(result: dict) -> None:
    """The doctor double-checks the simplification; only an approved version goes to the patient."""
    text, unsure = result["text"], result["needs_review"]

    if OUTPUT_DIR is None:
        print(dim("Preview only (--no-save): nothing is given to the patient."))
        return
    if not sys.stdin.isatty():
        print(warn("Not given to the patient: a doctor must approve it first (run this in a terminal)."))
        return

    edited = False
    while True:
        print(bold("\nDoctor: is this correct for the patient?") +
              dim("  [y] approve and give it  ·  [e] edit it  ·  [n] do not give it"))
        if unsure:
            print(warn("   Something above is uncertain or high-risk. Check it, fix it with [e], or type \"yes\" in full to approve."))
        try:
            answer = input("> ").strip().lower()
        except EOFError:
            answer = "n"

        if answer == "e":
            text = edit_text(text)
            edited = True
            print()
            _print_lines(text.splitlines())
            unsure = False if "Sorry, I am not sure" not in text and "Sorry, I could not" not in text else unsure
        elif answer in ("yes",) or (answer == "y" and not unsure):
            path = save(text, edited)
            print(bold("Approved.") + f" Patient copy saved to {path}")
            return
        elif answer in ("n", "no"):
            print(warn("Not approved: nothing was given to the patient."))
            return
        elif answer == "y" and unsure:
            print(warn('Type "yes" in full to approve something that is not certain.'))


def edit_text(text: str) -> str:
    """Open the explanation in the doctor's editor ($EDITOR, default nano) and return the result."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp = Path(f.name)
    try:
        subprocess.call([*shlex.split(editor), str(tmp)])
        return tmp.read_text(encoding="utf-8").strip()
    finally:
        tmp.unlink(missing_ok=True)


def save(text: str, edited: bool = False) -> Path:
    """Save the approved patient copy (the doctor's review notes are not included)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"patient_{datetime.now():%Y%m%d_%H%M%S}.txt"
    stamp = f"Checked by your doctor on {datetime.now():%d/%m/%Y}" + (" (corrected by the doctor)." if edited else ".")
    path.write_text(f"{text}\n\n{stamp}\n", encoding="utf-8")
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=COMMAND_NAME,
        description="Explain a doctor's prescription to the patient in simple words.",
        epilog=f"Settings file: {SETTINGS_FILE}",
    )
    parser.add_argument("text", nargs="*", help="prescription text to explain once (leave empty for interactive mode)")
    parser.add_argument("-f", "--file", type=Path, help="read the prescription from a text file")
    parser.add_argument("-a", "--audio", type=Path, help="explain a voice recording")
    parser.add_argument("-p", "--provider", choices=sorted(pipeline._PROVIDERS), help="AI provider for this run")
    parser.add_argument("-m", "--model", help="model name for this run")
    parser.add_argument("-o", "--output-dir", type=Path, help=f"where to save explanations (default: {OUTPUT_DIR})")
    parser.add_argument("--no-save", action="store_true", help="preview only: no doctor approval, nothing saved")
    parser.add_argument("--settings", action="store_true", help="show the settings file and current configuration")
    return parser.parse_args(argv)


def show_settings() -> None:
    print(bold("Settings file: ") + str(SETTINGS_FILE) + ("" if SETTINGS_FILE.is_file() else dim("  (missing)")))
    key_env = pipeline._PROVIDERS[pipeline.LLM_PROVIDER][1]
    key_state = "not needed" if key_env is None else ("set" if os.getenv(key_env) else warn("NOT SET"))
    print(f"Provider:  {pipeline.LLM_PROVIDER}")
    print(f"Model:     {pipeline.LLM_MODEL}")
    print(f"API key:   {key_state}" + (dim(f"  ({key_env})") if key_env else ""))
    print(f"Saves to:  {OUTPUT_DIR or 'nowhere (--no-save)'}")
    print(dim(f"\nProviders: {', '.join(sorted(pipeline._PROVIDERS))}. Edit the settings file to change the defaults."))


def run_once(text: str | None, audio: Path | None) -> int:
    """Explain one prescription (text or audio) and return an exit code."""
    try:
        if audio:
            with Spinner("Transcribing…"):
                text = pipeline.transcribe_voice(str(audio))
            print(dim("Heard: ") + text)
        doctor_review(explain(text or ""))
        return 0
    except pipeline.PipelineError as exc:
        print(warn(str(exc)), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    global OUTPUT_DIR
    args = parse_args(argv)

    logging.basicConfig(level=logging.ERROR, format="%(message)s")  # keep the doctor's screen clean
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.provider or args.model:
        pipeline.configure(provider=args.provider, model=args.model)
    if args.no_save:
        OUTPUT_DIR = None
    elif args.output_dir:
        OUTPUT_DIR = args.output_dir

    if args.settings:
        show_settings()
        return 0

    key_env = pipeline._PROVIDERS[pipeline.LLM_PROVIDER][1]
    if key_env and not os.getenv(key_env):
        print(warn(f"{key_env} is not set.") + f" Add it to {SETTINGS_FILE}, or use the free local model: {COMMAND_NAME} -p ollama",
              file=sys.stderr)
        return 1

    # One-shot modes: explain and exit.
    if args.audio:
        return run_once(None, args.audio)
    if args.file:
        return run_once(args.file.read_text(encoding="utf-8"), None)
    if args.text:
        return run_once(" ".join(args.text), None)

    print(bold("Prescription explainer") + dim(f"  ·  {pipeline.LLM_PROVIDER} / {pipeline.LLM_MODEL}"))
    while True:
        text = read_prescription()
        if text is None:
            print("Bye.")
            return 0

        try:
            if text.startswith(":audio"):
                audio_path = text[len(":audio"):].strip().strip("'\"")
                with Spinner("Transcribing…"):
                    text = pipeline.transcribe_voice(audio_path)
                print(dim("Heard: ") + text)
            doctor_review(explain(text))
        except pipeline.PipelineError as exc:
            print(warn(f"\n{exc}"))
        except KeyboardInterrupt:
            print(warn("\nCancelled."))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nBye.")
