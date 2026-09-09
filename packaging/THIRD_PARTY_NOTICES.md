# Third-party components in the Windows distribution

The distribution includes independent third-party components and retains their
license files. No affiliation with Google or the projects below is implied.

## AIStudioToAPI

Authors: Ellinav, iBenzene, bbbugg and the project contributors.
Version 1.3.7, commit db624c24ab111ad0f50308f20f8a75a216dbf873.
Source: https://github.com/iBUHub/AIStudioToAPI/tree/db624c24ab111ad0f50308f20f8a75a216dbf873
License: Creative Commons Attribution-NonCommercial 4.0 International.
The complete license and upstream source are in app/vendor/aistudio/app/.
The bundled release is not modified. job-bless supplies separate process and
browser integration scripts in app/src/aistudio/.
The bundled AIStudioToAPI component is licensed for noncommercial use; this
distribution does not grant additional commercial rights to it.

## Camoufox / Firefox

Camoufox 135.0.1-beta.24, Windows x86_64, by daijro and contributors.
Release and corresponding source: https://github.com/daijro/camoufox/releases/tag/v135.0.1-beta.24
Source repository: https://github.com/daijro/camoufox
License: Mozilla Public License 2.0 and included third-party notices.
The unmodified browser distribution is in app/vendor/aistudio/camoufox/;
its licensing information is also accessible through about:license.

## Node.js

Node.js 22.18.0, by the Node.js contributors.
Source: https://github.com/nodejs/node/tree/v22.18.0
MIT and bundled third-party licenses: app/vendor/aistudio/node/LICENSE.

## Python

CPython 3.13.11, by the Python Software Foundation and contributors.
Source: https://www.python.org/downloads/release/python-31311/
PSF License and notices: runtime/python/LICENSE.txt.

## Package dependencies

Node package sources and notices are retained in app/vendor/aistudio/app/node_modules/.
Python package license metadata is retained in runtime/python/Lib/site-packages/.
The build uses locked package versions; see the source project's packaging/ folder.
