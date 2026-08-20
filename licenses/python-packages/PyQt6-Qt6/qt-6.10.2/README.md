# Qt 6.10.2 upstream notices and provenance

This directory contains unmodified SPDX SBOMs from the official Qt 6.10.2
MSVC 2022 x86-64 binary archives and the license texts referenced by the
dependency closure of the Qt artifacts shipped by LoL Replay Tool. The SBOMs
are upstream module inventories and therefore also describe files that this
application does not ship. `licenses/distribution-manifest.json` is the
controlling technical inventory for a particular application build.

このディレクトリには、Qt 6.10.2公式MSVC 2022 x86-64 binary archiveから
そのまま取得したSPDX SBOMと、LoL Replay Toolが同梱するQt成果物の依存関係で
参照されるライセンス本文を収録しています。SBOMはupstream module全体の
inventoryなので、このアプリが同梱しないファイルも記載されています。
個別buildの技術的な配布物一覧は`licenses/distribution-manifest.json`を参照して
ください。

## Shipped Qt artifacts / 同梱するQt成果物

- `qtbase`: `Qt6Core.dll`, `Qt6Gui.dll`, `Qt6Network.dll`, `Qt6Widgets.dll`,
  `qtuiotouchplugin.dll`, `qgif.dll`, `qico.dll`, `qjpeg.dll`, `qminimal.dll`,
  `qoffscreen.dll`, `qwindows.dll`, and `qmodernwindowsstyle.dll`.
- `qtsvg`: `Qt6Svg.dll`, `qsvgicon.dll`, and `qsvg.dll`.
- `qtimageformats`: `qicns.dll`, `qtga.dll`, `qtiff.dll`, `qwbmp.dll`, and
  `qwebp.dll`.

`Qt6Pdf.dll`, `qpdf.dll` and Mesa `opengl32sw.dll` are not used or shipped.
Microsoft Visual C++ runtime files have separate component records and are not
covered by this directory.

`Qt6Pdf.dll`、`qpdf.dll`、Mesaの`opengl32sw.dll`は使用せず同梱しません。
Microsoft Visual C++ runtimeは別componentとして管理し、このディレクトリの
対象には含めません。

## Verified upstream archives / 検証した公式archive

| Module | Official binary archive SHA-256 | Official source archive SHA-256 | Source revision |
| --- | --- | --- | --- |
| `qtbase` | `c4cedcc54d2036ab20b193db113cf324d90f390647f831a522952cc9b158f38b` | `aeb78d29291a2b5fd53cb55950f8f5065b4978c25fb1d77f627d695ab9adf21e` | `000d6c62f7880bb8d3054724e8da0b8ae244130e` |
| `qtsvg` | `2c85364c1464100b583c36f071c6fca9fa36e1951a1f82ff26bf0a151de26e6c` | `f07ff80f38caf235187200345392ca7479445ddf49a36c3694cd52a735dad6e1` | `b925029db51aff0b17a48f5939cb83c27932d0cb` |
| `qtimageformats` | `726cb7ef71f140e736af04c75665af059c3b5490334f06ba91506efba9e51272` | `8b8f9c718638081e7b3c000e7f31910140b1202a98e98df5d1b496fe6f639d67` | `076fb82c55321e42beeae62d9e3ca8c4bb71439c` |

Every shipped Qt artifact listed above is byte-identical to the corresponding
member of the official binary archive. The official archive SBOM records the
MSVC 19.39.33520.0 build and source inventory, and every substantive source
record was matched to the official submodule source archive (line-ending
normalization was required for some text files). This establishes the
upstream artifact and source relationship used by this audit.

上記の各Qt成果物は、公式binary archive内の対応memberとbyte単位で一致します。
公式archiveのSBOMはMSVC 19.39.33520.0によるbuildとsource inventoryを記録し、
実質的な全source recordを公式submodule source archiveと照合しました。一部の
text fileでは改行コードの正規化が必要でした。

This evidence does not claim that the PyQt6-Qt6 wheel publisher exposed every
wheel repackaging step, patch, or build script. That remaining provenance
question stays recorded as a Release gate in `licenses/components.json`.

この証拠は、PyQt6-Qt6 wheel公開者がwheelの再packaging手順、patch、build
scriptをすべて公開したと主張するものではありません。その残存provenanceは
`licenses/components.json`でRelease gateとして維持します。
