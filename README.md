# media-converterr

Event-driven conversion of a Sonarr/Radarr media library on the GPU you have (Intel, AMD, NVIDIA) or the CPU, with
a durable queue, a weekly reconciliation scan and a web interface in the style of Sonarr/Radarr.

## What it does

- **Reacts to Sonarr/Radarr events** (download, upgrade, rename, file delete) through a webhook it registers in
  Sonarr/Radarr itself (or a Custom Script hook), and re-scans the libraries on a schedule to catch anything missed.
- **Re-encodes to a common quality standard on the GPU,** proving at start-up which encoders work on this machine:
  Intel QSV, VAAPI (Intel, AMD), NVIDIA NVENC, AMD AMF, or the CPU (SVT-AV1, x265, x264). By default SDR becomes AV1
  and HDR becomes 10-bit HEVC with its static HDR metadata on every frame and HDR10+ copied from the source
  (`hdr10plus_tool`); each kind of source can instead become HDR10 in AV1, Dolby Vision profile 8.1 (`dovi_tool`), or
  SDR tone mapped with libplacebo. When the GPU lacks a codec, another codec on the GPU (HEVC, H.264) or the CPU.
- **Keeps efficient sources as they are:** HEVC/AV1 sources at or below a bitrate per megapixel of picture (black
  bars excluded) are remuxed, not re-encoded. Dolby Vision is never kept as it is.
- **Keeps only the streams you want:** audio and subtitles in the configured languages (plus the original-language
  audio). Subtitles are extracted next to the video with Bazarr's naming (bitmap ones OCR'd to SRT, kept as `.sup`
  or `.idx/.sub`, or dropped) or kept in the container; forced tracks are merged into the full track.
- **Tracks every file in SQLite** (path, size, mtime, device, inode), so converted files are never redone, and asks
  Sonarr/Radarr to rescan after each conversion so they pick up the new file; tells Jellyfin (optional) at once.
- **Survives restarts:** events and jobs are queued durably, with retries and backoff.

## Requirements

- Docker (recommended; the image bundles Jellyfin's ffmpeg with Intel, AMD and NVIDIA encoder support, Vulkan,
  libplacebo, `hdr10plus_tool`, `dovi_tool`, MKVToolNix and Tesseract), or Python 3.11+ with those tools.
- A GPU with hardware encoding is strongly recommended (see "Validated hardware"); the CPU works but is slow at 4K.
  NVIDIA needs the NVIDIA Container Toolkit (`--gpus all`); AMD AMF needs AMD's AMF runtime (VAAPI works without).
- Sonarr and/or Radarr with API access; Jellyfin optional.

## Validated hardware

Results of `media_converterr.py validate` (real encodes through the converter's own commands; see below). Other GPUs
are expected to work where their capability check passes, but haven't been validated yet: please add yours.

| GPU | Backend | Passed | ffmpeg, kernel |
|---|---|---|---|
| Intel Arc Pro B50 (Battlemage G21, Xe2) | qsv | SDR AV1/HEVC/H.264, HDR10 HEVC/AV1, tone mapping quality and fast, Dolby Vision 8.1 (sample) | 8.1.3-Jellyfin, Linux 7.2 |
| Intel Arc Pro B50 (Battlemage G21, Xe2) | vaapi | SDR AV1/HEVC/H.264, HDR10 HEVC/AV1, tone mapping fast (quality falls back to it) | 8.1.3-Jellyfin, Linux 7.2 |
| (CPU on the same machine) | software | SDR AV1/HEVC/H.264, HDR10 HEVC/AV1, tone mapping quality | 8.1.3-Jellyfin, Linux 7.2 |

Not validated yet: NVIDIA (NVENC), AMD (VAAPI, AMF), Intel before Arc, Dolby Vision profile 5 sources.

## Validate your GPU

```sh
docker build -t media-converterr:latest .
# Intel/AMD: pass the render node and its group; NVIDIA: --gpus all instead
docker run --rm --device /dev/dri/renderD128 --group-add "$(stat -c %g /dev/dri/renderD128)" \
  -v "$PWD/validation":/data media-converterr:latest validate [--sample /media/some-hdr10plus-or-dolby-vision.mkv]
```

It prints PASS/FAIL per encoder and output path, a summary with the row(s) for the table above, and writes a JSON
report. Working with an AI coding agent? `AGENTS.md` has step-by-step instructions for it to test, fix and validate
your GPU and open a pull request with the result (`.github/PULL_REQUEST_TEMPLATE.md`).

## Container

The image bundles Jellyfin's ffmpeg build (VAAPI, QSV, Vulkan, libplacebo and Intel's media driver) on Debian.
Its entry point runs a library scan at start and every `SCAN_INTERVAL_HOURS` (default weekly) next to the queue
worker; any arguments run a single command instead (`docker run ... media-converterr:latest status`).

1. `docker build -t media-converterr:latest .`
2. Copy `media_converterr.example.toml` to `<config>/media_converterr.toml`; with `api_key_file` the API keys are read
   from the mounted Sonarr/Radarr `config.xml` files.
3. Adapt `compose.example.yaml` (paths, user, render group, languages) and start it.
4. Open the web interface, create a login (or continue without one), and on **Settings** check the Sonarr/Radarr (and optional Jellyfin)
   connections, enter the address Sonarr/Radarr use to reach this service, then **Test connections** and
   **Register webhooks**. Sonarr/Radarr must be able to reach the web port (for example a shared Docker network).

## Setup without a container

1. Copy `media_converterr.example.toml` to your config path and fill in the instance URLs, API keys (or
   `api_key_file`) and the library paths as Sonarr/Radarr see them (`remote_root`) and as the converter sees them
   (`local_root`).
2. Make the hook spool folder writable by both the *arr apps and the converter.
3. In Sonarr and Radarr, add a Connect entry of type **Custom Script** that writes the event into the spool (for
   example a small script that dumps its environment into `<spool>/<instance>.<unique>.event`), or that runs
   `media_converterr.py hook --instance sonarr` directly. Events to enable: On Import/Upgrade, On Rename, On File
   Delete (and On File Delete For Upgrade).
4. Run the worker and a periodic scan, for example with the bundled systemd units:
   `media_converterr.py --config <path> systemd install --scan-interval weekly --user <user> --group <group>`.

## Web interface

`web.py` (standard library only) serves the current state (running conversion with progress, queue, estimates),
the queue with retry buttons, history with durations and sizes, a file search with a Re-encode button, statistics (codec and HDR mode across the library,
conversions per day, space saved), errors, Sonarr/Radarr events and the live log. The container starts it on
`WEB_PORT` (default 8090; `0` disables it).

- **Login (optional):** the first visit either creates one account or continues without a login; the Settings page
  turns it on or off and changes the password later. `WEB_AUTH=off` forces it off (for an authenticating proxy in
  front). Without a login, anyone who can reach the page can use it, settings included.
- **Settings:** Sonarr/Radarr instances (URL, API key, library folder as each app and as the converter sees it),
  Jellyfin (optional) and the webhook address. Saved values live in the database and take precedence over the TOML
  file, which stays the first-boot source; an API key left empty keeps the saved one or the one from the file
  (`api_key_file`, environment). The worker picks up saved changes without a restart. API keys are stored in the
  database file in plain text, so protect the data folder like the *arr apps' own config.
- **Hardware:** which encoders work on this machine, on every GPU, each proven by a short test encode: Intel QSV,
  VAAPI (Intel and AMD GPUs), NVIDIA NVENC, AMD AMF and the CPU (SVT-AV1, x265), for AV1, 10-bit and 8-bit HEVC
  (and H.264 for information). Checked again when ffmpeg or the devices change, or on request. NVIDIA (needs the
  container started with NVIDIA's runtime) and AMD AMF (needs AMD's AMF runtime) have not been tested on real
  hardware yet, and VAAPI only on Intel. The CPU encoders work everywhere but are slow at 4K.
- **Conversion:** encoder (automatic: a GPU that passed its test, else the CPU; when the GPU lacks the chosen codec,
  another codec on the GPU or the codec on the CPU), SDR codec (AV1, HEVC, H.264), HDR codec (HEVC, AV1), what each
  kind of source becomes (HDR10/HDR10+, HLG, Dolby Vision: keep HDR, keep HDR10+, Dolby Vision 8.1, or tone map to
  SDR with libplacebo or the GPU's faster filter), subtitles (extracted next to the video or kept in the container;
  bitmap subtitles OCR'd to SRT, extracted as they are, or dropped), (AV1 or HEVC; HDR is always HEVC), quality per encoder, encoder preset, maximum
  resolution, whether efficient sources are kept by remux and the limit for that (Mbit/s per megapixel of picture,
  black bars excluded), and the audio/subtitle languages. Changes apply to files converted afterwards.
- **Webhooks:** **Register webhooks** creates or updates a Webhook connection named `media-converterr` in each
  instance, pointing at `<address>/hook/<instance>?token=<secret>`. Events arrive as JSON and are queued exactly
  like Custom Script events. Remove an older Custom Script connection afterwards, or events arrive twice.

Subtitles in the wanted languages are written as `.srt` files next to the video (Bazarr naming) instead of being embedded; bitmap subtitles (Blu-ray PGS, DVD VobSub) are converted with OCR (Tesseract).

Files the converter produced carry a `MEDIA_CONVERTERR` container tag and are never encoded again on their own (each encode costs quality); the dashboard's Re-encode button is the way to convert one again.

## Commands

| Command | Purpose |
| --- | --- |
| `worker [--once]` | Run the queue worker (hook events, conversions, *arr rescans) |
| `scan [--dry-run]` | Scan the libraries and enqueue new or changed files |
| `enqueue [--force] PATH...` | Queue specific files; `--force` re-encodes a file this converter already produced |
| `status [--json]` | Queue state, without touching media |
| `retry JOB_ID` | Retry a failed or cancelled job |
| `hook [--instance NAME]` | Capture the current *arr Custom Script event |
| `validate [--sample FILE] [--out DIR]` | Test this machine's encoders with real encodes and write a report |
| `init` | Import the database of the standalone converter once |
| `systemd install|remove` | Install or remove the worker and scan units |

`media_convert_5.py` holds the conversion policy and also works as a standalone scanner (see its docstring).
`clear_video_titles.py` and `recover_deleted_entries.py` are maintenance tools for the database.

## Tests

`python -m unittest discover -p "test_*.py"`

## License

MIT. See `LICENSE`.
