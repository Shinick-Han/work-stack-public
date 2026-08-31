# Third-party notices

## FullCalendar 6.1.21

Work Stack uses the MIT-licensed FullCalendar Standard packages (`core`, `react`,
`daygrid`, `list`, and `interaction`) for the optional Focus Agenda surface. No
Premium scheduler or resource-timeline package is included. A complete copy of the
license is distributed at `licenses/fullcalendar-6.1.21-LICENSE.md`.

## unicodedata2 17.0.0

Work Stack uses `unicodedata2` to pin Unicode Standard 17.0.0 normalization semantics
independently of the host Python runtime. The package metadata declares the Apache
License, Version 2.0. A complete copy is distributed at
`licenses/unicodedata2-17.0.0-LICENSE.txt`.

No `unicodedata2` source is vendored in this repository.

## Python 3.12.10 embeddable runtime

The Windows setup artifact bundles the official CPython 3.12.10 64-bit embeddable
distribution downloaded from python.org and verified against a pinned SHA-256 digest.
Its Python Software Foundation license is included in the installed `runtime/LICENSE.txt`.
The runtime archive is fetched only while building the setup artifact; installation is
offline and does not vendor the archive in this source repository.

## Windows desktop WebView host

The Windows setup artifact includes pywebview 6.2.1 (BSD-3-Clause), pythonnet
3.1.0 (MIT), clr-loader 0.3.1 (MIT), Bottle 0.13.4 (MIT), CFFI 2.1.1
(MIT-0), pycparser 3.0 (BSD-3-Clause), typing-extensions 4.16.0 (PSF-2.0),
and proxy_tools 0.1.0 (MIT). They let the signed CPython runtime host the
installed Microsoft Edge WebView2 Runtime without introducing a newly compiled
Work Stack executable. Package license texts remain in each installed
`runtime/Lib/site-packages/*.dist-info/licenses` directory where supplied.

pywebview's wheel also carries Microsoft WebView2 managed and native loader
components signed by Microsoft. Work Stack does not bundle the Evergreen browser
runtime itself; Windows supplies that runtime separately.

## Optional QR transfer tooling

`requirements-qr-windows.txt` independently pins optional, non-product tooling: colorama 0.4.6
(BSD), Pillow 12.3.0 (MIT-CMU), qrcode 8.2 (BSD), and zxing-cpp 3.1.1 (Apache-2.0). Their wheel
metadata and license files are installed by pip when a developer explicitly enables the tools.
No wheel or package source is vendored in this repository or the Work Stack setup artifact.
