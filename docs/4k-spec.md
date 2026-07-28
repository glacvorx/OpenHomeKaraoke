# Best Quality YouTube Download Implementation Plan

## Purpose

Add a best-quality YouTube download path that can download up to 4K video for songs that are not needed soon, while preserving the existing fast default-quality behavior for songs near the front of the queue.

The feature should be safe for live karaoke use:

- Songs in queue positions 1 through 5 should continue using the current fast/default quality.
- Songs added at queue position 6 or later should use best quality.
- Existing local library songs should be upgraded in the background when the feature is enabled.
- Upgrades should never interrupt the current song or the first 5 queued songs.
- The best-quality upgrader should be on by default and can be disabled with a startup flag.

## Current Application Behavior

The `/download` route in `app.py` starts a daemon thread that calls `K.download_video(...)`.

Current download code is in `karaoke.py`:

```python
fmt_hq  = 'bestvideo[height<=1080][vcodec^=h264]+bestaudio[acodec=aac]/bestvideo[height<=1080]+bestaudio'
fmt_std = 'bestvideo[height<=720][vcodec^=h264]+bestaudio[acodec=aac]/bestvideo[height<=720]+bestaudio'
base_opts = ['--fixup', 'force', '--socket-timeout', '3', '-R', 'infinite', '--remux-video', 'mp4']
```

Current meanings:

- Default/standard quality downloads the best available video up to 720p, preferring H.264, plus best audio, preferring AAC.
- Existing high quality downloads the best available video up to 1080p, preferring H.264, plus best audio, preferring AAC.
- Downloads are remuxed to MP4.
- The final library filename is `%(title)s---%(id)s.%(ext)s`.
- The YouTube ID is only persisted in the filename suffix.
- The library scanner indexes media files with extensions in `constants.media_types`: `.mp4`, `.mp3`, `.zip`, `.mkv`, `.avi`, `.webm`, `.mov`, `.m4a`.

Current ready-to-play flow:

1. User submits a download form.
2. Flask starts a background thread.
3. `download_video` marks `self.downloading_songs[song_url] = 1`.
4. yt-dlp downloads into `self.tmp_dir`.
5. The app finds the downloaded file by YouTube ID.
6. The file is moved into `self.download_path`.
7. `get_available_songs()` rescans the local library.
8. If requested, the song is appended to `self.queue`.
9. The player loop pops the queue head and calls `play_file(...)`.
10. VLC plays the primary media file. If matching generated vocal/nonvocal `.m4a` files exist, VLC can use them as slave audio.

Associated generated vocal files are tied to the primary media basename:

```text
nonvocal/<basename>.m4a
vocal/<basename>.m4a
nonvocal/.<basename>.m4a
vocal/.<basename>.m4a
```

## Agreed Product Rules

### Quality Selection

- "Best quality" means best available YouTube quality up to 4K/2160p.
- HDR is allowed and preferred.
- 1440p or 1080p counts as best quality when the YouTube video has no 4K format.
- Best quality should favor VLC playability and karaoke features over theoretical maximum codec purity.
- YouTube 4K source streams are expected to be VP9 or AV1, so best-quality downloading must allow VP9/AV1 as temporary source formats.
- The final stored/playable best-quality file should be converted to H.265/HEVC using FFmpeg hardware acceleration on macOS: `hevc_videotoolbox`.
- The app should continue using VLC for playback/control after conversion.
- Manual high-quality UI choices should be overridden by the new queue-position rule.

### Queue Position Rules

Only `K.queue` waiting songs count for queue position. The currently playing song does not count.

- Queue empty: download default quality.
- Resulting queue position 1 through 5: download default quality.
- Resulting queue position 6 or later: download best quality.
- For concurrent download requests that are marked "add to queue after download", pending enqueued downloads should count toward the resulting queue position even before their files have finished downloading.
- If a pending download has not started, recompute its quality from the current queue position.
- If a best download has started and the song moves into positions 1 through 5:
  - If elapsed time is under 7 minutes, cancel and restart as default.
  - If elapsed time is 7 minutes or more, continue best download.
- If a default download has started and the song moves to position 6 or later:
  - Let default finish.
  - Do not explicitly schedule a follow-up best job from that code path.
  - The continuous background upgrader can discover and upgrade it later.

### Background Upgrader

- On by default.
- Disable with a startup argument: `--disable-quality-upgrader`.
- Runs continuously while the app is running.
- One background upgrade at a time.
- Foreground best-quality downloads and background upgrades should share the same best-quality work gate, so only one best download/conversion/verification/replacement pipeline runs at a time.
- Does not pause foreground/default downloads.
- Scans all local library songs, not only queued songs.
- Skips:
  - the currently playing file
  - the first 5 queued files
  - songs without a `---YouTubeID` suffix
  - songs already known to be best available
  - songs currently being downloaded or upgraded
  - songs under retry backoff
- Queue position 6+ songs are eligible.
- Songs not in the queue are eligible.
- If a best upgrade completes for a queued song, update the queue entry to the new file path immediately.
- If a best upgrade completes for an unqueued song, future queueing should use the new file normally.
- After a verified best-quality replacement:
  - delete the old primary media file
  - delete old generated vocal/nonvocal files
  - let the existing vocal splitter regenerate new vocal/nonvocal files
  - redownload only one subtitle track as part of the best download
  - choose the best subtitle matching the song language: manual subtitles first, then auto-generated captions
  - use YouTube language metadata, or a single manual subtitle track if that is the only available manual option
  - if no reliable matching language can be determined, skip subtitle download instead of downloading multiple subtitle tracks
  - stop the vocal splitter before the media swap and stale split-file cleanup
  - wait for the vocal splitter to exit; if it does not stop, abort the replacement and retry later
  - restart the vocal splitter after foreground best-quality replacements
  - for continuous background upgrades, defer the vocal splitter restart until the upgrader reaches a pause/no-candidate scan, so the splitter is not repeatedly started and stopped between back-to-back media swaps

### Disk Space

Before starting a best-quality download or upgrade, require enough free disk space.

Recommended rule:

- If yt-dlp can estimate source size: require `estimated_source_size + estimated_converted_size + old_file_size + 2 GB`.
- If converted size cannot be estimated, approximate it from the configured HEVC bitrate and video duration.
- If size cannot be estimated at all: require `max(10 GB, 3.5 * old_file_size)`.
- For brand-new best downloads without an old file, require source estimate plus converted estimate plus 2 GB, or 10 GB if no estimate is available.

### Retry Behavior

- Best-quality failures should retry with backoff.
- Store failure count and next retry time in metadata.
- Backoff should survive app restarts.
- Default-quality user downloads should keep the current behavior and should not be blocked by best-quality retry state.

### Shutdown Behavior

- When the user exits the pygame app, the application should stop accepting new best-quality background work.
- If a download, conversion, or verification is already in progress, the terminal-launched application should wait for that active job to finish before terminating.
- Shutdown should not interrupt an in-progress file replacement. This prevents broken, partial, or missing library files.
- After active download/conversion/verification jobs are drained, normal player, streamer, and vocal-splitter cleanup can proceed.

### Library UI

The local library page should show actual detected resolution, such as:

- `720p`
- `1080p`
- `1440p`
- `2160p`
- `audio`
- `unknown`

It can also distinguish whether the file is considered best available, for example:

- `2160p best`
- `1080p best`
- `720p default`
- `Upgrading`
- `720p retry later`
- `unknown`

The browse/library pages should show an explicit visual tag while a song is being upgraded to best quality. The tag should update while the page is open by polling local metadata only; it must not trigger network calls or block page rendering.

## Format and Container Research

### yt-dlp Behavior

yt-dlp supports downloading and merging best video plus best audio with `bv+ba/b`. It also supports sorting formats by resolution, HDR, codec, extension, size, and other fields. Its documented default sort includes `res`, `fps`, and `hdr:12`; it avoids preferring Dolby Vision by default because Dolby Vision is not broadly compatible across devices.

Useful yt-dlp documented examples:

```bash
yt-dlp -f "bv+ba/b"
yt-dlp -S "res:720,fps"
```

For this feature, the intended best-quality selection should be based on yt-dlp format sorting rather than hard-coding only one codec family.

Source: https://github.com/yt-dlp/yt-dlp#format-selection

### YouTube 4K, VLC, and Conversion

YouTube's consumer 4K streams are expected to be VP9 or AV1. YouTube does not provide H.265/HEVC streams, and H.264/AVC is capped below the desired 4K target. Therefore, if this feature is going to deliver 4K, it cannot avoid VP9/AV1 at the download stage.

The final playback problem is separate from the source download problem:

- Download source: best available YouTube video up to 2160p, likely VP9 or AV1.
- Final stored file: H.265/HEVC, encoded with `hevc_videotoolbox`, plus VLC-friendly audio/subtitles.

This keeps the current VLC-based control path while avoiding direct VLC playback of 4K VP9/AV1, which is expected to be less reliable for this karaoke workflow on macOS.

Source: https://www.videolan.org/vlc/features.html

### Final Container Recommendation

The current recommendation is:

- Temporary source file: let yt-dlp/FFmpeg use whatever container is natural for the selected VP9/AV1 source, usually WebM or MKV.
- Final library file: prefer MP4 with HEVC video and AAC audio if subtitles can be handled cleanly.
- Subtitle fallback: keep final MP4 and write one external matching-language `.srt` sidecar if MP4 subtitle conversion fails.
- Optional container fallback: MKV with HEVC video can preserve more subtitle formats, but it should only be used if consistent MP4 output is less important than preserving embedded subtitles.

MP4 is attractive because the existing default workflow already stores MP4 files and macOS players generally handle HEVC-in-MP4 well. MKV remains acceptable because the existing app indexes `.mkv`, and VLC can play MKV, but MP4 should be the default final converted asset.

### When Converted HEVC Could Still Fail

Converting to HEVC does not remove every risk. Likely failure cases are:

- The installed VLC version is old and lacks reliable HEVC or HDR handling.
- The Mac cannot decode the selected 4K HEVC stream smoothly in software or hardware.
- HDR metadata from the VP9/AV1 source is not preserved correctly through `hevc_videotoolbox`.
- A corrupt or incomplete download passes a weak file-exists check.
- VLC can play the file, but pitch shifting/transposition is too CPU-heavy with 4K HEVC.
- Subtitles are embedded in a way that VLC cannot select cleanly after conversion.
- The local FFmpeg build does not include the `hevc_videotoolbox` encoder.

These are conversion/playback issues rather than YouTube source-selection issues.

### Transcoding Recommendation

Transcode best-quality downloads by default.

Reasons:

- 4K YouTube source requires VP9/AV1.
- VLC on macOS is not expected to handle 4K VP9/AV1 well enough for this karaoke workflow.
- Adding another player such as mpv would be a larger integration change because the app depends on programmatic player control.
- FFmpeg's macOS VideoToolbox HEVC encoder should make conversion fast enough for background work.

Required encoder:

```bash
hevc_videotoolbox
```

If `hevc_videotoolbox` is unavailable, the app should not fall back to slow software HEVC conversion by default. Record the failure and retry later with backoff.

Default quality target:

- Avoid constant bitrate encoding.
- Use a configurable target average HEVC bitrate.
- Default target average bitrate: `20M` for 4K karaoke videos.
- Configure a strict ceiling cap with `-maxrate 28M` and `-bufsize 56M`.
- Approximate converted output size from target average bitrate and duration when checking disk space.

HDR handling:

- HDR is preferred, but reliable 4K HEVC output is more important than preserving HDR metadata.
- 4K SDR HEVC is acceptable if HDR metadata cannot be preserved reliably.
- Before conversion, inspect source pixel format and color metadata with `ffprobe`.
- Pass appropriate pixel-format/color parameters to FFmpeg when needed, especially for HDR sources where pixel format mismatch is likely.
- For 10-bit/HDR sources, request `p010le` and HEVC Main10 during VideoToolbox conversion.

Candidate conversion command shape:

```bash
ffmpeg -y \
  -i source-best.mkv \
  -map 0:v:0 -map 0:a:0? -map 0:s? \
  -c:v hevc_videotoolbox \
  -b:v 20M \
  -maxrate 28M \
  -bufsize 56M \
  -tag:v hvc1 \
  -c:a aac \
  -b:a 192k \
  -c:s mov_text \
  output-best.mp4
```

This command must be validated with real files. In particular:

- Subtitle conversion to `mov_text` may not work for every subtitle type.
- If subtitle conversion fails, retry as final MP4 with subtitles written as an external `.srt` sidecar.
- MKV output is only an optional subtitle-preservation fallback, not the default path.
- HDR and pixel-format parameters must be checked with `ffprobe`; additional color metadata flags may be required.

Fallback strategy if conversion fails:

1. Keep the existing default-quality file.
2. Record the best-upgrade failure and retry with backoff.
3. If the failure is specifically subtitle conversion, retry best conversion as MP4 with an external `.srt`.
4. Do not fall back to storing raw VP9/AV1 as the final playable asset unless explicitly enabled later.

## Proposed Best Download Profile

Keep the current default profile unchanged.

Best-quality profile should:

- select best available video up to `2160p`
- allow HDR
- avoid Dolby Vision unless explicitly requested later
- allow VP9/AV1 for the temporary YouTube source download
- select best available audio
- request only one subtitle track matching the song language; if unavailable or ambiguous, skip subtitles
- convert the final stored/playable file to HEVC with `hevc_videotoolbox`
- prefer final MP4 output
- handle subtitle conversion failures by using external `.srt` sidecars
- verify final media with `ffprobe` and a 10-second FFmpeg null-decode scan
- avoid forcing YouTube `player_client=web` for best-quality discovery/downloads, because that client can expose only legacy combined format `18` at 360p while the default/tv client set exposes higher adaptive video formats
- compare the downloaded source height and converted output height against yt-dlp's discovered best available height for that video, so true 360p-only videos can still be marked best while accidental 360p fallbacks are rejected

Candidate yt-dlp options:

```bash
--fixup force
--socket-timeout 3
-R infinite
--merge-output-format mkv
-f "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b"
-S "res:2160,fps,hdr,vcodec,acodec,size"
```

This should be refined during implementation after testing real YouTube format lists. In particular:

- yt-dlp's codec names must be verified against real YouTube formats.
- `hdr` ordering should avoid forcing Dolby Vision unless it proves safe on the target VLC version.
- Source downloads can be VP9/AV1 because they are temporary.
- Final playable files must not remain raw VP9/AV1.
- If no 4K source exists, the highest available resolution still counts as best available.

## Metadata Model

Add a sidecar metadata file in the song library, proposed:

```text
.quality.json
```

Key by YouTube ID where possible.

Example:

```json
{
  "abc123": {
    "youtube_id": "abc123",
    "title": "Song title",
    "path": "Song title---abc123.mp4",
    "quality_profile": "best",
    "best_available": true,
    "height": 2160,
    "width": 3840,
    "duration": 242.1,
    "filesize": 1234567890,
    "container": "mp4",
    "video_codec": "hevc",
    "audio_codec": "aac",
    "hdr": true,
    "source_video_codec": "vp9",
    "conversion_encoder": "hevc_videotoolbox",
    "format_id": "315+251",
    "checked_at": "2026-06-25T10:00:00+08:00",
    "failure_count": 0,
    "next_retry_at": null
  }
}
```

Metadata should be updated by:

- successful default downloads
- successful best downloads
- successful background upgrades
- library scan fallback using `ffprobe`
- rename/delete operations

For existing files without metadata:

- If filename contains `---YouTubeID`, infer YouTube ID.
- Use `ffprobe` to detect actual resolution, duration, container, and codecs.
- Use yt-dlp metadata only when deciding whether a higher quality is available.

Do not rely on file size and duration alone to decide whether a file is best quality. File size helps with disk planning, but resolution and yt-dlp format metadata are more reliable.

## Verification

After source download and conversion, before replacing any library file:

1. Confirm temporary source file exists and size is non-zero.
2. Run `ffprobe` on the temporary source to verify:
   - readable container
   - duration greater than zero
   - expected video stream
   - source height up to 2160p
   - audio stream present, when available
   - pixel format
   - color range, color space, color transfer, and color primaries
   - HDR-relevant side data, when present
3. Run a bounded FFmpeg null-decode scan on the first 10 seconds of the temporary source.
4. Convert source to final HEVC output using `hevc_videotoolbox`, passing pixel-format/color parameters when source inspection indicates they are needed.
5. Confirm final file exists and size is non-zero.
6. Run `ffprobe` on final output to verify:
   - readable container
   - duration greater than zero
   - expected video stream for video songs
   - detected height
   - video codec is HEVC/H.265
   - audio stream present
7. Run a bounded FFmpeg null-decode scan on the first 10 seconds of the final output.
8. Confirm quality meets the selected profile:
   - default: current behavior, no strict new validation needed beyond existing checks
   - best: best available source up to 2160p, converted to HEVC final output
9. Only then move/swap into the library.

Candidate null-decode scan:

```bash
ffmpeg -v error -t 10 -i final-output.mp4 -f null -
```

Replacement should be atomic where possible:

1. Download to temp.
2. Verify temp source.
3. Convert to temp final output.
4. Verify temp final output.
5. Move old file to a temporary backup name or keep it until new move succeeds.
6. Move new file into final library path.
7. Update queue entries.
8. Rescan library.
9. Delete old primary file, temp source, and stale vocal/nonvocal files immediately after the verified swap succeeds.
10. Trigger vocal/nonvocal regeneration immediately for the upgraded song.

## Queue and Path Updates

When replacing a file:

- Update any queue entries pointing to the old path.
- Preserve the visible title if the original file had a user-renamed title before `---ID`.
- Update `available_songs` via `get_available_songs()`.
- Mark `status_dirty` and call `update_queue()` if queue entries changed.

If the final extension changes, queue path updates are required. The final best-quality extension should remain `.mp4`. If MP4 subtitle embedding fails, use external subtitles rather than changing the final media container.

## Vocal Splitter Interaction

Best-quality replacement should delete stale generated vocal files because they are tied to the old basename and media content. Before swapping the primary media, stop the vocal splitter and wait for it to exit so it cannot read a media file while it is being replaced or write old split output during replacement. If the splitter does not stop within the timeout, abort the replacement and let retry/backoff handle the next attempt. After foreground best-quality replacements, restart the vocal splitter immediately. During continuous background upgrade batches, defer restart until the worker reaches a no-candidate pause so it does not repeatedly restart on one song and get stopped by the next immediate upgrade.

The existing vocal splitter discovers work from:

```python
q = ([K.now_playing_filename] if K.now_playing_filename else []) + [i['file'] for i in K.queue]
```

This means queued songs can be regenerated naturally. For unqueued upgraded library songs, implementation must add or reuse a way to trigger vocal regeneration immediately after replacement.

Implementation task to resolve in code:

- Check whether `vocal_splitter.py` scans the full download directory independently or only the queue endpoint.
- If it only uses the queue endpoint, add a direct regeneration trigger or a dedicated vocal-regeneration task queue for upgraded songs.

## Concurrency Design

Introduce explicit download job state rather than relying only on `self.downloading_songs`.

Proposed structures:

- foreground download jobs keyed by YouTube URL or ID
- one background upgrade worker
- cancellation handle for subprocess-based yt-dlp jobs
- quality metadata lock
- queue/library lock if needed for path swaps

The current in-process `yt_dlp.main(...)` path is difficult to cancel cleanly. For best-quality jobs and any cancellable foreground jobs, use an external subprocess invocation when possible.

Default-quality downloads can continue using the current call path unless cancellation support is needed for all pending jobs.

## Startup Arguments and Config

Add startup arguments:

```bash
--disable-quality-upgrader
--best-quality-cancel-threshold 420
```

Defaults:

- `--disable-quality-upgrader`: upgrader is **on** by default; pass this flag to disable it
- `--best-quality-cancel-threshold`: `420` seconds, or 7 minutes

Potential later options:

```bash
--best-quality-max-height 2160
--best-quality-min-free-space-gb 10
--best-quality-container mp4
--best-quality-video-bitrate 20M
--best-quality-video-maxrate 28M
--best-quality-video-bufsize 56M
```

For now, hard-code max height 2160, final MP4 output, and `hevc_videotoolbox` unless implementation testing shows a need for configurability.

## Library UI Changes

The browse/library templates should display a resolution tag for each song.

Data needed per song path:

- detected height
- quality profile
- whether best available is known
- upgrade state, if active or failed

Possible labels:

- `720p default`
- `1080p`
- `1440p best`
- `2160p best`
- `Upgrading`
- `unknown`

Keep the UI lightweight and avoid blocking page render on yt-dlp network calls. Use stored metadata and local ffprobe data only during page render. Retry failures should be logged to stdout only and should not be surfaced in the web UI.

## Implementation Phases

### Phase 1: Metadata and Probing

- Add `.quality.json` read/write helpers.
- Add YouTube ID extraction from filenames.
- Add `ffprobe` helper.
- Populate metadata during library scan without network calls.
- Add library UI resolution tags.

### Phase 2: Best Download Profile

- Add best-quality yt-dlp option builder.
- Add temporary source download and verification.
- Download best source up to 2160p, allowing YouTube VP9/AV1 source streams.
- Inspect source pixel format and color metadata before conversion.
- Convert best source to HEVC using FFmpeg `hevc_videotoolbox`.
- Verify source and final output with `ffprobe` and 10-second FFmpeg null-decode scans.
- Store final best-quality output as MP4.
- Trigger vocal/nonvocal regeneration immediately after a verified replacement.
- Preserve current default profile.
- Add queue-position quality decision in `/download` or `download_video`.

### Phase 3: Background Upgrader

- Add `--disable-quality-upgrader` startup flag (upgrader is on by default).
- Add continuous worker thread.
- Refresh and scan eligible local library files on every worker pass, rather than relying only on the startup cache.
- Skip now-playing and queue positions 1 through 5.
- Respect free-space rules.
- Retry with backoff.
- Swap verified replacements.
- Print stdout scan diagnostics when no song is eligible, including counts for common skip reasons such as no YouTube ID, already best, queue protected, currently upgrading, and retry backoff.

### Phase 4: Cancellation and Dynamic Queue Priority

- Track pending/active download jobs.
- Recompute pending job quality when queue changes.
- Cancel active best jobs under 7 minutes if they become urgent.
- Let active best jobs continue after 7 minutes.
- Drain active download/conversion/verification jobs during application shutdown before process cleanup.

### Phase 5: Hardening

- Test real YouTube videos with:
  - 720p only
  - 1080p only
  - 1440p
  - 2160p SDR
  - 2160p HDR VP9 source converted to HEVC
  - 2160p HDR AV1 source converted to HEVC
  - videos where 2160p is only available as VP9/AV1
  - videos where no 2160p source exists
  - subtitles
- Test that final converted HEVC files play in VLC on macOS.
- Test VLC controls against converted HEVC files:
  - pause/resume
  - seek
  - volume
  - pitch/key transpose
  - speed change
  - subtitles
  - vocal/nonvocal slave audio
- Test queue movement during active downloads.
- Test replacing queued paths.
- Test deleting stale vocal/nonvocal files.
- Test low disk space.

## Ready For Implementation

All product-level questions from discovery are resolved. Implementation should still validate command support and codec behavior on the local macOS FFmpeg/VLC installation while building the feature.
