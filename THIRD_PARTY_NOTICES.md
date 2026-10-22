# Third-party notices

The add-on, the integration and the documentation in this repository are under
the MIT licence in [`LICENSE`](LICENSE). Two parts are other people's work, under
their own licences.

## The Voice PE firmware (`firmware/`)

`firmware/` is a `git subtree` of ESPHome's
[home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)
(through a fork), and is under its licence, the ESPHome License, in
[`firmware/LICENSE`](firmware/LICENSE):

- the MIT License for the Python code and other parts;
- the GNU GPL v3 for the C/C++ runtime code (files ending in .c, .cpp, .h, .hpp,
  .tcc and .ino).

ESPHome also fetches the firmware's `voice_kit` component and its sounds from that
repository when it builds the firmware.

**GPLv3:** firmware built from this repository contains GPLv3 code, so whoever
distributes compiled firmware images must comply with the GPLv3, including giving
access to the source code of the GPLv3 parts.

## Libraries

The add-on and the integration use libraries under their own licences, which are
not copied into this repository: the add-on's are locked in
[`openai_realtime_voice_agent/uv.lock`](openai_realtime_voice_agent/uv.lock), the
integration's tests' in [`integration_tests/uv.lock`](integration_tests/uv.lock).
