#!/usr/bin/env python3
"""Point the firmware config at this checkout for its components and sounds.

home-assistant-voice.realtime.yaml takes voice_kit, va_client and its sounds
from this repository on GitHub, at firmware_ref, so that it builds without a
checkout. CI builds the commit under test instead: this rewrites the config in
place to take them from the checkout, by paths relative to firmware/. It's for
a throwaway checkout; don't commit the result.

    python3 firmware/tools/local_sources.py
"""
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "home-assistant-voice.realtime.yaml"
GITHUB = "https://github.com/craig-b/ha-openai-realtime"

COMPONENTS = f"""  - source:
      type: git
      url: {GITHUB}
      ref: ${{firmware_ref}}
      path: firmware/esphome/components
    components: [voice_kit, va_client]
    refresh: 0s"""
LOCAL_COMPONENTS = """  - source: {type: local, path: esphome/components}
    components: [voice_kit, va_client]"""
SOUNDS = f"{GITHUB}/raw/${{firmware_ref}}/firmware/sounds/"
SOUND_COUNT = 16


def main() -> None:
    config = CONFIG.read_text()
    if config.count(COMPONENTS) != 1:
        raise SystemExit(f"{CONFIG.name}: its external_components block isn't the one this rewrites")
    if config.count(SOUNDS) != SOUND_COUNT:
        raise SystemExit(f"{CONFIG.name}: {config.count(SOUNDS)} sound URLs, not {SOUND_COUNT}")
    config = config.replace(COMPONENTS, LOCAL_COMPONENTS).replace(SOUNDS, "sounds/")
    if GITHUB in config:
        raise SystemExit(f"{CONFIG.name}: still takes something from {GITHUB}")
    CONFIG.write_text(config)


if __name__ == "__main__":
    main()
