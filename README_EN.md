# Multicam Sync

A Windows program that plays footage from multiple cameras in sync, then exports it as a combined layout or as per-camera clips in MP4.

## Running

**Fully extract** the distribution ZIP, then double-click `실행.cmd` (Run). The workspace opens in your default browser. No separate Python or FFmpeg installation is required. All video processing happens locally on this PC. `실행.vbs` is an alternative launcher that starts without a console window.

Keep the `runtime`, `bin`, and `static` folders together with the Python files in the same location. Closing the browser window does not stop the program; launching it again reopens the same workspace. Use **Quit Program** at the top of the screen to exit.

## 1. Load a Shoot Folder

Click **Select Folder** or paste a folder path. Select the parent folder that contains all cameras. The bundled example is `multi-cam sample video/Three_CAM`; **Load Three_CAM sample** on the welcome screen opens it from that relative path.

```
Shoot folder/
  CAM A/  clip1.MP4, clip2.MP4, ...
  CAM B/  clip1.MP4, clip2.MP4, ...
  CAM C/  ...
```

The number of cameras is inferred from names such as `CAM A`, `CAM B`, and `CAM C`. Multiple clips from the same camera are shown on a single track, with recording gaps preserved. If no CAM names are found, clips are grouped by subfolder, and individual files at the top level are each treated as a separate camera. DJI `.LRF` files are not double-counted with their original videos.

## 2. Auto Sync

After loading a folder, **Auto Sync → Preview Preparation** runs automatically. The default Auto mode references recording time and timecode, and compares multiple segments of recorded audio.

- **Auto / Audio:** Computes the time offset from how well multiple audio segments match.
- **Timecode:** Uses embedded SMPTE timecode. The cameras' timecode clocks must be synchronized.
- Clips with no audio or a weak match are marked **Unconfirmed**. Clips temporarily placed by recording time require manual verification.
- Confidence reflects the degree of match found by the analysis. It is not a probability that guarantees actual frame accuracy.

Previews are generated as lightweight H.264 video, so HEVC originals can also be viewed in the browser. Initial analysis and preview preparation take time proportional to footage length; subsequent sessions reuse the cache.

## 3. Manual Adjustment

Use the global play button and timeline to view all cameras at the same point in time. Selecting a common recording segment jumps to a section that all cameras captured together.

- Adjust each camera's time by entering a number or using the **±1 frame / ±100 ms** buttons.
- **A positive value moves that camera later on the timeline**; a negative value moves it earlier.
- If only a specific clip is off, adjust that clip's individual start time.
- Re-running Auto Sync resets manual corrections. Save the project JSON first if needed.
- Browser preview may momentarily show frame differences depending on performance. The final export is rendered on a shared frame count.

Saving a project stores source paths and sync information. Source videos are not copied, so if you move the originals, reload the folder.

## 4. Export

Set the start and end points, then choose an output mode.


| Option     | Result                                                 |
| ---------- | ------------------------------------------------------ |
| Combined   | One MP4 with all cameras merged in the selected layout |
| Individual | Per-camera MP4s trimmed to the same time range         |
| Both       | Both the combined MP4 and individual MP4s              |


You can choose horizontal, vertical, grid, or picture-in-picture (PIP) layouts, as well as camera order. In PIP, the first camera is the main view and the others appear as small views on the right. Audio for the combined video comes from one selected camera, or can be muted. Each individual video keeps its own camera's audio.

By default, individual videos are saved at the resolution of the largest source video for each camera. Even if resolutions differ between cameras, output FPS and time range are identical. The selected output resolution applies to the combined video. Source aspect ratios are preserved with black padding, and periods with no recording are filled with black frames and silence. When clips from the same camera overlap, the one that starts first takes priority. Export ranges are snapped to frame boundaries at the selected FPS.

When finished, download results from the screen or find them in the **exports** folder inside the program folder. Each export folder contains the videos along with the project and settings JSON used.

## Current Support

Windows 64-bit, minimum of 2 cameras. Reads MP4, MOV, MKV, AVI, MTS, M2TS, MXF, WEBM, and M4V. Output is H.264/AAC MP4. Working with dozens of cameras or long high-resolution footage simultaneously depends on the PC's memory and CPU. Audio auto sync corrects a **fixed time offset per clip**; it does not automatically stretch or compress for speed differences (drift) that occur during long recordings.

## Source Code

The program consists of `app.py`, `media_engine.py`, and `export_engine.py` in the program folder, plus HTML/CSS/JavaScript in the `static` folder. It runs directly on the bundled Python runtime; PyInstaller is not required. FFmpeg documentation: [Filters, compositing, and timing](https://ffmpeg.org/ffmpeg-filters.html). For licensing and distribution details, see `THIRD_PARTY_NOTICES.md`.