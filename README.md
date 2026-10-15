# Synthesia → PartyKeys bridge

Light a PartyKeys 36 keyboard from Synthesia on macOS or Windows, using a virtual MIDI cable. Requested notes and physically held keys light blue; keys return to dark when neither is active, including notes you miss.

> **Requires PartyKeys keyboard firmware v9 for the supported fast-lighting setup. Update before using the bridge.** Firmware v8.3 repeatedly reset during faster lighting tests. After an owner-reported upgrade to v9, the same keyboard passed tests at 40, 20, and 10 ms between small palette packets. The bridge does not automatically read or enforce the firmware version.

## Requirements

- PartyKeys 36, updated to **firmware v9**. Later firmware versions have not been tested.
- macOS or 64-bit Windows, Synthesia, and a USB data connection to the keyboard.
- Python 3.10 or newer for source installs. The Windows executable includes Python.
- A virtual MIDI bus: IAC on macOS, or a tool such as loopMIDI on Windows.

[PartyKeys Hub](https://apps.apple.com/app/id6797699163) is the official firmware updater. The tested keyboard was updated through Hub on an iPad. Its keyboard update flow requires the keyboard to join Wi-Fi even when controlled over USB. **Hub's app version is separate from the keyboard firmware version.**

This is an independent community project, not an official Synthesia or PartyKeys integration.

## Windows executable

Download **partykeys-windows-x64.zip** from [Releases](https://github.com/teaqu/synthesia-partykeys-bridge/releases/latest) and extract it. Python is not required. The ZIP includes `partykeys.exe`, a launcher, this README, and the demo MIDI file. This is an unsigned console application for Windows x64.

1. Install [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html), create a port named **Synthesia Lights**, and leave loopMIDI running.
2. In Synthesia, use **PartyKeys** for music input. Set **Synthesia Lights** as a music output with **Key Lights → Finger-based channel**. Disable ordinary notes/background/percussion/metronome on that output; keep Synthesia's built-in audio enabled. Disable direct music output to PartyKeys.
3. Double-click **Start Synthesia bridge.cmd**, then start a song in Synthesia. Press Ctrl+C in the console to stop.

The launcher uses cue-only lighting (`--no-press-feedback`), so Synthesia owns the physical MIDI input. Windows MIDI drivers can restrict simultaneous access to the same hardware input. The bridge still lights requested notes and clears missed cues. If your driver supports shared input, omit `--no-press-feedback` to enable physical held-key feedback too.

For custom port names, open PowerShell in the extracted folder:

```powershell
.\partykeys.exe ports
.\partykeys.exe bridge --input "Synthesia Lights" --output "YOUR PARTYKEYS OUTPUT" --no-press-feedback --palette-interval-ms 10
```

Always specify `--input` on Windows; automatic cue discovery currently looks for macOS IAC. Windows has no built-in IAC bus, and the executable does not install a virtual MIDI driver. The workflow tests and runs the executable on Windows Server 2022 with Python 3.12. Physical PartyKeys + Synthesia behavior on a Windows PC has not yet been verified; the hardware tests below were on macOS.

Every push to `main`, pull request, or manual [Windows executable workflow](https://github.com/teaqu/synthesia-partykeys-bridge/actions/workflows/windows.yml) run builds a fresh ZIP and SHA-256 checksum as a downloadable Actions artifact. Releases provide stable downloads of selected builds.

## Install from source

```sh
git clone https://github.com/teaqu/synthesia-partykeys-bridge.git
cd synthesia-partykeys-bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
partykeys ports
```

Dependencies are `mido` and `python-rtmidi`. If no suitable RtMidi wheel is available, installation may require macOS Command Line Tools.

## Set up Synthesia on macOS

1. Open **Audio MIDI Setup → Window → Show MIDI Studio**.
2. Open **IAC Driver**, enable **Device is online**, and add a bus named **Synthesia Lights**.
3. In Synthesia's music-device settings, enable **PartyKeys** as a music input.
4. Enable **IAC Driver Synthesia Lights** as a music output and set **Key Lights → Finger-based channel**.
5. Turn off ordinary song-note, background, percussion, and metronome output on that IAC bus. Keep sound enabled through Synthesia's built-in synthesizer or another audio destination.
6. Disable direct music output to PartyKeys and close any other apps controlling its LEDs.
7. Start the bridge:

```sh
partykeys bridge \
  --input "IAC Driver Synthesia Lights" \
  --output "PartyKeys" \
  --pressed-input "PartyKeys" \
  --palette-interval-ms 10
```

Start a song in Synthesia. Press **Ctrl+C** in the terminal to clear the lights and exit.

Port names can be exact names or unambiguous substrings. Use `partykeys ports` to see your names. Once selected, automatic reconnection stays restricted to those exact names.

## Lighting behavior and speed

- Synthesia cues and physically held keys are **blue** by default.
- A missed cue goes dark when Synthesia sends its release. A key still physically held stays blue until released.
- Physical input is used only for LED feedback; the bridge does not duplicate the notes Synthesia receives.
- Releases clear obsolete lights before new cues are illuminated. Pending updates use the latest state, so old chords do not build up in a queue.
- Live lighting follows Synthesia's key-light messages. There is no verified separate approaching/active transition or arbitrary lookahead in that stream. Timed advance lighting is available only in the independent MIDI-file preview below.

Palette mode (`palette71`, the bridge default) sends up to **three note/color pairs per packet**, at most 16 bytes. It enters lighting mode once, uses explicit per-note off messages, and never sends the global `71 00` all-off command.

| `--palette-interval-ms` | Maximum packet rate | Use |
| --- | --- | --- |
| `10` | 100/second | Fast setting tested after the reported v9 update |
| `20` | 50/second | Slower alternative |
| `40` | 25/second | Slower alternative |
| `100` | 10/second | Diagnostic option |
| `250` | 4/second | Conservative CLI default |

These are minimum send intervals, not measured end-to-end LED latency. Every write, including forced updates and cleanup, is paced. A three-note chord fits in one packet; larger changes take multiple packets. The main loop polls every 10 ms.

The CLI keeps a conservative 250 ms default. **Pass `--palette-interval-ms 10` for the fast setup above.** Slower settings do not make firmware v8.3 a supported configuration.

Palette colors have fixed device brightness; `--brightness` does not dim them. The optional `--protocol rgb15` uses custom RGB colors, one key per 250 ms, and a full startup clear that takes about ten seconds. Faster RGB operation has not been validated after the update.

### Options

```sh
# Change hand colors and make physically pressed keys red.
partykeys bridge --input "Synthesia Lights" --palette-interval-ms 10 \
  --left-color 0050FF --right-color FF6400 --pressed-color FF0000

# Follow Synthesia cues without physical-key feedback.
partykeys bridge --input "Synthesia Lights" --palette-interval-ms 10 --no-press-feedback

# Inspect incoming MIDI and routing.
partykeys bridge --input "Synthesia Lights" --debug
```

Normal note range is **MIDI 48–83** (36 keys). `--base-note` changes the leftmost MIDI note to match a transposed keyboard; out-of-range notes are ignored.

Finger-channel defaults use 1-based MIDI channels: left **2–6, 12**; right **1, 7–11, 13**. Override these with `--left-channels` and `--right-channels`. Synthesia was observed sending finger-channel onsets with releases on common channel 1; a channel-1 release therefore clears that pitch across all cue channels. Physical held-key state remains separate. Note On with velocity zero is a release; CC16 on channel 1, CC120/123, and MIDI Stop/Reset clear applicable cue state.

## Firmware and hardware validation

On the tested unit, Hub initially reported **v8.3**. Faster RGB and palette tests caused multicoloured lighting resets and macOS USB MIDI teardown errors. A separate native CoreMIDI sender reproduced the failure. The official updater's Wi-Fi attempts also triggered repeated USB teardown events while the bridge was stopped.

The owner then reported completing a **v9 update on an iPad**. The version was not independently reread afterward. Tests on the reconnected keyboard produced:

| Palette spacing | Packets | Individual key changes | Result |
| --- | --- | --- | --- |
| 40 ms | 160 | 480 | No USB teardown detected |
| 20 ms | 240 | 720 | No USB teardown detected |
| 10 ms | 240 | 720 | No USB teardown detected |

The tests used bursts of small blue/on and explicit/off packets, with holds between bursts and five seconds of observation afterward. They do **not** establish sustained maximum throughput or stability on every keyboard. The 10 ms setting was then used for the live bridge. No vendor release note confirming the reset fix was available.

There are **53 automated tests** covering framing, rate limits, batch cleanup, stale-cue cancellation, physical input, reconnection, Windows terminal input, and MIDI preview behavior. These tests do not prove physical LED behavior. Port enumeration alone is insufficient: PartyKeys remained listed during some earlier failures.

## Independent MIDI preview

`preview` reads a MIDI file and supplies its own timing and advance lighting. **It is not synchronized with Synthesia.**

```sh
partykeys inspect examples/demo.mid
partykeys preview examples/demo.mid --lookahead 2 --near 1 --brightness 0.6
```

The default preview uses the slower RGB driver: dim hand color for an approaching note, bright hand color nearer its onset, green while active, then dark. For the faster palette driver:

```sh
partykeys preview examples/demo.mid --protocol palette71 --palette-interval-ms 10
```

Palette mode cannot reproduce continuous brightness dimming. Preview commands, entered in its terminal, are `pause`, `resume`, `seek 35.5`, `speed 0.75`, `stop`, `play`, `status`, and `quit`. Lookahead is measured in real seconds at the selected speed; seek positions use song seconds. Pause clears lights; stop returns to pre-roll.

Use `--left-tracks` / `--right-tracks` for zero-based track indices, or `--left-channels` / `--right-channels` for 1-based MIDI channels. Track assignments take precedence. `--latency-ms` advances LED changes only and does not synchronize with another application.

Optional `--audio-output "YOUR SYNTH MIDI INPUT"` plays file notes through a separate MIDI synthesizer. Use a dedicated audio destination: pause, seek, and exit send panic/reset there. File SysEx is not forwarded. Sustained tails and synthesizer envelopes cannot be reconstructed exactly after seeking.

Type 0/1 PPQN MIDI files and tempo changes are supported. Type 2 and SMPTE timing are rejected. Lights follow note durations rather than sustain-pedal tails.

## Troubleshooting

- **Resets or a multicoloured startup pattern:** stop sending LED commands and confirm keyboard firmware v9. Check the USB cable, power, and other LED-controlling apps. Reduce the interval rate while diagnosing. The bridge's automatic reconnection cannot fix a device firmware or power fault.
- **No cues:** check the IAC bus and Synthesia's Key Lights setting. Keep ordinary music output off the lighting bus. `--debug` shows received messages.
- **Delayed lights:** include `--palette-interval-ms 10`; the default is 250 ms. Synthesia controls when cues arrive.
- **Disconnected device:** bridge mode retries missing ports and MIDI errors once per second, then initializes and applies current state. Preview mode stops on disconnection. Already-held physical keys cannot be reconstructed; release and press again.
- **Shutdown:** cleanup is paced and may take several seconds. A second interrupt aborts remaining cleanup. An unplugged device or killed process cannot be cleared. The previous built-in LED animation is not restored.
- **CoreMIDI native error in an automation sandbox:** run in a normal macOS Terminal. Native RtMidi aborts cannot be caught by Python.

## Development

The Windows workflow runs the test suite, builds with PyInstaller on a Windows runner, and then exercises the actual EXE: help, native MIDI port enumeration, MIDI-file inspection, palette dry runs, and piped preview controls. Packaging explicitly includes Mido's dynamically loaded RtMidi backend and the native RtMidi files.


```sh
python -m unittest discover -s tests -v
partykeys test --dry-run --note 60 --seconds 0.1
partykeys preview examples/demo.mid --dry-run --lookahead 0.2 --speed 2
```

`--dry-run` sends no hardware LED output. Preview/test dry runs require no MIDI hardware; bridge dry runs still read a real cue input.

Optional macOS integration check:

```sh
python tests/coremidi_smoke.py
```

It opens only temporary virtual MIDI ports, verifies routing and cleanup, and has a 30-second watchdog. It does not send to the physical keyboard.

The driver handles RGB/palette framing and state; the bridge interprets cue/physical MIDI; connection management handles discovery and recovery; the song and preview modules handle independent playback. Input uses direct RtMidi polling to avoid a callback-thread shutdown hang reproduced during development.

## License

This project is licensed under the [MIT License](LICENSE). Bundled dependencies retain their own licenses, included in `THIRD_PARTY_LICENSES` in the Windows distribution.

## References and acknowledgments

- [luoshichuan-ai's PartyKeys MIDI Light Bridge](https://github.com/luoshichuan-ai/PartyKeys-midi-keyboard-light-bridge): the palette approach, mode-entry-only connection, explicit note-off, and avoidance of global all-off informed this implementation. That project serves browser piano apps; this bridge connects desktop Synthesia through IAC.
- [PartyKeys RGB protocol reference](https://github.com/allen4z/PartykeysProtocol/blob/main/LED_PROTOCOL.md) and [PartyKeys protocol documentation](https://protocol.partykeys.org/): SysEx framing, RGB encoding, and key layout. Device and speaker-relay profiles differ; this implementation targets the directly connected keyboard.
- [SynthesiaKontrol](https://github.com/ojacques/SynthesiaKontrol) and [key_led](https://github.com/touchgadget/key_led): Synthesia key-light channel and release interpretation.
- [Apple IAC setup](https://support.apple.com/guide/audio-midi-setup/transfer-midi-information-between-apps-ams1013/mac) and [Mido RtMidi backend](https://mido.readthedocs.io/en/stable/backends/rtmidi.html).
