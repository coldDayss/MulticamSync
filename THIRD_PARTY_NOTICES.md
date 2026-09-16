# Third-party components

The application invokes FFmpeg as a separate local process. It uses Python and NumPy for metadata processing, audio features and correlation. No user video is uploaded by the application.

## FFmpeg / FFprobe

- Bundled build: `9.0.1-essentials_build-www.gyan.dev`, Windows x64.
- Distribution: https://www.gyan.dev/ffmpeg/builds/
- Download used: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
- Upstream source: https://ffmpeg.org/download.html and https://git.ffmpeg.org/ffmpeg.git
- Build information and external-library source links: https://www.gyan.dev/ffmpeg/builds/#libraries
- This build enables GPL and version 3. The build's supplied GPL license is included at `bin/LICENSE`.
- The build includes external codecs such as x264 and x265; those projects retain their respective copyrights and licenses.

## Python

- Python 3.12 runtime is included in `runtime` and runs the application's Python files directly.
- Copyright Python Software Foundation and contributors.
- License text: `runtime/LICENSE.txt`; upstream license: https://docs.python.org/3/license.html
- Source: https://www.python.org/downloads/source/

## NumPy

- Copyright NumPy Developers. BSD 3-Clause license; bundled binary dependencies carry their own notices.
- NumPy distribution metadata and license texts are included at `runtime/Lib/site-packages/numpy-2.3.5.dist-info`.
- Source and licensing: https://github.com/numpy/numpy

## Packaging

This portable distribution uses the included Python interpreter directly. It does not contain or require a PyInstaller bootloader.

## Implementation references

- FFmpeg filters, scaling, timestamps, concatenation and layouts: https://ffmpeg.org/ffmpeg-filters.html
- Cross-correlation definition and lag convention: https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html

This application implements its correlation with NumPy; SciPy is not required.
