# media-converterr

## Maintaining this file

- Keep this guide current, in the same change as the code. It's a register of the project's current state, not a
  logbook: correct stale information in place, no dates or change history.
- Distinguish verified facts from assumptions, and say what hasn't been validated.
- **This file and everything tracked in this repository is public.** Never add credentials, personal data, anything
  about a particular installation (hosts, paths, ports, hardware, service settings, language choices) or the contents
  of anyone's media library. Use placeholders and invented examples. The one exception is the README's "Validated
  hardware" table: GPU models with driver/ffmpeg/kernel versions and test results, nothing about the machine or its
  owner.
- Installation-specific notes belong in `AGENTS.local.md` (gitignored). **If it exists, read it too**: it holds the
  local deployment, tools and standing instructions, and it overrides this file where they differ.

## Working agreement

- Discuss first: investigate, explain, propose options with a recommendation, and change things only after
  confirmation. Read-only investigation is fine.
- Commits, pushes, deployments and service restarts each need their own confirmation unless a standing instruction
  (for example in `AGENTS.local.md`) says otherwise.
- Read `media_converterr.py` (queue, hooks, database) and `media_convert_5.py` (conversion policy) before changing them.

## What this is

Event-driven conversion of a Sonarr/Radarr media library on whatever encoder the machine has (Intel QSV, VAAPI,
NVIDIA NVENC, AMD AMF, CPU), configured from a web interface in the style of Sonarr/Radarr. `media_converterr.py` is
the service (hooks, durable queue, worker, scheduled scan, systemd units, `validate`); `media_convert_5.py` holds the
conversion policy (and still works as a standalone scanner); `encoders.py` the backends, the capability check and
the choice of encoder; `gpus.py` GPU identification; `validate.py` the hardware validation; `settings_store.py`,
`web.py` and `web_ui.py` the web interface. See `README.md` for setup and commands.

**Direction: a generic, publishable converter.** Anyone can run it on their own hardware and configure it from the
web interface. In place: settings, connections and webhook self-registration in the web interface; capability check,
encoder backends and GPU identification; conversion policy settings (codecs, dynamic range, quality, resolution,
remux rule, languages, subtitles); hardware validation (`validate`) with instructions for contributors' agents.
Still open: a calibration tool and a common quality scale (one level mapped to per-encoder values; the same
`global_quality` number is not the same quality on different encoders; only Intel QSV HEVC/AV1 are measured).

## How it works

- **Webhooks:** Sonarr/Radarr Webhook connections POST JSON to the web interface's `/hook/<instance>?token=<secret>`
  (`web.py` `receive_hook`; token in `app_settings.hook_token`). `webhook_environ()` turns the JSON into the variables
  a Custom Script gets, and `capture_hook_event` queues it, so both routes share `process_hook_event`. The Settings
  page's Register button creates or updates a Webhook connection named `media-converterr` through the *arr API
  (`register_webhook`, from the Webhook schema; events: download, upgrade, rename, file delete, delete for upgrade,
  series/movie delete) and warns about a Custom Script connection to this service (events would arrive twice).
  Payload field names follow the *arr Webhook format (`series.path`, `movie.folderPath`, `episodeFile`/`movieFile`,
  `renamedEpisodeFiles`/`renamedMovieFiles` with `previousPath`, `deletedFiles`): covered by unit tests with
  hand-written payloads, **not yet verified against live Sonarr/Radarr**.
- **Settings store (`settings_store.py`):** table `app_settings` (key → JSON). `connections` (instances, Jellyfin,
  `public_url`) saved from the web interface replaces the TOML's (`apply_stored_connections`; an empty API key falls
  back to the file's key for the same name). The worker compares `settings_store.version()` each loop and reloads
  its settings when it changes. Also holds the webhook token, the login (PBKDF2-SHA256) and the session key.
- **Events (Custom Script route):** a Sonarr/Radarr Custom Script hook writes each event (its environment) as a file into the hook spool
  (`hook_spool_dir`), or runs `media_converterr.py hook` directly. The worker claims each `.event` file (renames it to
  `.processing`), stores only the `sonarr_*`/`radarr_*` variables as a queued `hook_events` row and deletes the file;
  a failure renames it to `.failed.<time>`. Test events are ignored.
- **Paths:** event paths are *arr paths; `translate_remote_path` maps each instance's `remote_root` to its
  `local_root`.
- **Jobs:** files are matched by path plus size, mtime, device and inode; a changed file counts as new. Every file
  without an `ok` row is converted. After a conversion the worker sends `RescanSeries`/`RescanMovie`, so the *arr app
  picks up the new file (a rescan doesn't fire a download event, so there's no loop).
- **Deletes:** file-delete events drop the file's row and cancel queued jobs; series/movie delete events (if sent)
  clear or unlink the rows under that folder.
- **Conversion policy (`media_convert_5.py`):** every file is re-encoded (quality/size standard). SDR → AV1
  (`av1_qsv`, `GQ` 20); HDR10, HLG and Dolby Vision → 10-bit HEVC (`hevc_qsv`, `HEVC_GQ` 16, `hevc_metadata` colour
  signalling; static HDR10 metadata verified on every frame). `output_codec(mode)` decides; `validate_output` checks
  the codec. **HDR10+:** after the encode, `inject_hdr10plus` extracts the source's HDR10+ JSON, injects it into the
  HEVC stream (`hdr10plus_tool`), muxes the raw stream with `mkvmerge` (it derives timestamps from the stream; ffmpeg
  numbers raw HEVC frames in decode order, which broke B-frame order and crashed players) together with the encode's
  other tracks, and verifies: equal frame counts, HDR10+ present, and packet timestamps identical to the encode's. It
  never fails the conversion (falls back to plain HDR10 with a warning). No AV1 tool can carry HDR10+, hence HEVC for HDR;
  audio in the wanted languages plus untagged tracks plus `disposition:original=1` tracks (else the first audio track);
  text subtitles in the wanted languages are **not embedded**: they are written as sidecar files `<name>.<lang>[.hi][.forced].srt` (Bazarr naming; codes en, nl, pt, pt-BR) from the same ffmpeg pass (`subtitle_extractions`, `subtitle_output_args`) and installed after the video (`install_subtitles`). A subtitle from the release outranks a downloaded one (source priority: extracted > downloaded > produced), so an existing file of that name is moved to `<database dir>/replaced-subtitles/<path>` and replaced. Duplicate-name subtitles are not extracted. **Untagged subtitle streams** (a lone Blu-ray `.m2ts` carries no language tags; they live in the disc's playlist files) are extracted last as `.und<index>` and named after the language of their text (`identify_untagged`): a text stream is read as is, a bitmap stream OCR'd from its first 80 cues with `eng+nld+por`; the language comes from `sidecar_names.detect_language` (en/nl/pt word markers, with es/fr/de/it recognised so they aren't mistaken for one of those), and a stream that is unclear, unwanted, or whose name a tagged stream took is dropped. A track with 8% or more cues holding sound descriptions or speaker labels (`[door slams]`, `MAN:`) is marked HI. European vs Brazilian Portuguese needs more text than a sample, so a Portuguese bitmap stream is OCR'd in full first. Verified on a UHD Blu-ray remux's 30 PGS tracks with their tags hidden: English, English SDH (`.en.hi`), Dutch, Brazilian and European Portuguese named right; French/German/Italian recognised and dropped; Spanish and the rest dropped as unclear. About 15 s of OCR per untagged bitmap track. **Language detection learns from the library** (`language_profiles.py`, user instruction: keep it growing as media is re-downloaded; no model): every tagged text subtitle a conversion or the subtitle pass handles, wanted or not (other languages are extracted for this alone, one track per language, until a language has 200), is counted into a profile of its language (1–3 letter sequences inside words). An untagged track goes to the nearest profile (out-of-place distance) only when that language is measured: at least 5 learning tracks and 2 held-back test tracks (one in five, by content hash), recall and precision at least 0.95 on them; the track must also be no further than 1.15 × that language's furthest correct test track (so an unlearned neighbour such as Afrikaans is refused rather than taken for Dutch) and 4 % nearer than the runner-up. Otherwise the built-in word lists (`sidecar_names.detect_language`: en, nl, pt and, to refuse them, es, fr, de, it) decide; Portuguese is split into pt/pt-BR by phrasing either way. Untagged tracks are never learned from. Dropped untagged tracks (unclear or unwanted) keep their text in `<database dir>/untagged-samples/` for the on-demand audit. `languages [--learn-library]` reports each profile (tracks, tests, recall, precision, used) and can seed the profiles from the library's sidecars (language from the name). Measured on this library's sidecars: en/nl/pt 160–165 learning and 35–39 test tracks each, recall and precision 1.0. **Blu-ray bitmap subtitles (PGS)** are copied out as `.sup` in the same pass and OCR'd (`ocr_subtitles.py`: PGS parser and RLE decoder, Pillow rendering, Tesseract `eng`/`nld`/`por`, `--psm 6`); a text stream wins a file name over a bitmap one. An OCR result is installed only at a mean word confidence of 75 or more, otherwise it is saved to `<database dir>/ocr-rejected/<path>` and any existing subtitle stays. **DVD bitmap subtitles (VobSub)** are copied into a subtitle-only `.mks`, turned into `.idx`/`.sub` by `mkvextract` and decoded (SPU control sequences, interlaced nibble RLE, `.idx` palette; an unknown stop time 0xFFFF ends at the next subtitle) before the same OCR. Verified with generated PGS (96% confidence, exact text and timing) and with ffmpeg-encoded DVD subtitles made from it, including the converter's own OCR step with production temp names (`mkvextract` replaces the extension of the output name it's given, so the step passes `<base>.idx`). A raw bitmap stream whose OCR failed is kept in `ocr-rejected/`; `ocr-retry [--dry-run]` runs OCR on those again and installs the results like a conversion (forced folded, existing subtitle backed up), leaving a stream whose video is gone or that fails again. Wanted languages
  come from `MEDIA_CONVERT_AUDIO_LANGUAGES` / `MEDIA_CONVERT_SUBTITLE_LANGUAGES` or `media_convert.local.toml`;
  empty means keep every language.
- **Low-bitrate efficient sources are remuxed, not encoded** (user decision; automatic jobs only — a manual Re-encode from the dashboard or `enqueue --force` always encodes): `remux_decision()` picks a remux when the source video is HEVC or AV1 at or below `REMUX_MBPS_PER_MPX` (default 1.8 Mbit/s per megapixel: 15 for a full 4K frame, 3.7 for 1080p; user decision) measured on the picture without black bars (`picture_area`: ffmpeg cropdetect on 2 s at five positions from 20 to 80 %, the largest area wins, a few seconds per file; only run when the full frame alone would allow the remux, since bars can only raise the per-pixel rate; films whose aspect ratio changes can be undercounted, which only leans toward encoding), except Dolby Vision (any profile; user decision: DV is never kept as it is) and unknown bitrates. `build_remux_cmd()` copies video and audio, applies the usual track filter, keeps chapters and metadata (HDR10+ stays in the stream) and tags `remux-<codec>-<mode>`; the worker checks codec, colour signalling and frame count and records `remuxed-<codec>-<mode>`.
- **No forced subtitles in the library** (user decision): a forced track (foreign dialogue, signs) is merged into the regular and HI tracks of its language (`forced_subs.py`: a cue is added when no cue within 1 s says the same thing; blank cues are ignored), then archived in `<database dir>/forced-subtitles/` and not installed. A library track that changes is backed up to `replaced-subtitles/` first. **A "forced" track of 500 or more cues (`FULL_TRACK_CUES`) is a mislabelled full track** (some releases flag their only track of a language as forced; genuine ones seen had at most about 300 cues): without a regular track of that language from the same release it is installed as the regular track, replacing a downloaded one with a backup; next to a release regular track it is merged as usual (a film half in another language can have a long genuine forced track). `fold-forced [--dry-run]` applies this to `.forced.srt` files already in the library; there the origin of an existing regular track is unknown, so a long forced sidecar next to one is left in place and counted as `review`. The reverse also happens: **a regular track with fewer than 150 visible cues and under a quarter of the video's longest track (`SHORT_TRACK_CUES`, `SHORT_TRACK_SHARE`; pending or already-installed tracks count) is an unflagged forced/signs track** (`mark_short_tracks_forced`, run at the start of the fold): it is relabelled `.forced` and merged or archived like any forced track. Seen in WEB releases whose only English or Portuguese track held signs or the translated foreign-language scenes; installed, it hid the missing subtitle from Bazarr. Only pending tracks are relabelled, never library files (a short downloaded file is a quality matter for subtitlerr). **Tracks tagged Portuguese whose text is Brazilian are installed as `.pt-BR`** (`relabel_brazilian_tracks`, same markers as the sidecar repair; skipped when the release has its own `.pt-BR` track): releases tag both variants `por`.
- **An empty extraction from a stream with content stops the job** (`check_text_extractions`): the original stays untouched. A stream that really has no packets (forced tracks without lines) passes.
- **The remux duration check uses video/audio stream durations**, not the container's: a subtitle ending after the picture made the container shorter after removal and failed the check.
- **Subtitle name inventory (`sidecar_inventory` table):** every scan, subtitle install and repair records each
  properly named subtitle by folder and SHA-1 of its content. The rename repair restores a mangled file's exact
  previous suffix from it, before any other rule: Sonarr/Radarr drop `pt-BR` entirely (`.pt-BR.hi.srt` becomes
  `.hi.srt`) or turn it into `.BR.pt` (restored as `pt-BR` by `tagged_language` when the inventory has no entry), and
  on a rename they rewrite the extra files of the whole series, so the hook repairs every video in the series/movie
  folder, not only the renamed ones (all seen live). Detecting the language from the text is the last resort.
- **Subtitles of a renamed output are protected** (`protect_sidecars`, table `protected_sidecars`): when the output
  path differs from the source (`.m2ts`/`.mp4` → `.mkv`), Radarr and Sonarr see the old file missing from disk and
  delete the extra files they track with it, which are the subtitles they renamed and so carry the new output's name
  (seen live: three films lost their `.en`/`.nl`/`.pt-BR` files; with the recycle bin off that deletion is
  permanent). Each subtitle is copied to `<database dir>/protected-sidecars/` after the swap and put back if it
  disappears while the output exists, until 15 minutes after the file-delete webhook for the source (24 h without
  one). The worker checks on every idle poll and right after a delete event. Subtitles the converter moves away
  itself (European Portuguese, archived forced tracks) are forgotten first. Covered by unit tests; not yet seen
  restoring live.
- **Sidecar names are repaired after a Sonarr/Radarr rename** (`sidecar_names.py`, `repair_sidecars`): the *arr apps move sidecars with the video but only know the tags forced/foreign/default/cc/psdh/sdh and write back a two-letter code, so `.en.hi.srt` became `.hi.srt` (Hindi), `.pt-BR.srt` a bare `.srt` and a clash got a number (`.1.hi.srt`); verified with Sonarr's Rename and its source. Jellyfin and Bazarr read the original names correctly (`hi` next to a language is hearing-impaired; `pt-BR` is its own language), so the names are kept and repaired instead. The Rename hook event (sent after the move, with old and new paths) first moves sidecars still named after the previous path to the new name (the *arr apps only move the sidecars on their record, and after a repair that record holds the mangled names, so repaired files stayed behind; verified live), then runs the repair for each renamed video: a sidecar whose suffix isn't `.<lang>[.hi][.forced]` (with a flag word in place of the language — `hi`, `sdh`, `cc`, `forced`, `foreign`, `default` — counted as mangled) gets its language from the text (en/nl/pt word markers, 20 needed and twice the runner-up; pt vs pt-BR by the same markers as subtitlerr's relabelling) and its HI/forced flags from the surviving tags. A copy whose proper name already holds the same lines (timing may differ; the properly named copy is kept, being the one Bazarr knows and syncs) goes to `<database dir>/odd-sidecars/`; an unclear language or a different text under the proper name is left with a warning. A sidecar of another video with a longer name in the same folder is never touched. With `[jellyfin]` configured (key from the environment), changed items are sent to Jellyfin's `/Library/Media/Updated` (what the *arr connection sends) so it rescans them at once; without it Jellyfin catches up at its next scan. The same notification goes out after every conversion or remux (the replaced path as `Deleted` when the output name differs), after the subtitle pass rebuilds a file (once its sidecars are in place) and after `ocr-retry` installs a subtitle: an *arr rescan fires no event, so their Jellyfin connection stays silent, and Jellyfin's file watcher can miss changes (restarts, watch limits). `fix-sidecars [--dry-run] [PATH...]` runs the same repair over the library (paths may be videos or folders). A real Hindi `.hi.srt` would be seen as mangled but stays as is (its text matches no known language).
- **Scans skip hidden files** (names starting with `.`): they are temp or staging files, never library videos.
- **Subtitle pass (`extract-subtitles [--dry-run] [--limit N] [PATH...]`):** moves embedded subtitles out of existing library files: wanted ones become sidecars exactly as in a conversion (bitmaps OCR'd, release replaces download with a backup), then the file is remuxed without subtitle streams (video, audio, attachments, chapters, metadata copied; written beside the original as a hidden `.<name>.partial` (`-f matroska`; not a video extension, so Sonarr/Radarr never import it — an earlier `.strip.tmp.mkv` was imported as an episode file and Bazarr downloaded subtitles for it) and swapped in by rename after checking stream counts and duration; the HDD-to-SSD-and-back variant was slower because of the copy back). Text subtitles are installed at the swap; bitmap OCR runs on one background thread while the next file is remuxed (at most one pending), and a raw bitmap stream whose OCR fails is kept in `ocr-rejected/`). Files without subtitle streams are skipped after an ffprobe; files whose subtitles are all in unwanted languages are still remuxed, so no library file keeps embedded subtitles (user-confirmed). Takes the GPU lock per file so it never overlaps a conversion, keeps an `ok` database row in step (no re-encode), and queues an *arr rescan.
- **Own outputs are never re-encoded automatically** (user decision): every output carries a `MEDIA_CONVERTERR=<codec>-<mode>` container tag (`OUTPUT_TAG`, `output_tag()`); `converter_output()` also counts a file as its own output when its database row is `ok` with an `encoded-*` note and the size is unchanged (outputs from before the tag, touched since). The scan and the worker skip such files and refresh the row. Only a forced job re-encodes one: the dashboard's **Re-encode** button (Files page, History) or `enqueue --force` (`jobs.force`, added by migration).
- **GPU lock:** with `GPU_LOCK` set, the worker tries an advisory `flock` on that file before claiming a conversion and holds it for the job (`GpuLock`), so other GPU users (for example a model server's clients) can take turns with it. While the lock is taken, conversions stay queued; hook events and callbacks still run.
- **Configuration:** a TOML file (`media_converterr.example.toml`); defaults are container-style paths (`/config`,
  `/data`, `/hooks`, `/media/...`). The real config holds API keys and stays out of git.

- **Encoder backends (`encoders.py`):** `build_cmd` asks `encode_plan(mode)` (→ `encoders.plan`) for backend, device,
  codec and bit depth, then the backend for hardware init, decode, filter chain and encoder options; the rest of the
  command (stream mapping, colour tags, metadata bitstream filter, GOP, tag) is shared. `output_codec(mode)` is the
  plan's codec. Backends:
  - `qsv` (Intel): the converter's original pipeline, kept byte for byte by `testdata/build_cmd_qsv.json` (28 cases:
    every HDR mode, both SDR codecs, with and without software decoding). 8-bit HEVC (`main`, nv12) for GPUs
    without 10-bit. Verified on Intel Arc.
  - `vaapi` (Intel via iHD/i965, AMD via radeonsi): VAAPI decode/scale/encode; ICQ with the GPU quality values when
    the driver offers it (found by the check), else CQP with the same numbers. Verified on Intel Arc (HDR10 HEVC
    keeps PQ/BT.2020 and the static metadata; SDR AV1); **untested on AMD**; quality values not calibrated.
  - `nvenc` (CUDA decode/scale, `-cq`) and `amf` (CPU decode/scale, CQP): written from the ffmpeg documentation and
    shipped **untested** (user decision); labelled so in the web interface. HDR10 static metadata unverified there.
  - `software`: SVT-AV1 (CRF, QSV preset names mapped to SVT numbers) and x265 (CRF, main10,
    `hdr10-opt:repeat-headers` for HDR), CPU scaling to 10-bit; verified here on 10 s clips (HDR10 metadata kept);
    4K speed on this CPU at preset slow: x265 about 0.07× real time, SVT-AV1 about 0.37×. CRF defaults (25 AV1, 18
    HEVC) provisional.
  - Dolby Vision profile 5 needs libplacebo on Vulkan for every backend; CPU-fed backends render on Vulkan and
    download the frames.
  - **Capability check:** `detect()` proves each backend × render node × codec × bit depth (AV1 10-bit, HEVC 10/8-bit,
    H.264 8-bit for information) with a short test encode (listed encoders prove nothing: this build lists NVENC and
    AMF without their drivers) and checks libplacebo on Vulkan; stored as `app_settings.capabilities`, re-run at
    worker start when ffmpeg or the devices change or the stored check predates bit depths (`refresh_capabilities`),
    or from the Hardware page.
  - **GPU identification (`gpus.py`):** display adapters from sysfs, named from `pci.ids` (Debian package in the
    image), matched to a family table of documented hardware-encode support (Intel Gen7 to Xe2, NVIDIA Kepler to
    Blackwell, AMD VCE to VCN 5, including chips without an encoder such as GT 1030 and RX 6400/6500). The Hardware
    page shows each GPU, what its family should encode and what the check proved, and explains a gap (driver,
    NVIDIA runtime, AMF runtime, device not passed in). The test encodes stay the proof; the table only explains.
    Identified here: the Intel Xe2 GPU correctly.
  - **Choice (`plan`):** HDR needs 10-bit HEVC; SDR the configured codec (10-bit, or 8-bit HEVC). Automatic tries
    GPUs in the order QSV, NVENC, VAAPI, AMF on every device; when no GPU encodes the SDR codec but one encodes HEVC,
    SDR switches to HEVC on that GPU instead of the much slower CPU; the CPU last. A forced backend is used even if
    its check failed. Without a stored check: QSV on the default device (the pre-backend behaviour).
- **Dynamic range and codecs (`output_spec`):** each source is classed SDR, HDR10/HDR10+, HLG, Dolby Vision 7/8 or
  Dolby Vision 5, and the settings choose its output: HDR10 (with HDR10+ when present) or HDR10 only or SDR; HLG or
  SDR; for DV 7/8 Dolby Vision 8.1 (with HDR10+ when present), HDR10 from the base layer (with or without HDR10+) or
  SDR; for DV 5 HDR10 (rendered) or SDR. **Defaults keep the source's HDR format** (user decision: HEVC was chosen
  for HDR so dynamic metadata survives); DV 5 is the exception (its base layer isn't HDR10, so it is rendered). SDR codec AV1, HEVC or H.264; HDR codec HEVC or AV1 (HDR10+ and DV force HEVC). Verified on
  Intel Arc with real 8 s clips: HDR10 in AV1 (QSV and SVT-AV1) keeps PQ/BT.2020 and the mastering-display and
  light-level metadata; DV 8.1 (`inject_dovi`: `dovi_tool -m 2 extract-rpu` from the piped source stream,
  `inject-rpu`, mkvmerge, then checks: same frames, same timestamps, profile 8 read back) on a DV 8 Blu-ray clip;
  SDR via libplacebo (BT.2390, perceptual gamut, scaling inside libplacebo) and via `tonemap_vaapi`, as AV1, HEVC and
  H.264 (1080p), BT.709 signalling. **Tone mapping speed** (4K, Intel Arc): Quality about 0.18× real time (CPU-decoded
  frames uploaded to a separately opened Vulkan device; a Vulkan device derived from VAAPI made uploads about 7×
  slower), Fast about 4×. Tone mapping never remuxes. Output tags/notes record the codec and the output mode
  (`sdr`, `dovi-8.1`, or the source mode). H.264 is an output too (SDR, 8-bit), and part of the GPU fallback.
- **Picture settings:** `KEYFRAME_SECONDS` (default 2: `-g` = seconds × 60 and forced keyframes every N s; the
  default reproduces the earlier commands), `LOOKAHEAD` (default 100: QSV `look_ahead_depth`, NVENC `rc-lookahead`
  up to 32, x265 `rc-lookahead`, SVT-AV1 `lookahead` up to 120, x264 `rc-lookahead`), `DEINTERLACE` (default on: a
  source whose `field_order` is tt/bb/tb/bt is deinterlaced: `deinterlace_vaapi` (motion adaptive) on QSV/VAAPI,
  `bwdif_cuda` on NVENC, `bwdif` (send_frame) on the CPU and before libplacebo renders; the VAAPI deinterlacer drops
  the last frame, which validate allows for interlaced sources only) and `CROP` (default off: `crop_rect` from the
  black-bar detection, centred, even, ignored under 2 %; QSV crops and scales in one `vpp_qsv` step, VAAPI and NVENC
  crop on the CPU (CPU decode), CPU backends and renders crop before scaling; never for Dolby Vision 8.1 output).
  Verified with `validate` on synthetic interlaced (576i MPEG-2) and letterboxed (1080p with 140 px bars) sources.
- **Job steps and progress:** each conversion lists its steps (`job_steps`: Converting/Remuxing, Injecting HDR10+,
  Injecting Dolby Vision, Checking output, Installing, Subtitles) and records the current one in
  `<work>/progress.json`; ffmpeg writes `-progress` to `<work>/progress.txt`. The queue page shows "(n/N) step",
  and during the conversion step the real position over the source's duration, the speed and the time left
  ("finishing" once ffmpeg reports the end). At worker start, temporary files and injection folders of an
  interrupted job are removed (`remove_stale_work_files`).
- **European Portuguese setting (`EUROPEAN_PT`):** `always` (default), `fallback` (a European `.pt` subtitle only
  for videos without a Brazilian one) or `never`. Releases tag both variants `por`; Brazilian text is relabelled
  `.pt-BR` first (`relabel_brazilian_tracks`), then `apply_portuguese_policy` drops pending `.pt` tracks as set, and
  the scan's `retire_european_portuguese` moves library `.pt` sidecars made redundant by a Brazilian one (or all, with
  `never`) to `replaced-subtitles/`. Bazarr has no fallback: European Portuguese must also leave its language
  profile, or it downloads them again.
- **Subtitle settings:** `SUBTITLE_MODE` `extract` (default: sidecars, as described below) or `embed` (wanted-language
  streams mapped into the output and copied, `mov_text` converted to SRT; `subtitle_extractions` returns nothing and
  the subtitle pass is skipped, so nothing is stripped). `BITMAP_SUBTITLES` when extracting: `ocr` (default),
  `sidecar` (`keep_bitmap_sidecars`: PGS as `<lang>.sup`, VobSub as `<lang>.idx` + `.sub` via mkvextract; forced
  folding only touches SRT; `place_subtitles` does OCR-or-keep, fold, install) or `drop`. Verified: embed on a real
  Blu-ray clip kept exactly the wanted PGS tracks (3 of 30). Jellyfin's handling of external `.sup` not verified.
- **Conversion settings (web interface):** a `conversion` document in `app_settings` sets the policy module's globals
  through `apply_conversion_settings` whenever settings load (worker start and reload, scan, web): SDR output codec
  (`SDR_CODEC`, AV1 or 10-bit HEVC; HDR is always HEVC), AV1 and HEVC `global_quality`, QSV preset, maximum resolution
  (`TARGET_WIDTH`/`TARGET_HEIGHT`), remux on/off and limit, audio and subtitle languages (default: the environment
  variables or `media_convert.local.toml`). `validate_conversion_settings` checks every value.
- **Quality values:** the same `global_quality` number is not the same quality on both encoders. Measured on 30 s 4K
  HDR clips (PSNR-Y against the source, mean and worst second) from a grainy film remux, a clean digital WEB-DL and a
  low-bitrate web episode: `hevc_qsv` 16 equals or slightly beats `av1_qsv` 20 at similar or lower bitrate; at 20 it
  was 1.5–2 dB worse, most on grain and dark gradients. Hence `HEVC_GQ` 16. XPSNR proved unreliable on frame sizes
  that aren't a multiple of 8 (a clip scored 13 dB against itself); libvmaf isn't in the bundled ffmpeg.

## Validating a GPU (for agents working on a clone)

This project is meant to run on any GPU with hardware encoding, but only some have been tested (see the README's
"Validated hardware"). If you are an AI agent asked to make it work on, or validate, the machine you run on:

1. **Build and run the tests:** `docker build -t media-converterr:latest .`, then
   `docker run --rm --entrypoint "" -v "$PWD":/src:ro -w /src media-converterr:latest python3 -m unittest discover -p "test_*.py"`.
2. **Pass the GPU in** and validate: Intel/AMD `--device /dev/dri/renderD128 --group-add <render gid>` (find the node
   and the group with `ls -l /dev/dri`); NVIDIA `--gpus all` (NVIDIA Container Toolkit). Run
   `docker run --rm <devices> -v <a writable folder>:/data media-converterr:latest validate` (optionally
   `--sample /path/to/a/real/HDR10+-or-Dolby-Vision/file`, mounted read-only). It prints PASS/FAIL per test, a summary
   and README row(s), and writes a JSON report under `/data/validation/`.
3. **Read the Hardware page or the report first:** the family table (`gpus.py`) says what the GPU should encode; a
   capability test that fails on capable hardware usually means a missing driver, runtime or device, not a code bug
   (see `gpus.HINTS`). Fix the environment before the code.
4. **Fix the code where it is wrong for this hardware:** pipelines live in `encoders.py` (one backend class each:
   hardware init, decode, filters, encoder options, test commands), the choice of encoder in `encoders.plan`, the GPU
   families in `gpus.FAMILIES`. Keep other vendors' commands unchanged: the QSV snapshot test must still pass unless
   the change is deliberate (then regenerate `testdata/build_cmd_qsv.json` and explain why). Add unit tests for every
   new command shape (they must not need a GPU). Prove each change with real encodes; check colour signalling, HDR
   metadata on the frames and frame counts, as `validate.py` does.
5. **Repeat until `validate` passes**, or document what still fails and why under Known issues.
6. **Update the README table** with the printed row(s) (GPU model, backend, passed paths, ffmpeg and kernel versions;
   nothing about the machine or its owner) and this file's backend notes.
7. **Open a pull request** with `.github/PULL_REQUEST_TEMPLATE.md` filled in: the validation summary, the driver
   versions and the checklist. One GPU family per pull request keeps reviews simple.

Never commit media files, sample paths, hostnames, API keys or local configuration (`*.toml` other than the example,
`AGENTS.local.md`, databases are ignored by git for that reason).

## Implementing Windows support (for agents)

Status: untested. Docker Desktop runs the Linux image (CPU encoding; NVIDIA through `--gpus all`); Intel/AMD GPUs are
not reachable from containers on Windows (no `/dev/dri`); native Windows isn't supported. Work in this order, each
step validated before the next, and keep Linux behaviour unchanged (all tests, including the QSV snapshot):

1. **Docker Desktop, CPU and NVIDIA:** run the unit tests and `validate` in the container on Windows (NVIDIA:
   `--gpus all` with the NVIDIA driver's WSL support). Fix what fails in `encoders.py` (NVENC pipeline: CUDA decode,
   `scale_cuda`, `-cq`; HDR10 static metadata on the frames is the part most likely to need work). Check the
   path mapping with Windows paths on the Sonarr/Radarr side (`translate_remote_path`, `remote_root` like
   `D:\Media\TV`: backslashes and drive letters). Add a README table row marked "Windows (Docker Desktop)".
2. **Intel/AMD under WSL2 (optional, experimental):** Mesa's `d3d12` VA-API driver exposes the GPU's encoders through
   `/dev/dxg`. It needs that driver in the image (the bundled Jellyfin ffmpeg ships iHD/i965/radeonsi only), the
   device passed in, and a backend or render-device handling for `/dev/dxg` in `encoders.py`/`gpus.py` (no PCI sysfs
   there). Prove it with `validate` before enabling it by default.
3. **Native Windows (no Docker), a larger port:**
   - Portable process and GPU locks (`fcntl.flock` in `media_converterr.py`/`media_convert_5.py`: e.g. `msvcrt.locking`
     behind a small helper); `pwd`/`grp` and the systemd commands only on Linux; signals that exist on Windows.
   - Windows encoder pipelines: QSV through D3D11 (`-init_hw_device d3d11va=dx -init_hw_device qsv=qs@dx`,
     `-hwaccel d3d11va`/`qsv`, `scale_qsv`/`vpp_qsv`), AMF with D3D11 decode, NVENC unchanged (CUDA exists on
     Windows); libplacebo on Vulkan works on Windows for renders. A new backend class each, chosen by
     `encoders.plan` from the capability check (which already proves what works).
   - Tools on Windows: ffmpeg with those encoders, `hdr10plus_tool`, `dovi_tool`, MKVToolNix, Tesseract (PATH or
     configured locations); paths and temporary files via `os.path`/`tempfile` (no hard-coded `/tmp`, `/data`).
   - A way to run it as a service (for example a scheduled task or NSSM) documented in the README.
   - Unit tests that cover the Windows command shapes without needing Windows; `validate` on real Windows hardware.
4. **Open a pull request per step** (`.github/PULL_REQUEST_TEMPLATE.md`), with the validation summary and the
   Windows version, GPU driver and ffmpeg build.

## Known issues / open items

- **Brazilian Portuguese naming:** `.pt-BR.srt` is the name Jellyfin maps to "Portuguese (Brazil)" (language
  `pt-br`) and Bazarr to `pb`; some TV clients still print the raw "pt-br" because Brazilian Portuguese has no
  three-letter code of its own. `.pob.srt` (OpenSubtitles' code) was tried: Bazarr reads it as Brazilian, but
  Jellyfin and Wholphin don't recognise it ("pob - Undefined"), so `.pt-BR` stays.
- **Blu-ray `.m2ts` streams carry no language tags** (they live in the disc's playlist files): every audio track is
  kept (dubs and commentaries included), as untagged audio always is. Subtitles are named from their text (see
  above); detecting audio languages isn't implemented. Measured on a UHD Blu-ray remux with 30 PGS tracks, tags
  hidden: English, Dutch and Brazilian Portuguese named correctly, French/German/Italian recognised and dropped,
  every other language dropped as unclear; about 35 s of OCR per track (150-cue sample). Limits seen: the Iberian
  Portuguese track also read as `pt-BR` (the sample had too few variant markers; with a Brazilian track present it
  loses the name and is dropped, as the European Portuguese fallback setting wants anyway), and the English SDH
  track wasn't recognised as HI (few sound descriptions in its first cues), so next to a plain English track it is
  dropped.

- **Dolby Vision → HDR10+ not implemented:** DV output choices are HDR10 from the base layer (keeping HDR10+ when the
  source also has it), Dolby Vision 8.1, or SDR. Generating HDR10+ from the RPU's L1 values is still a proposal.
- **HDR10+ and Dolby Vision injection on a full WEB-DL carrying both** is not yet verified. Refusals seen on clips
  cut with ffmpeg were cut artefacts (video starting at 0.042 s, leading open-GOP frames); `validate` therefore cuts
  samples with `mkvmerge --split parts:` and retries until decoded frames match packets, and with such a cut Dolby
  Vision 8.1 injection verifies.
- **VAAPI encoders can't take libplacebo frames** (ffmpeg 8.1: hwupload from a Vulkan filter device fails), so the
  VAAPI backend tone maps with the GPU's own filter (Fast) and sends Dolby Vision profile 5 to the CPU encoder.
- **Fast tone mapping (`tonemap_vaapi`) crushes dark scenes** on Intel Arc (night scene nearly black where libplacebo
  keeps detail), hence Quality by default.
- **Dolby Vision profile 5 on a real sample:** not tested (no sample); its render path changed to CPU-decoded frames
  through Vulkan because mapping VAAPI frames into Vulkan segfaulted on Intel Arc (tested with an HDR10 source).
- Files converted by earlier versions (HDR to AV1, dynamic metadata dropped) keep that state; re-downloading restores
  HDR10+ or Dolby Vision.

- Files encoded before the stream language filter still carry unwanted tracks; a stream-copy remux would fix them,
  but the database row must be updated afterwards or the new size/mtime triggers a full re-encode.
- Files encoded by an older version were tone-mapped from HDR to SDR; only re-downloading the source restores HDR.
- A forced track archived because no regular track of its language existed yet (for example the translated foreign-language scenes of a WEB release whose only English track was forced) is not merged into a regular track Bazarr downloads later. Proposed: merge archived forced tracks when a regular sidecar appears (Bazarr post-processing or a periodic pass).
- Hearing-impaired subtitle tracks are kept. Proposed: drop an SDH/HI track when the same language has a regular one,
  otherwise strip the HI text during a remux.
- OCR of real disc streams: PGS has run in the library without failures (results not yet reviewed). DVD (VobSub) OCR failed on every stream until the `mkvextract` naming fix; the raw streams kept in `ocr-rejected/` still need `ocr-retry` and a look at the results and confidence.
- **Container (`Dockerfile`, `container_entry.sh`, `compose.example.yaml`):** Debian trixie plus Jellyfin's ffmpeg
  package (it bundles Intel's iHD driver and the VPL runtime; PATH points at it), `mesa-vulkan-drivers` for the
  Dolby Vision profile 5 path, Python 3.13. Entry point: scan at start and every `SCAN_INTERVAL_HOURS`, plus `worker`;
  arguments run one command instead. Verified on an Intel GPU: `av1_qsv` encode, Vulkan + libplacebo, and the
  converter's own command on an SDR and an HDR10 excerpt (static HDR metadata retained). Not yet run as the live
  service.
- API keys can come from the *arr `config.xml` (`api_key_file`), so the config needs no secrets.
- **Web interface (`web.py`, `web_ui.py`):** laid out like Sonarr/Radarr (user request; their dark palette and blue accent, light
  theme with blue header and slate sidebar/toolbar; no page headings, Settings legends, 35 px inputs, help text with an
  info icon, Sonarr button kinds, binary size units): header with logo, file search and a System menu, a
  sidebar of sections (Activity: Queue, History, Events; Library: Files; Failed; Settings: Connect, Conversion,
  General; System: Status, Tasks, Hardware, Logs), a toolbar per page, settings forms with labels left and help
  below, a Show Advanced toggle (remembered in the browser), connection cards with event labels that open edit dialogs with
  Test (unsaved values, JSON), Cancel, Save and Delete; System › Status has Health (setup problems, failed GPU
  tests, failed jobs), Disk Space, About and statistics; Tasks a Scheduled table; Logs level icons. Dark by default, light when the browser prefers it; narrow
  screens get a menu button. `web_ui.py` holds layout, CSS, icons and components; `web.py` the pages, actions and
  routes. Stdlib `ThreadingHTTPServer` started by the entry point on `WEB_PORT` (default 8090, `0` off). Login is
  optional: the first visit (`/setup`) creates one account or stores `{"disabled": true}` in `app_settings.auth`;
  Settings › General turns it on/off or replaces the account. HMAC-signed session cookie (HttpOnly, SameSite=Strict);
  cross-origin POSTs are refused (`Sec-Fetch-Site`, else `Origin`) in either mode; `WEB_AUTH=off` forces it off.
  Tasks can start a library scan (a detached `media_converterr.py scan`). Pages read the database read-only; the
  writes are retry (`retry_job`), Re-encode (`enqueue_job(force=True)`, only for existing files under the configured
  roots), the settings and the webhook endpoint. Verified: every page renders (test and live); the look has not been
  checked in a browser by the agent.
- Python 3.11+, ffmpeg (VAAPI, QSV; libplacebo and Vulkan for Dolby Vision profile 5), SQLite, systemd.
- `hdr10plus_tool` and `dovi_tool` (pinned release binaries), `mkvmerge` (MKVToolNix, muxing raw HEVC), Tesseract with `eng`/`nld`/`por` data and Pillow (PGS OCR); all in the container image.
- Tests: the OCR tests need Tesseract and Pillow and are skipped without them (run them in the image).
- Sonarr and Radarr APIs.
- Tests: `python -m unittest discover -p "test_*.py"`.
