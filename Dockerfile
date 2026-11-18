# media-converterr: worker + periodic scan, encoding with Intel QSV through Jellyfin's ffmpeg build
# (VAAPI, QSV/VPL, Vulkan and libplacebo, with Intel's media driver bundled).
FROM debian:trixie-slim

ARG JELLYFIN_FFMPEG=jellyfin-ffmpeg8
# Dolby Vision / HDR10+ metadata tools (pinned release binaries, verified by checksum); mkvtoolnix muxes their raw
# HEVC output with correct timestamps (ffmpeg assigns raw HEVC timestamps in decode order, breaking B-frame order)
ARG DOVI_TOOL_VERSION=2.3.4
ARG DOVI_TOOL_SHA256=1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f
ARG HDR10PLUS_TOOL_VERSION=1.7.2
ARG HDR10PLUS_TOOL_SHA256=06385f37a639d61ba21d4be3150c863846933bc3b58110e094d8fc8f1c2249f2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive \
    PATH=/usr/lib/jellyfin-ffmpeg:$PATH HOME=/tmp XDG_CACHE_HOME=/tmp/.cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 mesa-vulkan-drivers mkvtoolnix pci.ids \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-nld tesseract-ocr-por python3-pil \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian trixie main" \
      > /etc/apt/sources.list.d/jellyfin.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "$JELLYFIN_FFMPEG" \
 && fetch() { curl -fsSL -o /tmp/t.tgz "$1" && echo "$2  /tmp/t.tgz" | sha256sum -c - && tar xzf /tmp/t.tgz -C /usr/local/bin && rm /tmp/t.tgz; } \
 && fetch "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" "$DOVI_TOOL_SHA256" \
 && fetch "https://github.com/quietvoid/hdr10plus_tool/releases/download/${HDR10PLUS_TOOL_VERSION}/hdr10plus_tool-${HDR10PLUS_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" "$HDR10PLUS_TOOL_SHA256" \
 && dovi_tool --version && hdr10plus_tool --version \
 && apt-get purge -y gnupg curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY media_converterr.py media_convert_5.py ocr_subtitles.py forced_subs.py sidecar_names.py settings_store.py encoders.py gpus.py validate.py web_ui.py web.py clear_video_titles.py recover_deleted_entries.py container_entry.sh /app/
RUN chmod 0755 /app/container_entry.sh

# Non-root by default; set `user:` in compose to the owner of your media library, and add the render group.
USER 1000:1000
ENTRYPOINT ["/app/container_entry.sh"]
